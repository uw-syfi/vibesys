"""The host helper's interfaces on a real host: projects, directories, launches, the checkout.

Each class implements one interface of ``server.host_helper``; the helper's entrypoint
wires them, and ``vibesys tasks`` reads tasks through ``FileProjects``. Processes start
through ``vs_sim.api``'s launchers, so this module names no interpreter of its own: a
launch runs the command line the ``vibesys`` launcher would. Like the helper, the module
is light (``tests/architecture/test_light_entrypoints.py``).

Public surface: ``FileProjects``, ``ProjectStopIntents``, ``OsDirectories``,
``SubprocessRunLauncher``,
``SubprocessRunPreflight``, ``LoginStarter`` with ``LoginBounds`` and ``login_instruction``,
``read_task_goal``,
``GitCheckoutProbe``, ``OsConfigFiles``, and ``StoreLogReader`` with ``run_streams_directory``.
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from pydantic import JsonValue, TypeAdapter

from server.host_agent_logins import LoginStarted, LoginUnavailable, LoginUnavailableReason
from server.host_api import (
    RUN_EVENTS_FILE,
    RUN_EVENTS_READ_MAX_BYTES,
    Checkout,
    DetachedLaunchErrorCode,
    DetachedLaunchFailure,
    HelperErrorCode,
    LiveInstanceRecord,
    LogOffset,
    LogOwner,
    LogPipe,
    LogPipeSpan,
    LogStream,
    LogStreamOutcome,
    LogTail,
    RunDigest,
    RunDigestUnreadable,
)
from server.host_examples import ExampleObjective, TaskGoal, TaskList, TaskSummary, bounded_goal
from server.host_helper_logs import (
    LogStoreCorruptError,
    RunLocation,
    StoredBytes,
    UnknownInstanceError,
    UnknownLogStreamError,
)
from server.host_helper_machine import git_state, vibesys_version
from server.host_helper_preflight import preflight_unavailable
from server.host_helper_requests import (
    EventPage,
    PathHints,
    ProjectLookupError,
    ProjectRun,
    Scan,
    ScannedEntry,
    SpanEventPage,
    TailPage,
    event_page,
)
from server.host_preflight import PreflightReport
from server.run_digest import digest_journal
from server.span_index import (
    Entry,
    JournalIndex,
    StaleIndexError,
    load_journal_index,
    read_documents,
    span_page,
)
from vs_decode.api import Unreadable, decode_json
from vs_project.api import (
    GitRunHistory,
    NullGitTrackerEvents,
    Project,
    ProjectError,
    load_objectives,
)
from vs_sim.api import ProcessSpec, ProcessTimeoutError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from server.run_summary import Summarized
    from server.run_summary_cache import SummaryReader
    from vs_logs.read import LogReader as StoreReader
    from vs_project.api import GitRepositoryFactory, TaskDirectory
    from vs_sim.api import (
        CommandProbe,
        PipedProcess,
        PipedProcessLauncher,
        ProcessLauncher,
        Threads,
    )

_PYPROJECT_LIMIT_BYTES = 1 << 20
_LAUNCH_OUTPUT_TAIL = 2000
_LAUNCH_RECORD_LIMIT = 1 << 20
_KEPT_INDEXES = 4


_TALLY_CHUNK_BYTES = 1 << 20
_EVENT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def _journal_began(log_directory: Path) -> datetime:
    """When the run's journal says it started: its first event's time, else the file's.

    A run with no manifest has no recorded creation time; the journal's first event is
    the nearest thing, and an unreadable first line leaves the file's modification time.
    """
    journal = log_directory / RUN_EVENTS_FILE
    try:
        with journal.open("rb") as lines:
            first = lines.readline(_LAUNCH_RECORD_LIMIT)
        stamp = journal.stat().st_mtime
    except OSError:
        return datetime.fromtimestamp(0, UTC)
    decoded = decode_json(first, _EVENT, source="journal line", max_bytes=_LAUNCH_RECORD_LIMIT)
    began = decoded.get("timestamp") if not isinstance(decoded, Unreadable) else None
    with suppress(ValueError):
        if isinstance(began, str):
            parsed = datetime.fromisoformat(began)
            # A stamp with no offset is UTC, as the journal writes it; a naive time would make
            # sorting it against the manifests' aware times raise TypeError.
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.fromtimestamp(stamp, UTC)


@dataclass(frozen=True)
class JournalTally:
    """The complete lines of a journal's first ``size`` bytes."""

    size: int
    lines: int


