"""Withdrawing ledger requests: by an operator, or when no run has leased them for the grace period.

Both paths go through :func:`withdraw`, so a removed request and an abandoned one
end the same way: the job is cancelled if it is still queued or running, and the
attempt is recorded as withdrawn. The cancel comes first, so a crash between the
two leaves a cancelled job under a live record, which the next withdrawal or a
run attaching to it settles, never a withdrawn record over a live job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from .cluster_types import ClusterObservation, ClusterUnknown
from .ledger import (
    LedgerEntry,
    LedgerError,
    LedgerRecord,
    LedgerState,
    Unleased,
    lease_verdict,
)

if TYPE_CHECKING:
    from .api import Cluster, EvaluationLedger


@dataclass(frozen=True, slots=True)
class Withdrawn:
    """The attempt is withdrawn. ``cancelled_job`` is the Slurm job cancelled, if one was live."""

    key: str
    operation_id: str
    cancelled_job: str | None
    already: bool = False


@dataclass(frozen=True, slots=True)
class NotInLedger:
    """The ledger holds no request with this key."""

    key: str


@dataclass(frozen=True, slots=True)
class WithdrawUnknown:
    """The job's state or the cancel could not be established; nothing was recorded."""

    key: str
    reason: str


WithdrawOutcome = Withdrawn | NotInLedger | WithdrawUnknown


def withdraw(
    ledger: EvaluationLedger, cluster: Cluster, key: str, *, reason: str
) -> WithdrawOutcome:
    """Cancel the request's job if it is live, then record its attempt as withdrawn."""
    try:
        entry = ledger.read(key)
    except LedgerError as error:
        return WithdrawUnknown(key, str(error))
    if entry is None:
        return NotInLedger(key)
    record = entry.record
    if record.state is LedgerState.WITHDRAWN:
        return Withdrawn(key, record.operation_id, None, already=True)
    cancelled = _cancel_live(cluster, record)
    if isinstance(cancelled, WithdrawUnknown):
        return cancelled
    try:
        ledger.update(
            record.model_copy(update={"state": LedgerState.WITHDRAWN, "withdrawn_reason": reason})
        )
    except LedgerError as error:
        return WithdrawUnknown(key, str(error))
    return Withdrawn(key, record.operation_id, cancelled)


def _cancel_live(cluster: Cluster, record: LedgerRecord) -> str | None | WithdrawUnknown:
    """Cancel the attempt's job if it may still run; the job cancelled, if known."""
    observed = cluster.inspect(record.operation_id)
    if isinstance(observed, ClusterObservation):
        if observed.status.terminal:
            return None
        cancelled = cluster.cancel(record.operation_id)
        if isinstance(cancelled, ClusterUnknown):
            return WithdrawUnknown(record.key, cancelled.reason)
        return cancelled.job_id or observed.job_id
    if record.state is not LedgerState.SUBMITTING:
        return WithdrawUnknown(record.key, observed.reason)
    # No job is known under the attempt's identity: a tombstone keeps a submitter that
    # is still on its way from starting one.
    cancelled = cluster.cancel(record.operation_id)
    if isinstance(cancelled, ClusterUnknown):
        return WithdrawUnknown(record.key, cancelled.reason)
    return cancelled.job_id


def sweep_unleased(
    ledger: EvaluationLedger, cluster: Cluster, *, now: float, grace_seconds: float
) -> tuple[WithdrawOutcome, ...]:
    """Withdraw every live request no run has leased for longer than ``grace_seconds``.

    A finished job is left alone: nothing holds resources, and a later run that
    asks for the same key collects its result instead of running it again.
    """
    outcomes: list[WithdrawOutcome] = []
    for key in ledger.list_keys():
        try:
            entry = ledger.read(key)
        except LedgerError as error:
            outcomes.append(WithdrawUnknown(key, str(error)))
            continue
        if entry is None or not _abandoned(entry, now=now, grace_seconds=grace_seconds):
            continue
        if _finished(cluster, entry.record):
            continue
        outcomes.append(
            withdraw(ledger, cluster, key, reason=f"no run leased it for {grace_seconds:.0f} s")
        )
    return tuple(outcomes)


def _abandoned(entry: LedgerEntry, *, now: float, grace_seconds: float) -> bool:
    if entry.record.state is LedgerState.WITHDRAWN:
        return False
    verdict = lease_verdict(entry, now=now, grace_seconds=grace_seconds)
    return isinstance(verdict, Unleased)


def _finished(cluster: Cluster, record: LedgerRecord) -> bool:
    observed = cluster.inspect(record.operation_id)
    match observed:
        case ClusterObservation():
            return observed.status.terminal
        case ClusterUnknown():
            return False
        case _:
            assert_never(observed)


__all__ = [
    "NotInLedger",
    "WithdrawOutcome",
    "WithdrawUnknown",
    "Withdrawn",
    "sweep_unleased",
    "withdraw",
]
