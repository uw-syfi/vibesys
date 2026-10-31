"""Reading a host's usage from disk: the journals behind the rollups, and the byte budget.

The rollup (``server.usage_rollup``) and the summary (``server.usage_summary``) are pure. This
module is where a read touches files, and so where its cost is bounded: one ``Budget`` of bytes
is shared by every fold a read does, so a host with years of runs answers in calls of bounded
size and reports what is left (``pending``). The bound is in bytes, never in time, so the same
disk gives the same answer on any machine.

``JsonlUsageJournal`` is a run's JSONL journal as ``UsageJournal``: its identity is the summary
cache's fingerprint of the journal's complete lines (``server.run_summary_cache``), and a fold is
the summary reader's, so a run the summary already covers costs a cache read and what the journal
gained since. ``FileRollupStore`` keeps ``run-usage.pb`` beside the journal, written atomically.
A file that does not decode, or fails its rules, is no rollup: it is rebuilt.

``collect_runs`` reads the rollup of every run of the given projects that can hold a turn in the
window, newest journal first. The runs are listed from the host's log store, which asks git
nothing, and a journal not written since the window began cannot hold a turn in it, so it is
skipped by its modification time without being opened.

Light like the helper: the standard library, ``vs_project.api`` and the light ``server`` modules.

The price list and the person's prices live beside them in the host's usage state directory
(``Project.usage_state_directory``): ``rates.pb`` is the last document fetched and ``prices.pb``
the only copy of what a person set. The host fetches only in ``refresh_rates``, never in a read;
``stored_pricing`` prices from what is on disk.

Public surface: ``JsonlUsageJournal``, ``FileRollupStore``, ``UsageProjects``, ``CollectedRuns``,
``collect_runs``, ``usage_summary``, ``source_identity``, ``usage_directory``, ``COLD_BUDGET_BYTES``,
``ProtoRateStore``, ``FilePriceBook``, ``stored_pricing``, ``refresh_rates``, ``save_prices`` and
``RATES_REFRESH_TIMEOUT_S``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

from server.host_api import RUN_EVENTS_FILE
from server.run_digest import journal_kept_landings
from server.run_summary_cache import journal_fingerprint
from server.usage_outside import Outside, RunFacts, exclude
from server.usage_pricing import (
    DocumentFetcher,
    FetchError,
    ListPricing,
    PriceBookUnreadableError,
    RateDocument,
    check_book,
    read_rate_book,
    refresh_rate_book,
    same_book,
    saved_book,
)
from server.usage_rollup import (
    ROLLUP_FILE,
    Budget,
    FoldedJournal,
    JournalIdentity,
    Outcome,
    read_or_build,
)
from server.usage_summary import OutsideInput, RunInput, SummaryInputs, summarize
from server.usage_transcripts import RootReport, scan_transcripts
from vs_project.api import Project, ProjectError, atomic_write_bytes
from vs_proto.api import Timestamp, ValidationError, host_payloads, usage_state, validate
from vs_sim.api import HttpsGetError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    from server.run_summary import Summarized
    from server.usage_summary import Pricing
    from vs_sim.api import Clock, HttpsGet

COLD_BUDGET_BYTES: Final = 256 * 1024 * 1024
"""The journal bytes one read may fold from nothing. Folding runs at tens to hundreds of
megabytes a second, so a call stays inside the operation's deadline, and a host with more asks
again (``pending``)."""

RATES_REFRESH_TIMEOUT_S: Final = 10.0
"""The longest the fetch of the price list waits on the connection at a time; the gateway's own
deadline bounds the whole call."""
RATES_MAX_BYTES: Final = 16 * 1024 * 1024
"""The most bytes the price list may be: it is about 3 MiB today."""

_ROOT_MAX_BYTES: Final = 512
_NO_DIGEST = "0" * 64
_SOURCE_ID_FILE = "source-id"
_RATES_FILE = "rates.pb"
_PRICES_FILE = "prices.pb"


class UsageProjects(Protocol):
    """What reading usage needs of the host's projects: the fold of a run's journal."""

    def run_summary(self, project: Path, run_id: str) -> Summarized:
        """The fold of the run's journal as it is now."""
        ...