def tally_journal(stream: BinaryIO, previous: JournalTally) -> JournalTally:
    """``previous`` extended over what ``stream`` now holds past it, counting whole lines only.

    A journal that is shorter than ``previous`` is counted from its start. A last line
    still being written has no newline yet and is not counted: the tally ends before it.
    """
    stream.seek(0, os.SEEK_END)
    if stream.tell() < previous.size:
        previous = JournalTally(0, 0)
    stream.seek(previous.size)
    size, lines = previous.size, previous.lines
    while True:
        start = stream.tell()
        chunk = stream.read(_TALLY_CHUNK_BYTES)
        if not chunk:
            break
        newlines = chunk.count(b"\n")
        if newlines:
            lines += newlines
            size = start + chunk.rindex(b"\n") + 1
    return JournalTally(size, lines)


def read_task_goal(task: TaskDirectory) -> TaskGoal:
    """What ``task`` is for: its ``OBJECTIVE.md`` (cut to ``GOAL_MAX_CHARS``) and its axes.

    Raises ``OSError`` for a file that cannot be read and ``ValueError`` (naming the file) for
    ``objectives.toml`` that is invalid or a goal that is not UTF-8 text.
    """
    objectives = load_objectives(task.path).objective
    try:
        text = task.objective_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        message = f"{task.objective_path} is not UTF-8 text: {exc}"
        raise ValueError(message) from exc
    goal, truncated = bounded_goal(text)
    return TaskGoal(
        task=task.name.value,
        objectives=tuple(
            ExampleObjective(metric=axis.name, direction=axis.direction) for axis in objectives
        ),
        goal=goal,
        goal_truncated=truncated,
    )


