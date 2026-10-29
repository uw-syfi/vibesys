"""What a person reading a project's evaluation ledger sees: one row per request (#2757).

``list_entries`` reads every request through the ledger interface and asks the
cluster for the state of each job not yet settled; a settled request (done,
failed or withdrawn) is shown from its record alone. It needs no running VibeSys process.
Rows name a request by its short entry id, the first characters of its key,
which ``resolve_entries`` maps back to keys for a removal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, assert_never

from pydantic import BaseModel, ConfigDict

from .cluster_types import ClusterObservation, ClusterUnknown
from .ledger import LedgerEntry, LedgerError, LedgerState
from .scheduler_states import SlurmJobStatus

if TYPE_CHECKING:
    from .api import Cluster, EvaluationLedger

ENTRY_ID_LENGTH = 12
LISTING_FORMAT_VERSION = 1


class EntryPhase(StrEnum):
    """Where a request is, as an operator reads it."""

    CLAIMED = "claimed"  # a run is submitting its job
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    WITHDRAWN = "withdrawn"
    UNKNOWN = "unknown"  # the cluster could not say


class LedgerRow(BaseModel):
    """One request: who owns and leases it, what it evaluates, and where its job is."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: str
    key: str
    project: str
    attempt: int
    owner: str
    lease_holders: tuple[str, ...]
    # Holders whose lease this build cannot read (a newer build's); each holds the request.
    unreadable_leases: tuple[str, ...] = ()
    # Seconds since the newest lease renewal; None when no run holds a lease.
    lease_age_seconds: float | None
    candidate: str | None
    workstream: str | None
    phase: EntryPhase
    job_id: str | None
    # Seconds since the request was claimed, while its job waits in the queue.
    waiting_seconds: float | None
    pending_reason: str | None
    estimated_start: str | None
    # The job's directory on the cluster, which holds its result files.
    result_location: str | None
    detail: str | None


class LedgerListing(BaseModel):
    """The rows of one project's ledger, in key order; the ``--json`` document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: int = LISTING_FORMAT_VERSION
    project: str
    rows: tuple[LedgerRow, ...]
    unreadable: tuple[str, ...] = ()


def entry_id(key: str) -> str:
    """The short id a person types for a request."""
    return key[:ENTRY_ID_LENGTH]


def list_entries(
    ledger: EvaluationLedger, cluster: Cluster, *, project: str, now: float
) -> LedgerListing:
    """Read every request of the project and the state of each one's job."""
    rows: list[LedgerRow] = []
    unreadable: list[str] = []
    for key in ledger.list_keys():
        try:
            entry = ledger.read(key)
        except LedgerError as error:
            unreadable.append(f"{entry_id(key)}: {error}")
            continue
        if entry is not None:
            rows.append(_row(entry, cluster, now=now))
    return LedgerListing(project=project, rows=tuple(rows), unreadable=tuple(unreadable))


@dataclass(frozen=True, slots=True)
class Resolved:
    """Every id named exactly one request."""

    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Unresolved:
    """Some ids named no request, or more than one."""

    unknown: tuple[str, ...]
    ambiguous: tuple[str, ...]


def resolve_entries(keys: tuple[str, ...], ids: tuple[str, ...]) -> Resolved | Unresolved:
    """Map entry ids (or any unique key prefix) to keys, in the order given."""
    found: list[str] = []
    unknown: list[str] = []
    ambiguous: list[str] = []
    for given in ids:
        matches = [key for key in keys if given and key.startswith(given.lower())]
        if not matches:
            unknown.append(given)
        elif len(matches) > 1:
            ambiguous.append(given)
        elif matches[0] not in found:
            found.append(matches[0])
    if unknown or ambiguous:
        return Unresolved(tuple(unknown), tuple(ambiguous))
    return Resolved(tuple(found))


def _row(entry: LedgerEntry, cluster: Cluster, *, now: float) -> LedgerRow:
    record = entry.record
    newest = max((lease.renewed_at for lease in entry.leases), default=None)
    phase, observed, detail = _phase(entry, cluster)
    job = record.job
    return LedgerRow(
        entry=entry_id(record.key),
        key=record.key,
        project=record.project,
        attempt=record.attempt,
        owner=record.owner,
        lease_holders=tuple(lease.holder for lease in entry.leases),
        unreadable_leases=tuple(lease.holder for lease in entry.unreadable_leases),
        lease_age_seconds=None if newest is None else max(0.0, now - newest),
        candidate=record.labels.candidate,
        workstream=record.labels.workstream,
        phase=phase,
        job_id=job.job_id if job is not None else (observed.job_id if observed else None),
        waiting_seconds=(
            max(0.0, now - record.created_at)
            if phase in {EntryPhase.CLAIMED, EntryPhase.QUEUED}
            else None
        ),
        pending_reason=observed.pending_reason if observed is not None else None,
        estimated_start=observed.estimated_start if observed is not None else None,
        result_location=(
            None if job is None else PurePosixPath(job.remote_workspace).parent.as_posix()
        ),
        detail=detail if detail is not None else _stated_reason(entry),
    )


def _stated_reason(entry: LedgerEntry) -> str | None:
    """Why the attempt ended, or the withdrawal under way on it."""
    mark = entry.withdrawal
    if mark is not None and not entry.record.state.final:
        return f"being withdrawn by {mark.by}: {mark.reason}"
    return entry.record.reason


def _phase(
    entry: LedgerEntry, cluster: Cluster
) -> tuple[EntryPhase, ClusterObservation | None, str | None]:
    record = entry.record
    match record.state:
        case LedgerState.WITHDRAWN:
            return EntryPhase.WITHDRAWN, None, None
        case LedgerState.COMPLETED:
            return EntryPhase.DONE, None, None
        case LedgerState.FAILED:
            return EntryPhase.FAILED, None, None
        case LedgerState.SUBMITTING | LedgerState.SUBMITTED:
            pass
        case _:
            assert_never(record.state)
    observed = cluster.inspect(record.operation_id)
    match observed:
        case ClusterUnknown(reason=reason):
            if record.state is LedgerState.SUBMITTING:
                return EntryPhase.CLAIMED, None, None
            return EntryPhase.UNKNOWN, None, reason
        case ClusterObservation(status=status):
            return _STATUS_PHASES[status], observed, None
        case _:
            assert_never(observed)


_STATUS_PHASES = {
    SlurmJobStatus.UNKNOWN: EntryPhase.UNKNOWN,
    SlurmJobStatus.PENDING: EntryPhase.QUEUED,
    SlurmJobStatus.RUNNING: EntryPhase.RUNNING,
    SlurmJobStatus.COMPLETED: EntryPhase.DONE,
    SlurmJobStatus.FAILED: EntryPhase.FAILED,
    SlurmJobStatus.CANCELLED: EntryPhase.FAILED,
}


__all__ = [
    "ENTRY_ID_LENGTH",
    "EntryPhase",
    "LedgerListing",
    "LedgerRow",
    "Resolved",
    "Unresolved",
    "entry_id",
    "list_entries",
    "resolve_entries",
]