class JsonlUsageJournal:
    """One run's JSONL journal, as the rollup reads it."""

    def __init__(self, journal: Path, *, summarize: Callable[[], Summarized]) -> None:
        """Read ``journal``; ``summarize`` folds it."""
        self._journal = journal
        self._summarize = summarize

    def identity(self) -> JournalIdentity | None:
        """The fingerprint of the journal's complete lines; ``None`` when it cannot be read."""
        found = journal_fingerprint(self._journal)
        if found is None:
            return None
        return JournalIdentity(found.boundary, found.head_sha256, found.tail_sha256)

    def cold_bytes(self) -> int:
        """The journal's size, which a fold from nothing reads at most."""
        try:
            return self._journal.stat().st_size
        except OSError:
            return 0

    def fold(self) -> FoldedJournal:
        """The summary fold, when its kept ideas landed, the prefix it covers, and whether that prefix ends a line.

        The landings are read from the same prefix the fold covers, so a rollup describes one
        state of the journal and a line appended while it was built waits for the next build.
        Raises ``OSError`` when the journal cannot be read.
        """
        summarized = self._summarize()
        covered = summarized.journal_bytes
        found = journal_fingerprint(self._journal, covered)
        identity = (
            JournalIdentity(covered, _NO_DIGEST, _NO_DIGEST)
            if found is None
            else JournalIdentity(found.boundary, found.head_sha256, found.tail_sha256)
        )
        return FoldedJournal(
            state=summarized.fold.state(),
            bests=journal_kept_landings(_prefix(self._journal, covered)),
            identity=identity,
            persistable=found is not None,
        )


def _prefix(journal: Path, size: int) -> Iterator[bytes]:
    """The lines of the first ``size`` bytes of ``journal``."""
    read = 0
    with journal.open("rb") as stream:
        for line in stream:
            if read >= size:
                return
            read += len(line)
            yield line


class FileRollupStore:
    """A run's rollup in ``run-usage.pb`` beside its journal."""

    def __init__(self, path: Path) -> None:
        """Keep the rollup at ``path``."""
        self._path = path

    def read(self) -> usage_state.RunUsageRollup | None:
        """The stored rollup, or ``None`` when there is none or it is damaged."""
        try:
            rollup = usage_state.RunUsageRollup.from_binary(self._path.read_bytes())
            validate(rollup)
        except (OSError, ValueError, ValidationError):
            return None
        return rollup

    def write(self, rollup: usage_state.RunUsageRollup) -> None:
        """Replace the stored rollup. A write that fails costs only the next read's time.

        A rollup that breaks its own rules is not written: the next read would not take it back.
        """
        try:
            validate(rollup)
            atomic_write_bytes(self._path, rollup.to_binary())
        except (OSError, ProjectError, ValidationError):
            return


def source_identity(directory: Path, new_id: Callable[[], str] = lambda: uuid.uuid4().hex) -> str:
    """The random identity of the usage state in ``directory``, made on first use.

    Two profiles over one state home, or two machines on one shared file system, read the same
    file and report the same identity, so a client counts their usage once. A directory that
    cannot be written gives an identity that lasts only until the next read: the usage it
    reports is counted per read, which is the safe way to be wrong.
    """
    path = directory / _SOURCE_ID_FILE
    try:
        found = usage_state.UsageSourceIdentity.from_binary(path.read_bytes())
        validate(found)
    except (OSError, ValueError, ValidationError):
        pass
    else:
        return found.id
    made = usage_state.UsageSourceIdentity(id=new_id())
    validate(made)
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_bytes(path, made.to_binary())
    except (OSError, ProjectError):
        return made.id
    return made.id


@dataclass(frozen=True)
class CollectedRuns:
    """The rollups of the runs a read could place, and what it left."""

    runs: tuple[RunInput, ...]
    pending: int
    """Runs whose journals the budget could not pay for; their stale rollups, if any, are in ``runs``."""
    scanned: int
    """Runs whose journals were read or served from a rollup."""
    skipped: int
    """Runs not written since the window began, or whose journal this host does not hold."""
    failed: int
    """Runs whose journal could not be read."""