class FileProjects:
    """Projects read through ``vs_project``'s ``Project`` on this filesystem.

    Runs are read through ``vs_project``'s ``RunHistory`` over the repository
    ``repositories`` opens at the project root, so every run lists whichever branch the
    project has checked out.
    """

    def __init__(self, repositories: GitRepositoryFactory, threads: Threads) -> None:
        """Read run branches through the repositories ``repositories`` opens."""
        self._repositories = repositories
        self._threads = threads
        self._journals: dict[tuple[Path, str], Path] = {}
        self._tallies: dict[Path, JournalTally] = {}
        self._digests: dict[Path, tuple[tuple[int, int], RunDigest]] = {}
        self._indexes: dict[Path, JournalIndex] = {}
        self._summary_reader: SummaryReader | None = None

    def tasks(self, project: Path) -> TaskList:
        """The project's tasks, ordered by name."""
        opened = self._initialized(project)
        try:
            tasks = opened.discover_tasks()
        except ProjectError as exc:
            raise ProjectLookupError(
                HelperErrorCode.TASK_INVALID, str(opened.root), str(exc)
            ) from exc
        return TaskList(
            project_root=str(opened.root),
            tasks=tuple(TaskSummary(name=task.name.value) for task in tasks),
        )

    def task_goal(self, project: Path, task: str) -> TaskGoal:
        """The goal and axes of the project's task ``task``."""
        opened = self._initialized(project)
        try:
            return read_task_goal(opened.select_task(task))
        except (ProjectError, OSError, ValueError) as exc:
            raise ProjectLookupError(
                HelperErrorCode.TASK_INVALID, str(opened.root), str(exc)
            ) from exc

    def runs(self, project: Path) -> tuple[Path, tuple[ProjectRun, ...]]:
        """The project's root and its runs, oldest first; runs this VibeSys cannot read are left out.

        A run is its identity, not only its portable record: a run with a journal in this
        machine's log store is listed even when its manifest was never committed or its
        branch is gone (a run that stopped at start-up), under its id with no task.
        """
        opened = self._open(project)
        try:
            history = GitRunHistory(opened, self._repositories(opened.root, NullGitTrackerEvents()))
            manifests = history.recorded().runs
            logged = Project.logged_run_ids_for(opened.root)
        except ProjectError as exc:
            raise ProjectLookupError(
                HelperErrorCode.TASK_INVALID, str(opened.root), str(exc)
            ) from exc
        recorded = [
            ProjectRun(
                run_id=manifest.run_id,
                task=manifest.task_name,
                display_name=manifest.display_name,
                created_at=manifest.created_at,
            )
            for manifest in manifests
        ]
        known = {run.run_id for run in recorded}
        journaled = [
            ProjectRun(
                run_id=run_id,
                task=None,
                display_name=run_id,
                created_at=_journal_began(Project.log_directory_for(opened.root, run_id)),
            )
            for run_id in logged
            if run_id not in known
            and (Project.log_directory_for(opened.root, run_id) / RUN_EVENTS_FILE).is_file()
        ]
        runs = sorted([*recorded, *journaled], key=lambda run: (run.created_at, run.run_id))
        return opened.root, tuple(runs)

    def run_digest(self, project_root: Path, run_id: str) -> RunDigest | RunDigestUnreadable | None:
        """What run ``run_id``'s journal says at a glance; ``None`` when there is no journal.

        An ended run's journal never changes, so its digest is kept for as long as the journal
        keeps its size and modification time; a run still going is read again.
        """
        journal = Project.log_directory_for(project_root, run_id) / RUN_EVENTS_FILE
        try:
            with journal.open("rb") as lines:
                stamp = os.fstat(lines.fileno())
                version = (stamp.st_size, stamp.st_mtime_ns)
                kept = self._digests.get(journal)
                if kept is not None and kept[0] == version:
                    return kept[1]
                digest = digest_journal(lines)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return RunDigestUnreadable(
                message=f"the run's journal could not be read: {exc.strerror}"
            )
        if digest.ending is not None or digest.closing is not None:
            self._digests[journal] = (version, digest)
        return digest

    def require_initialized(self, project: Path) -> Path:
        """The project's root, once it is known to define tasks."""
        return self._initialized(project).root

    def run_events(
        self, project: Path, run_id: str, from_sequence: int, limit: int, *, with_totals: bool
    ) -> EventPage:
        """A page of run ``run_id``'s journal, once the project is known to have the run."""
        journal = self._journal(project, run_id)
        try:
            with journal.open("rb") as lines:
                page = event_page(lines, from_sequence, limit, RUN_EVENTS_READ_MAX_BYTES)
                if not with_totals:
                    return page
                tally = self._tally(journal, lines)
        except FileNotFoundError:
            nothing = EventPage((), from_sequence, complete=True)
            return replace(nothing, total_events=0, total_bytes=0) if with_totals else nothing
        return replace(page, total_events=tally.lines, total_bytes=tally.size)

    def _journal(self, project: Path, run_id: str) -> Path:
        """The journal of ``run_id``, found through the project's run history once per channel.

        Every page of one run's history used to list the project's runs through git again.
        The helper serves one channel, so a run it found stays found. The cache is a dict
        of immutable paths that workers only ever add to: two workers racing to resolve
        the same run store the same value, so it needs no lock.

        A run the project records but whose journal this machine does not hold (it is in another
        project's log store, or its checkout predates the journal) is refused as ``unknown_run``
        by every read, the summary's way: an empty page would call it a run with no events.
        """
        key = (project, run_id)
        journal = self._journals.get(key)
        if journal is None:
            root, runs = self.runs(project)
            if all(run.run_id != run_id for run in runs):
                message = f"{root} has no run {run_id!r}"
                raise ProjectLookupError(HelperErrorCode.UNKNOWN_RUN, str(root), message)
            journal = Project.log_directory_for(root, run_id) / RUN_EVENTS_FILE
            self._journals[key] = journal
        if not journal.is_file():
            message = f"{project} holds no journal of run {run_id!r}"
            raise ProjectLookupError(HelperErrorCode.UNKNOWN_RUN, str(project), message)
        return journal

    def run_journal(self, project: Path, run_id: str) -> Path:
        """The journal file of run ``run_id``, once the project is known to have the run."""
        return self._journal(project, run_id)

    def _tally(self, journal: Path, stream: BinaryIO) -> JournalTally:
        """The journal's complete lines, counted from where the last count stopped.

        A journal is append-only, so the count of its first ``size`` bytes never changes;
        a shorter file than the last count read is a different journal and counts afresh.
        Each worker stores a whole ``JournalTally`` (never a half-updated one), so a race
        leaves a correct, if older, tally.
        """
        last = self._tallies.get(journal)
        tally = tally_journal(stream, last if last is not None else JournalTally(0, 0))
        self._tallies[journal] = tally
        return tally

    def run_span_events(
        self, project: Path, run_id: str, span_id: str, from_sequence: int, limit: int
    ) -> SpanEventPage:
        """A page of the journal events of ``span_id`` and the spans beneath it, from the span index."""

        def ask(index: JournalIndex) -> tuple[list[Entry], tuple[int, int, bool]]:
            entries, total, complete = span_page(
                index, span_id, from_sequence, limit, RUN_EVENTS_READ_MAX_BYTES
            )
            next_sequence = entries[-1].sequence + 1 if entries else from_sequence
            return entries, (next_sequence, total, complete)

        (next_sequence, total, complete), documents = self._indexed_read(
            self._journal(project, run_id), ask
        )
        return SpanEventPage(tuple(documents), next_sequence, complete=complete, total=total)

    def run_tail(
        self, project: Path, run_id: str, before_sequence: int | None, limit: int
    ) -> TailPage:
        """The newest events before ``before_sequence`` that one page carries, from the span index."""

        def ask(index: JournalIndex) -> tuple[list[Entry], tuple[int, bool]]:
            entries, older = index.tail(before_sequence, limit, RUN_EVENTS_READ_MAX_BYTES)
            return entries, (index.last_sequence, older)

        (last_sequence, older), documents = self._indexed_read(self._journal(project, run_id), ask)
        return TailPage(tuple(documents), complete=not older, last_sequence=last_sequence)

    def run_summary(self, project: Path, run_id: str) -> Summarized:
        """The fold of run ``run_id``'s journal, from the cache beside it and what it gained since."""
        # lint-waiver: LW-640283 [PLC0415]; the summary fold costs the helper nothing until asked.
        # > At module level every channel would load the fold and its cache before `hello`, which
        # > the helper's start-up ceiling forbids; the import stays visible to tach.
        from server.run_summary_cache import SummaryReader  # noqa: PLC0415

        if self._summary_reader is None:
            self._summary_reader = SummaryReader(self._threads.lock())
        return self._summary_reader.summarize(self._journal(project, run_id))

    def _indexed_read[Facts](
        self, journal: Path, ask: Callable[[JournalIndex], tuple[list[Entry], Facts]]
    ) -> tuple[Facts, list[dict[str, JsonValue]]]:
        """What ``ask`` says of ``journal``'s span index, with the events of the entries it picks.

        The index of each recently read journal is kept, and checked against the journal on
        every use (``load_journal_index``), so an answer never depends on what was kept. A
        journal rewritten between reading the index and its events (``StaleIndexError``) is
        read again from a fresh index; a second change in a row is the caller's failure.
        """
        index = self._index_of(journal, self._indexes.get(journal))
        entries, facts = ask(index)
        try:
            return facts, read_documents(journal, entries)
        except StaleIndexError:
            entries, facts = ask(self._index_of(journal, None))
            return facts, read_documents(journal, entries)

    def _index_of(self, journal: Path, previous: JournalIndex | None) -> JournalIndex:
        index = load_journal_index(journal, previous)
        self._indexes[journal] = index
        for forgotten in list(self._indexes)[:-_KEPT_INDEXES]:
            self._indexes.pop(forgotten, None)
        return index

    def _open(self, project: Path) -> Project:
        try:
            return Project.open(project)
        except ProjectError as exc:
            raise ProjectLookupError(
                HelperErrorCode.PROJECT_MISSING, str(project), str(exc)
            ) from exc

    def _initialized(self, project: Path) -> Project:
        opened = self._open(project)
        if not opened.is_initialized():
            message = (
                f"{opened.root} has no VibeSys tasks: "
                f"{opened.configuration_path()}/tasks/ does not exist"
            )
            raise ProjectLookupError(
                HelperErrorCode.PROJECT_UNINITIALIZED, str(opened.root), message
            )
        return opened


