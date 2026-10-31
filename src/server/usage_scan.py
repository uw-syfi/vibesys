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

Public surface: ``JsonlUsageJournal``, ``FileRollupStore``, ``UsageProjects``, ``CollectedRuns``,
``collect_runs``, ``usage_summary``, ``source_identity`` and ``COLD_BUDGET_BYTES``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from server.host_api import RUN_EVENTS_FILE, RunDigest
from server.host_run_dashboard import kept_landings
from server.run_summary_cache import journal_fingerprint
from server.usage_rollup import (
    ROLLUP_FILE,
    Budget,
    FoldedJournal,
    JournalIdentity,
    Outcome,
    read_or_build,
)
from server.usage_summary import RunInput, SummaryInputs, summarize
from vs_project.api import Project, ProjectError, atomic_write_bytes
from vs_proto.api import ValidationError, host_payloads, usage_state, validate

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from pathlib import Path

    from server.host_api import RunDigestUnreadable
    from server.run_summary import Summarized
    from server.usage_summary import Pricing

COLD_BUDGET_BYTES: Final = 256 * 1024 * 1024
"""The journal bytes one read may fold from nothing. Folding runs at tens to hundreds of
megabytes a second, so a call stays inside the operation's deadline, and a host with more asks
again (``pending``)."""

_NO_DIGEST = "0" * 64
_SOURCE_ID_FILE = "source-id"


class UsageProjects(Protocol):
    """What reading usage needs of the host's projects: the fold and the digest of a run."""

    def run_summary(self, project: Path, run_id: str) -> Summarized:
        """The fold of the run's journal as it is now."""
        ...

    def run_digest(self, project_root: Path, run_id: str) -> RunDigest | RunDigestUnreadable | None:
        """What the journal says of the run at a glance."""
        ...


class JsonlUsageJournal:
    """One run's JSONL journal, as the rollup reads it."""

    def __init__(
        self,
        journal: Path,
        *,
        summarize: Callable[[], Summarized],
        bests: Callable[[], tuple[datetime, ...]],
    ) -> None:
        """Read ``journal``; ``summarize`` folds it and ``bests`` says when its kept ideas landed."""
        self._journal = journal
        self._summarize = summarize
        self._bests = bests

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
        """The summary fold, the prefix it covers, and whether that prefix ends a complete line."""
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
            bests=self._bests(),
            identity=identity,
            persistable=found is not None,
        )


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
        """Replace the stored rollup. A write that fails costs only the next read's time."""
        try:
            atomic_write_bytes(self._path, rollup.to_binary())
        except (OSError, ProjectError):
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
            reading = read_or_build(
                JsonlUsageJournal(
                    journal,
                    summarize=lambda run_id=run_id, path=path: projects.run_summary(path, run_id),
                    bests=lambda run_id=run_id, root=root: _bests(projects, root, run_id),
                ),
                FileRollupStore(journal.with_name(ROLLUP_FILE)),
                budget,
            )
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


def usage_summary(
    request: host_payloads.UsageSummaryRequest,
    projects: UsageProjects,
    paths: Sequence[Path],
    pricing: Pricing,
    *,
    budget_bytes: int = COLD_BUDGET_BYTES,
) -> host_payloads.UsageSummaryResponse:
    """The reply to ``request`` over the runs of ``paths`` (the request's projects, expanded).

    Raises ``ValueError`` when the request's edges do not increase, which the read maps to invalid
    input.
    """
    if request.now is None:
        message = "now: required"
        raise ValueError(message)
    edges = [stamp.to_datetime() for stamp in request.edges]
    collected = collect_runs(projects, paths, since=edges[0], budget=Budget(budget_bytes))
    return summarize(
        SummaryInputs(
            request_id=request.request_id,
            edges=edges,
            now=request.now.to_datetime(),
            runs=collected.runs,
            journal=_journal_source(collected),
            pricing=pricing,
            pending_runs=collected.pending,
        )
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
        source_id=source_identity(Project.usage_state_directory()),
        status=status,
        scanned=collected.scanned,
        skipped=collected.skipped,
        failed=collected.failed,
    )


def _bests(projects: UsageProjects, root: Path, run_id: str) -> tuple[datetime, ...]:
    digest = projects.run_digest(root, run_id)
    if isinstance(digest, RunDigest) and digest.dashboard is not None:
        return kept_landings(digest.dashboard.points)
    return ()