def collect_runs(
    projects: UsageProjects,
    paths: Sequence[Path],
    *,
    since: datetime,
    budget: Budget,
) -> CollectedRuns:
    """The rollup of each run of ``paths`` (one per requested project, in request order).

    The runs of a project are the ones this host holds logs for, listed from the log store: the
    answer is a sum over them, so it visits each, and it asks git nothing. A run is read when its
    journal was written at or after ``since``; the newest journals come first, so the part of the
    window people look at settles first.
    """
    collected: list[RunInput] = []
    pending = scanned = skipped = failed = 0
    for index, path in enumerate(paths):
        root = path.expanduser().resolve()
        held = _written_since(root, since)
        skipped += held.skipped
        failed += held.failed
        for _, run_id, journal in sorted(held.journals, reverse=True):
            try:
                reading = read_or_build(
                    JsonlUsageJournal(
                        journal,
                        summarize=lambda run_id=run_id, path=path: projects.run_summary(
                            path, run_id
                        ),
                    ),
                    FileRollupStore(journal.with_name(ROLLUP_FILE)),
                    budget,
                )
            except OSError:
                failed += 1
                continue
            if reading.outcome is Outcome.MISSING:
                failed += 1
                continue
            scanned += 1
            pending += reading.outcome is Outcome.PENDING
            if reading.rollup is not None:
                collected.append(RunInput(index, run_id, reading.rollup))
    return CollectedRuns(tuple(collected), pending, scanned, skipped, failed)


@dataclass(frozen=True)
class _Held:
    journals: tuple[tuple[float, str, Path], ...]
    """Each journal written since the window began, with when and the run it belongs to."""
    skipped: int
    failed: int


def _written_since(root: Path, since: datetime) -> _Held:
    """The journals of the runs ``root`` holds logs for that were written at or after ``since``."""
    try:
        run_ids = Project.logged_run_ids_for(root)
    except ProjectError:
        return _Held((), 0, 1)
    journals: list[tuple[float, str, Path]] = []
    skipped = failed = 0
    for run_id in run_ids:
        journal = Project.log_directory_for(root, run_id) / RUN_EVENTS_FILE
        try:
            written = journal.stat().st_mtime
        except FileNotFoundError:
            skipped += 1
            continue
        except OSError:
            failed += 1
            continue
        if written < since.timestamp():
            skipped += 1
        else:
            journals.append((written, run_id, journal))
    return _Held(tuple(journals), skipped, failed)


def usage_summary(  # noqa: PLR0913  # lint-waiver: LW-518204 [PLR0913]; one read's independent inputs: the request, the host's projects, the directories, the prices and the environment the provider CLIs keep their sessions by.
    request: host_payloads.UsageSummaryRequest,
    projects: UsageProjects,
    paths: Sequence[Path],
    pricing: Pricing,
    environment: Mapping[str, str],
    *,
    clock: Clock,
    budget_bytes: int = COLD_BUDGET_BYTES,
) -> host_payloads.UsageSummaryResponse:
    """The reply to ``request`` over the runs of ``paths`` (the request's projects, expanded).

    One byte budget pays for the journals first, then the transcripts, so a cold host answers in
    calls of bounded size and says what is left. A person's own CLI use is the transcript
    sessions that no run of this host made (``server.usage_outside``); it is left partial while
    any run of the host is still waiting, since that run might yet claim a session.

    ``clock`` is the host's own: what the transcript cache forgets for age is decided by it, never
    by the request's ``now``, so a client with a wrong clock cannot erase history the host cannot
    read back.

    Raises ``ValueError`` when the request's edges do not increase, which the read maps to invalid
    input.
    """
    if request.now is None:
        message = "now: required"
        raise ValueError(message)
    edges = [stamp.to_datetime() for stamp in request.edges]
    now = request.now.to_datetime()
    budget = Budget(budget_bytes)
    collected = collect_runs(projects, paths, since=edges[0], budget=budget)
    host = collect_host_runs(projects, collected, since=edges[0], budget=budget)
    if not paths:
        # No project was named: the runs are every run the host indexed, read for exclusion.
        collected = CollectedRuns(
            host.indexed, host.pending, host.scanned, host.skipped, host.failed
        )
    outside, pending_files = _outside(
        request,
        host,
        environment,
        retained_at=datetime.fromtimestamp(clock.now(), UTC),
        since=edges[0],
        budget=budget,
    )
    return summarize(
        SummaryInputs(
            request_id=request.request_id,
            edges=edges,
            now=now,
            runs=collected.runs,
            journal=_journal_source(collected),
            pricing=pricing,
            pending_runs=collected.pending + (host.pending if paths else 0),
            pending_files=pending_files,
            outside=outside,
        )
    )