class OsConfigFiles:
    """The machine's configuration files on this filesystem (``ConfigFiles``)."""

    def read(self, path: str) -> str | None:
        """The file's text, or ``None`` when it does not exist."""
        try:
            return Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def write(self, path: str, text: str) -> None:
        """Replace the file whole: write a sibling, then rename it over the file."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            staged.write_text(text, encoding="utf-8")
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)


class ProjectStopIntents:
    """Records an operator's stop in the run's project state (``record_stop_request``)."""

    def record(self, record: LiveInstanceRecord) -> None:
        """Note the stop in ``record``'s project; a project that cannot take it is left as it is."""
        try:
            Project.open(record.project_root).state.record_stop_request(record.run_id)
        except (ProjectError, OSError):
            return


class OsDirectories:
    """Directories as this user's account lists them."""

    def home(self) -> str:
        """``$HOME``, or the account's home directory."""
        return str(Path.home())

    def expand(self, path: str) -> str:
        """``path`` with a leading ``~`` or ``~user`` expanded."""
        return str(Path(path).expanduser()) if path.startswith("~") else path

    def scan(self, path: str, limit: int) -> Scan:
        """Up to ``limit`` entries of ``path``; a link counts as a directory when it leads to one."""
        entries: list[ScannedEntry] = []
        with os.scandir(path) as listing:
            for entry in listing:
                if len(entries) >= limit:
                    return Scan(entries=tuple(entries), complete=False)
                entries.append(ScannedEntry(entry.name, _is_directory(entry)))
        return Scan(entries=tuple(entries), complete=True)

    def hints(self, path: str) -> PathHints:
        """What ``path`` holds, each hint False when it cannot be read."""
        directory = Path(path)
        return PathHints(
            vibesys_checkout=_declares_vibesys(directory / "pyproject.toml"),
            vibesys_project=_defines_tasks(directory),
            git_repository=_exists(directory / ".git"),
        )


