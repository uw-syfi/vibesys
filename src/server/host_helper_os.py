"""The host helper's interfaces on a real host: projects, directories, launches, the checkout.

Each class implements one interface of ``server.host_helper``; the helper's entrypoint
wires them, and ``vibesys tasks`` reads tasks through ``FileProjects``. Processes start
through ``vs_sim.api``'s launchers, so this module names no interpreter of its own: a
launch runs the command line the ``vibesys`` launcher would. Like the helper, the module
is light (``tests/architecture/test_light_entrypoints.py``).

Public surface: ``FileProjects``, ``OsDirectories``, ``SubprocessRunLauncher``,
``GitCheckoutProbe``, and ``StoreLogReader`` with ``run_streams_directory``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

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
    TaskList,
    TaskSummary,
)
from server.host_helper_logs import (
    LogStoreCorruptError,
    RunLocation,
    StoredBytes,
    UnknownInstanceError,
    UnknownLogStreamError,
)
from server.host_helper_machine import git_state, vibesys_version
from server.host_helper_requests import (
    EventPage,
    PathHints,
    ProjectLookupError,
    ProjectRun,
    Scan,
    ScannedEntry,
    event_page,
)
from vs_decode.api import Unreadable, decode_json
from vs_project.api import Project, ProjectError
from vs_sim.api import ProcessSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_logs.read import LogReader as StoreReader
    from vs_sim.api import CommandProbe, ProcessLauncher

_PYPROJECT_LIMIT_BYTES = 1 << 20
_LAUNCH_OUTPUT_TAIL = 2000
_LAUNCH_RECORD_LIMIT = 1 << 20


class FileProjects:
    """Projects read through ``vs_project``'s ``Project`` on this filesystem."""

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

    def runs(self, project: Path) -> tuple[Path, tuple[ProjectRun, ...]]:
        """The project's root and its runs, oldest first; runs this VibeSys cannot read are left out."""
        opened = self._open(project)
        try:
            manifests = opened.state.list_runs()
        except ProjectError as exc:
            raise ProjectLookupError(
                HelperErrorCode.TASK_INVALID, str(opened.root), str(exc)
            ) from exc
        return opened.root, tuple(
            ProjectRun(
                run_id=manifest.run_id,
                task=manifest.task_name,
                display_name=manifest.display_name,
                created_at=manifest.created_at,
            )
            for manifest in manifests
        )

    def require_initialized(self, project: Path) -> Path:
        """The project's root, once it is known to define tasks."""
        return self._initialized(project).root

    def run_events(self, project: Path, run_id: str, from_sequence: int, limit: int) -> EventPage:
        """A page of run ``run_id``'s journal, once the project is known to have the run."""
        root, runs = self.runs(project)
        if all(run.run_id != run_id for run in runs):
            message = f"{root} has no run {run_id!r}"
            raise ProjectLookupError(HelperErrorCode.UNKNOWN_RUN, str(root), message)
        journal = Project.log_directory_for(root, run_id) / RUN_EVENTS_FILE
        try:
            with journal.open("rb") as lines:
                return event_page(lines, from_sequence, limit, RUN_EVENTS_READ_MAX_BYTES)
        except FileNotFoundError:
            return EventPage((), from_sequence, complete=True)

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