@dataclass(frozen=True)
class HostRuns:
    """What the host's other runs say, for telling a run's sessions from a person's."""

    facts: tuple[RunFacts, ...]
    pending: int
    """Runs whose rollups the budget could not pay for."""
    indexed: tuple[RunInput, ...] = ()
    """The rollups of the indexed runs, for a request that named no project (index 0)."""
    scanned: int = 0
    skipped: int = 0
    failed: int = 0


def collect_host_runs(
    projects: UsageProjects, requested: CollectedRuns, *, since: datetime, budget: Budget
) -> HostRuns:
    """The facts of every run this host published for discovery, and of the ``requested`` ones.

    A transcript session may belong to a run of any project on the host, not only those asked
    for, so exclusion reads every run: each costs its rollup, built once and then served.
    """
    facts: dict[str, RunFacts] = {
        each.run_id: _facts(each.run_id, None, each.rollup) for each in requested.runs
    }
    indexed: list[RunInput] = []
    pending = scanned = skipped = failed = 0
    for run in Project.indexed_run_addresses():
        journal = run.log_directory / RUN_EVENTS_FILE
        try:
            if journal.stat().st_mtime < since.timestamp():
                skipped += 1
                continue
        except OSError:
            skipped += 1
            continue
        try:
            reading = read_or_build(
                JsonlUsageJournal(
                    journal,
                    summarize=lambda run=run: projects.run_summary(run.project_root, run.run_id),
                ),
                FileRollupStore(journal.with_name(ROLLUP_FILE)),
                budget,
            )
        except OSError:
            failed += 1
            continue
        if reading.outcome is Outcome.MISSING:
            failed += 1
            continue
        scanned += 1
        pending += reading.outcome is Outcome.PENDING
        if reading.rollup is not None:
            facts[run.run_id] = _facts(run.run_id, str(run.project_root), reading.rollup)
            indexed.append(RunInput(0, run.run_id, reading.rollup))
    return HostRuns(tuple(facts.values()), pending, tuple(indexed), scanned, skipped, failed)


def _facts(run_id: str, project_root: str | None, rollup: usage_state.RunUsageRollup) -> RunFacts:
    return RunFacts(
        run_id=run_id,
        project_root=project_root,
        providers=frozenset(row.provider for row in rollup.rows),
        session_ids=frozenset(rollup.provider_session_ids),
        session_ids_complete=rollup.session_ids_complete,
        first_turn_at=None if rollup.first_turn_at is None else rollup.first_turn_at.to_datetime(),
        last_turn_at=None if rollup.last_turn_at is None else rollup.last_turn_at.to_datetime(),
    )


def _outside(  # noqa: PLR0913  # lint-waiver: LW-518205 [PLR0913]; the stages of one read, each an independent input.
    request: host_payloads.UsageSummaryRequest,
    host: HostRuns,
    environment: Mapping[str, str],
    *,
    retained_at: datetime,
    since: datetime,
    budget: Budget,
) -> tuple[list[OutsideInput], int]:
    """The transcript sources with the runs' own sessions left out, and the files still pending.

    ``retained_at`` is the host's time: rows older than the retention before it are forgotten.
    """
    directory = usage_directory()
    scanned = scan_transcripts(
        directory, environment, now=retained_at, since=since, budget=max(1, budget.remaining)
    )
    window = (int(since.timestamp()), int(request.edges[-1].seconds))
    left = exclude(scanned.rows, host.facts, Project.run_id_for_workspace, counted=window)
    source_id = source_identity(directory)
    partial = host.pending > 0
    sources = [
        OutsideInput(
            source=_transcript_source(report, source_id, left, partial=partial),
            rows=tuple(found for found in left.kept if found.provider == report.provider),
        )
        for report in scanned.reports
    ]
    return sources, scanned.pending_files