def _is_directory(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _defines_tasks(directory: Path) -> bool:
    try:
        return Project.open(directory).is_initialized()
    except (ProjectError, OSError):
        return False


def _exists(path: Path) -> bool:
    try:
        return path.exists(follow_symlinks=False)
    except OSError:
        return False


def _declares_vibesys(pyproject: Path) -> bool:
    return vibesys_version(pyproject) is not None


class SubprocessRunLauncher:
    """Starts a detached run with the command line ``vibesys --detach`` runs.

    ``command`` maps ``vibesys`` arguments to that command line (the launcher's
    ``headless_command``), so a run started here and one started at a terminal run
    the same code under the same interpreter.
    """

    def __init__(
        self, processes: ProcessLauncher, command: Callable[[Sequence[str]], list[str]]
    ) -> None:
        """Start processes through ``processes``; build each command line with ``command``."""
        self._processes = processes
        self._command = command

    def start(
        self, project: Path, arguments: tuple[str, ...]
    ) -> LiveInstanceRecord | DetachedLaunchFailure:
        """Run the launch in ``project`` and read the one line it prints."""
        try:
            return asyncio.run(self._start(project, arguments))
        except OSError as error:
            return _launch_failure(f"could not start the launch: {error}", 1)

    async def _start(
        self, project: Path, arguments: tuple[str, ...]
    ) -> LiveInstanceRecord | DetachedLaunchFailure:
        process = await self._processes.start(
            ProcessSpec(argv=tuple(self._command(arguments)), cwd=project)
        )
        outcome = await process.wait()
        lines = outcome.stdout.decode("utf-8", errors="replace").strip().splitlines()
        last = lines[-1] if lines else ""
        for model in (LiveInstanceRecord, DetachedLaunchFailure):
            decoded = decode_json(
                last, model, source="launch output", max_bytes=_LAUNCH_RECORD_LIMIT
            )
            if not isinstance(decoded, Unreadable):
                return decoded
        tail = outcome.stderr.decode("utf-8", errors="replace").strip()[-_LAUNCH_OUTPUT_TAIL:]
        summary = f"the launch exited with status {outcome.returncode} and printed no record"
        return _launch_failure(f"{summary}: {tail}" if tail else summary, outcome.returncode)


class SubprocessRunPreflight:
    """Runs ``vibesys preflight`` with the run's arguments, as ``run.start`` would launch them.

    ``command`` is the launcher's ``headless_command``, so the preflight runs the same code,
    under the same interpreter and in the same directory, as the run it checks.
    """

    def __init__(
        self, processes: ProcessLauncher, command: Callable[[Sequence[str]], list[str]]
    ) -> None:
        """Start processes through ``processes``; build each command line with ``command``."""
        self._processes = processes
        self._command = command

    def report(self, project: Path, arguments: tuple[str, ...]) -> PreflightReport:
        """The report ``vibesys preflight ARGUMENTS`` prints in ``project``."""
        try:
            return asyncio.run(self._report(project, arguments))
        except OSError as error:
            return preflight_unavailable(f"could not start the preflight: {error}")

    async def _report(self, project: Path, arguments: tuple[str, ...]) -> PreflightReport:
        process = await self._processes.start(
            ProcessSpec(argv=tuple(self._command(("preflight", *arguments))), cwd=project)
        )
        outcome = await process.wait()
        lines = outcome.stdout.decode("utf-8", errors="replace").strip().splitlines()
        decoded = decode_json(
            lines[-1] if lines else "",
            PreflightReport,
            source="preflight output",
            max_bytes=_LAUNCH_RECORD_LIMIT,
        )
        if not isinstance(decoded, Unreadable):
            return decoded
        tail = outcome.stderr.decode("utf-8", errors="replace").strip()[-_LAUNCH_OUTPUT_TAIL:]
        summary = f"the preflight exited with status {outcome.returncode} and printed no report"
        return preflight_unavailable(f"{summary}: {tail}" if tail else summary)


def _launch_failure(message: str, status: int) -> DetachedLaunchFailure:
    return DetachedLaunchFailure(
        code=DetachedLaunchErrorCode.SERVER_START_FAILED,
        stage="server_start",
        message=message,
        exit_code=status if status > 0 else 1,
    )


class GitCheckoutProbe:
    """Describes a source checkout with ``git``; ``root`` is ``None`` for an installed distribution."""

    def __init__(self, root: Path | None, probe: CommandProbe) -> None:
        """Describe ``root`` by running ``git`` through ``probe``."""
        self._root = root
        self._probe = probe

    def describe(self) -> Checkout | None:
        """The checkout's root, ``HEAD`` commit and whether tracked files changed."""
        if self._root is None:
            return None
        root = str(self._root)
        commit, dirty = git_state(self._probe, root)
        return Checkout(root=root, commit=commit, dirty=dirty)


_OWNER: TypeAdapter[LogOwner] = TypeAdapter(LogOwner)
"""Owners pass through as JSON: the writer validated them, and the helper never imports vibesys."""


def run_streams_directory(location: RunLocation) -> Path:
    """Where ``location``'s run keeps its log streams, as ``vs_project`` lays it out."""
    return Project.log_streams_directory_for(location.project_root, location.run_id)


class StoreLogReader:
    """The helper's ``LogReader`` over each run's ``vs_logs`` store on this host.

    ``locate`` names the run an instance drives or drove (``None`` when the host knows
    nothing of it); ``directory_for`` names that run's store. Each request opens the
    store afresh (``instance``), so the helper keeps no state between requests.
    ``vs_logs.read`` is imported on the first logs request, so a channel that never asks
    for logs, and every ``hello``, loads none of it (``tests/e2e/test_host_helper.py`` holds the ceiling).
    """

    def __init__(
        self,
        locate: Callable[[str], RunLocation | None],
        directory_for: Callable[[RunLocation], Path],
    ) -> None:
        """Read the store ``directory_for`` names for the run ``locate`` finds."""
        self._locate = locate
        self._directory_for = directory_for

    def instance(self, instance_id: str) -> _StoreInstanceLogs:
        """The instance's run's store, opened; ``UnknownInstanceError`` for an unknown run."""
        location = self._locate(instance_id)
        if location is None:
            raise UnknownInstanceError(instance_id)
        # lint-waiver: LW-640282 [PLC0415]; logs cost the helper nothing until a client reads one.
        # > The same deferral as in `read`: `hello` must not load vs_logs.read, and an import
        # > tach can see keeps its visibility rule enforced.
        from vs_logs.read import CorruptLogError, open_log_directory  # noqa: PLC0415

        reader = open_log_directory(self._directory_for(location), _OWNER)
        return _StoreInstanceLogs(reader, CorruptLogError)


class _StoreInstanceLogs:
    """One run's ``vs_logs`` store, opened: each read sees what was written since.

    An index that does not decode (``corrupt``, ``vs_logs.read.CorruptLogError`` passed in
    so this module still loads none of ``vs_logs``) is ``LogStoreCorruptError``.
    """

    def __init__(self, reader: StoreReader[LogOwner], corrupt: type[Exception]) -> None:
        self._reader = reader
        self._corrupt = corrupt

    def _using[T](self, use: Callable[[StoreReader[LogOwner]], T]) -> T:
        try:
            return use(self._reader)
        except self._corrupt as error:
            raise LogStoreCorruptError(str(error)) from error

    def streams(self) -> tuple[LogStream, ...]:
        """The instance's streams, oldest first; none when its run recorded no logs."""
        return tuple(
            LogStream(
                id=info.stream_id,
                owner=info.owner,
                command=info.command,
                retained_start=info.retained_start,
                size=info.size,
                outcome=None if info.outcome is None else LogStreamOutcome(info.outcome.value),
            )
            for info in self._using(lambda reader: reader.list_streams())
        )

    def read(self, stream_id: str, start: LogOffset | LogTail, max_bytes: int) -> StoredBytes:
        """At most ``max_bytes`` of the stream from ``start``."""
        # lint-waiver: LW-640281 [PLC0415]; logs cost the helper nothing until a client reads one.
        # > At module level every channel would load vs_logs.read before `hello`, which the
        # > helper's start-up ceiling forbids; a lazy facade (PEP 562) in vs_logs would hide
        # > the import from tach, which keeps vs_logs.read visible to the host helper only.
        from vs_logs.read import FromOffset, Tail, UnknownStreamError  # noqa: PLC0415

        origin = (
            FromOffset(start.offset) if isinstance(start, LogOffset) else Tail(start.tail_bytes)
        )
        try:
            chunk = self._using(lambda reader: reader.read(stream_id, origin, max_bytes))
        except UnknownStreamError as error:
            raise UnknownLogStreamError(stream_id) from error
        return StoredBytes(
            offset=chunk.offset,
            data=chunk.data,
            pipes=tuple(
                LogPipeSpan(offset=span.offset, length=span.length, pipe=LogPipe(span.pipe.value))
                for span in chunk.pipes
                if span.length > 0
            ),
            truncated_before=chunk.truncated_before,
            eof=chunk.eof,
            closed=chunk.closed,
        )


# --- starting a harness's own login ------------------------------------------------------
#
# `LoginStarter.start` runs the login command the agent library names (agentshim's facts, the
# choice of variant in `vs_agent`) as a child that outlives the request: a device-code login
# prints a page to open and a code to enter, then waits for the person to finish. The starter
# reads the output until it has that instruction, answers, and leaves the child running; a
# worker waits for the child so it is reaped, and ends it if the person never finishes.

_LOGIN_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_LOGIN_URL = re.compile(r"https?://[^\s\x1b\"'<>)]+")
_LOGIN_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,8}\b")
_LOGIN_LINE_MAX = 300