def _transcript_source(
    report: RootReport, source_id: str, left: Outside, *, partial: bool
) -> host_payloads.UsageSource:
    """How completely one provider directory was read, and what was left out of it."""
    status = report.status
    if partial and status is usage_state.UsageSourceStatus.OK:
        status = usage_state.UsageSourceStatus.PARTIAL
    return host_payloads.UsageSource(
        kind=usage_state.UsageSourceKind.TRANSCRIPTS,
        source_id=source_id,
        provider=report.provider,
        root=report.root.encode()[:_ROOT_MAX_BYTES].decode(errors="ignore"),
        status=status,
        scanned=report.scanned,
        skipped=report.skipped,
        malformed=report.malformed,
        failed=report.failed,
        excluded_exact=len(left.excluded_sessions.get(report.provider, ())),
        excluded_approximate=left.excluded_turns.get(report.provider, 0),
    )


def _journal_source(collected: CollectedRuns) -> host_payloads.UsageSource:
    """How completely the journals were read: partial while any run waits or failed."""
    if collected.pending or collected.failed:
        status = usage_state.UsageSourceStatus.PARTIAL
    elif collected.scanned == 0 and collected.skipped == 0:
        status = usage_state.UsageSourceStatus.MISSING
    else:
        status = usage_state.UsageSourceStatus.OK
    return host_payloads.UsageSource(
        kind=usage_state.UsageSourceKind.JOURNAL,
        source_id=source_identity(usage_directory()),
        status=status,
        scanned=collected.scanned,
        skipped=collected.skipped,
        failed=collected.failed,
    )


class ProtoRateStore:
    """The last price document in ``rates.pb``: a cache of the network, rebuilt when it is damaged."""

    def __init__(self, path: Path) -> None:
        """Keep the document at ``path``."""
        self._path = path

    def read(self) -> RateDocument | None:
        """The stored document; ``None`` when there is none or the file fails its rules."""
        try:
            found = usage_state.UsageRateSnapshot.from_binary(self._path.read_bytes())
            validate(found)
        except (OSError, ValueError, ValidationError):
            return None
        return RateDocument(
            found.document if found.has_field("document") else None,
            None if found.fetched_at is None else found.fetched_at.to_datetime(),
            None if found.last_attempt_at is None else found.last_attempt_at.to_datetime(),
        )

    def write(self, stored: RateDocument) -> None:
        """Replace the document; a write that fails loses only the next read's rates."""
        snapshot = usage_state.UsageRateSnapshot()
        if stored.document is not None:
            snapshot.document = stored.document
        if stored.fetched_at is not None:
            snapshot.fetched_at = Timestamp.from_datetime(stored.fetched_at)
        if stored.last_attempt_at is not None:
            snapshot.last_attempt_at = Timestamp.from_datetime(stored.last_attempt_at)
        _write(self._path, snapshot.to_binary())


class FilePriceBook:
    """What a person set, in ``prices.pb``: the only copy, so damage is reported and never overwritten."""

    def __init__(self, path: Path) -> None:
        """Keep the book at ``path``."""
        self._path = path

    def read(self) -> usage_state.UsagePriceBook:
        """The stored book: an empty one at revision 0 when there is no file.

        Raises ``PriceBookUnreadableError`` when there is a file and it cannot be used: it does
        not read, does not decode, fails its rules, or has a ``Map to`` chain that loops. The
        file is the person's only copy, so a damaged one is never taken for an empty book.
        """
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return usage_state.UsagePriceBook()
        except OSError as error:
            message = f"{_PRICES_FILE} cannot be read: {error.strerror or 'an I/O error'}"
            raise PriceBookUnreadableError(message) from error
        try:
            book = usage_state.UsagePriceBook.from_binary(raw)
            validate(book)
            check_book(book.overrides, book.aliases)
        except (ValueError, ValidationError) as error:
            message = f"{_PRICES_FILE} cannot be used: {str(error).splitlines()[0][:200]}"
            raise PriceBookUnreadableError(message) from error
        return book

    def write(self, book: usage_state.UsagePriceBook) -> None:
        """Replace the book. Raises ``OSError`` when it cannot be written."""
        validate(book)
        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_bytes(self._path, book.to_binary())
        except ProjectError as error:
            raise OSError(str(error)) from error