def login_instruction(url: str | None, code: str | None) -> str | None:
    """What the person is asked to do, in one line, or ``None`` when nothing was shown.

    A page without a code is just the page. An instruction that would not fit a wire line is
    dropped rather than cut: half a URL is worse than none.
    """
    if url is None:
        return None
    line = f"Open {url} and enter the code {code}" if code is not None else f"Open {url}"
    return line if len(line) <= _LOGIN_LINE_MAX else None


class _Output:
    """What a login's output has shown so far; the reader writes, ``start`` waits on it."""

    def __init__(self, threads: Threads) -> None:
        self.condition = threads.condition()
        self.url: str | None = None
        self.code: str | None = None
        self.first_line = ""
        self.ended = False
        self.lines = 0

    def take(self, line: str) -> None:
        text = _LOGIN_ANSI.sub("", line).strip()
        with self.condition:
            self.lines += 1
            if text and not self.first_line:
                self.first_line = text[:_LOGIN_LINE_MAX]
            if self.url is None and (found := _LOGIN_URL.search(text)) is not None:
                self.url = found.group(0)
            if self.code is None and (found := _LOGIN_CODE.search(text)) is not None:
                self.code = found.group(0)
            self.condition.notify_all()

    def end(self) -> None:
        with self.condition:
            self.ended = True
            self.condition.notify_all()