class HttpsFetcher:
    """The price list's fetcher over the host's HTTPS client."""

    def __init__(self, https: HttpsGet) -> None:
        """Fetch with ``https``, bounded in time and size."""
        self._https = https

    def fetch(self, url: str, accept: str) -> bytes:
        """The body at ``url``; ``FetchError`` when it cannot be had whole."""
        try:
            return self._https.get(
                url, accept=accept, timeout=RATES_REFRESH_TIMEOUT_S, max_bytes=RATES_MAX_BYTES
            )
        except HttpsGetError as error:
            raise FetchError(str(error)) from error


def usage_directory() -> Path:
    """The host's directory for the usage state: the price list, the person's prices, the source id."""
    return Project.usage_state_directory()


def stored_pricing(directory: Path, *, now: datetime) -> ListPricing:
    """The prices of the list on disk (or the pinned table) and the person's book; no network.

    A book that exists and cannot be used does not stop the read: estimates are made without it
    and the pricing says so (``prices_status``), so the person is told and nothing is saved over
    their only copy.
    """
    rates = read_rate_book(ProtoRateStore(directory / _RATES_FILE), now=now)
    try:
        book = FilePriceBook(directory / _PRICES_FILE).read()
    except PriceBookUnreadableError:
        return ListPricing(
            rates, usage_state.UsagePriceBook(), status=usage_state.UsagePricesStatus.UNREADABLE
        )
    return ListPricing(rates, book)


def refresh_rates(
    directory: Path, fetcher: DocumentFetcher, *, now: datetime, force: bool
) -> usage_state.UsagePricing:
    """Fetch the price list if one is due and say how current the rates are after.

    A failed fetch is not an error: the answer then says the last good document (or the pinned
    table) is in use, which is what a read will price with.
    """
    store = ProtoRateStore(directory / _RATES_FILE)
    book = refresh_rate_book(store, fetcher, now=now, force=force)
    return ListPricing(book, usage_state.UsagePriceBook()).pricing


def save_prices(
    directory: Path, request: host_payloads.UsagePricesSaveRequest
) -> host_payloads.UsagePricesSaveResponse:
    """Save the person's book if it was edited from the host's current revision.

    The book is replaced whole. A request equal to the host's book is already saved and changes
    nothing, so a retry after an unknown outcome is safe; one made against an older revision
    changes nothing and answers with the host's book, so concurrent edits cannot overwrite each
    other unseen. Raises ``PriceBookError`` (a ``ValueError``) naming the offending key when the
    book cannot be saved, and ``PriceBookUnreadableError`` when the stored book is damaged: it is
    never replaced without the person's say.
    """
    file = FilePriceBook(directory / _PRICES_FILE)
    current = file.read()
    if same_book(current, request.overrides, request.aliases):
        check_book(request.overrides, request.aliases)
        return host_payloads.UsagePricesSaveResponse(
            request_id=request.request_id, prices=current, saved=True
        )
    if request.base_revision != current.revision:
        return host_payloads.UsagePricesSaveResponse(
            request_id=request.request_id, prices=current, saved=False
        )
    book = saved_book(current, request.overrides, request.aliases)
    file.write(book)
    return host_payloads.UsagePricesSaveResponse(
        request_id=request.request_id, prices=book, saved=True
    )


def _write(path: Path, contents: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_bytes(path, contents)
    except (OSError, ProjectError):
        return