@dataclass(frozen=True, slots=True)
class LoginBounds:
    """How long a login is waited for, in seconds of ``Threads``' clock."""

    page_wait: float = 10.0
    """The wait for the first page the login shows."""
    code_wait: float = 1.0
    """The further wait for a code once a page is shown."""
    lifetime: float = 900.0
    """How long a login may stay open before it is ended."""


class LoginStarter:
    """Runs a login command and reports what it asked the person to do."""

    def __init__(
        self,
        processes: PipedProcessLauncher,
        threads: Threads,
        environment: Callable[[], Mapping[str, str]],
        bounds: LoginBounds,
    ) -> None:
        """Start commands through ``processes``, in the environment ``environment()`` gives them.

        The environment is read when a login starts, not here: it is the login shell's, which a
        helper that never starts a login has no reason to load. ``threads`` is the clock waited on.
        """
        self._processes = processes
        self._threads = threads
        self._environment = environment
        self._bounds = bounds

    def start(self, argv: tuple[str, ...]) -> LoginStarted | LoginUnavailable:
        """Run ``argv`` and answer once it has shown its page and code, ended, or run out of time."""
        try:
            process = self._processes.start(argv, env=self._environment(), merge_stderr=True)
        except OSError as error:
            return _failed(f"could not run {argv[0]}: {error.strerror or error}")
        output = _Output(self._threads)
        self._threads.spawn(lambda: _read(process, output), name="login-output")
        self._threads.spawn(lambda: _reap(process, self._bounds.lifetime), name="login-reaper")
        with output.condition:
            output.condition.wait_for(
                lambda: output.url is not None or output.ended, self._bounds.page_wait
            )
            if output.url is not None:
                output.condition.wait_for(
                    lambda: output.code is not None or output.ended, self._bounds.code_wait
                )
            instruction = login_instruction(output.url, output.code)
            if instruction is None and output.url is None and output.ended and output.first_line:
                return _failed(output.first_line)
        return LoginStarted(instruction=instruction)


def _failed(detail: str) -> LoginUnavailable:
    return LoginUnavailable(
        reason=LoginUnavailableReason.START_FAILED, detail=detail.splitlines()[0][:_LOGIN_LINE_MAX]
    )


def _read(process: PipedProcess, output: _Output) -> None:
    """Feed the child's output lines to ``output`` until its pipe closes."""
    try:
        while line := process.stdout.readline():
            output.take(line)
    finally:
        output.end()


def _reap(process: PipedProcess, lifetime: float) -> None:
    """Wait for the child so it is reaped, and end its group if it outlives ``lifetime``."""
    try:
        process.wait(lifetime)
    except ProcessTimeoutError:
        process.terminate_group()
        try:
            process.wait(5.0)
        except ProcessTimeoutError:
            process.kill_group()
            process.wait(5.0)
