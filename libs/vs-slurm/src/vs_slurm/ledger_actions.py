"""Withdrawing ledger requests: by an operator, or when no run has leased them for the grace period.

Both paths go through :func:`withdraw`, so a removed request and an abandoned one
end the same way: the job is cancelled if it is still queued or running, and the
attempt is recorded as withdrawn, by compare-and-swap, so a racing writer is
never overwritten. The cancel comes first, so a crash between the
two leaves a cancelled job under a live record, which the next withdrawal or a
run attaching to it settles, never a withdrawn record over a live job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .cluster_types import ClusterObservation, ClusterUnknown
from .ledger import (
    Leased,
    LedgerEntry,
    LedgerError,
    LedgerRecord,
    LedgerState,
    lease_verdict,
    settled_state,
    transitioned,
)

if TYPE_CHECKING:
    from .api import Cluster, EvaluationLedger
    from .scheduler_states import SlurmJobStatus


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


# Each lost compare-and-swap costs one more read; a race is lost only to a writer
# that moved the attempt forward, and an attempt has at most three states ahead.
_ROUNDS = 8


def withdraw(
    ledger: EvaluationLedger, cluster: Cluster, key: str, *, reason: str
) -> WithdrawOutcome:
    """Cancel the request's job if it is live, then record its attempt as withdrawn.

    The record changes by compare-and-swap: when a submitter records its job
    between this read and this write, the write loses, and the next round
    cancels the job it recorded. A failed or withdrawn attempt is final.
    """
    for _ in range(_ROUNDS):
        outcome = _withdraw_once(ledger, cluster, key, reason=reason)
        if outcome is not None:
            return outcome
    return WithdrawUnknown(key, "the request kept changing while it was withdrawn")


def _withdraw_once(
    ledger: EvaluationLedger, cluster: Cluster, key: str, *, reason: str
) -> WithdrawOutcome | None:
    """One read, cancel and swap; None when another writer won the swap."""
    try:
        entry = ledger.read(key)
        if entry is None:
            return NotInLedger(key)
        record = entry.record
        if record.state.final:
            return Withdrawn(key, record.operation_id, None, already=True)
        cancelled = _cancel_live(cluster, record)
        if isinstance(cancelled, WithdrawUnknown):
            return cancelled
        won = ledger.replace(entry, transitioned(record, LedgerState.WITHDRAWN, reason=reason))
    except LedgerError as error:
        return WithdrawUnknown(key, str(error))
    return Withdrawn(key, record.operation_id, cancelled) if won else None


def settle(ledger: EvaluationLedger, entry: LedgerEntry, status: SlurmJobStatus) -> bool:
    """Record that the attempt's job ended with ``status``; False if it is live or a writer won.

    A settled attempt is never inspected on the cluster again: a completed one
    is reused by every later ask, a failed one makes the next ask start anew.
    """
    state = settled_state(status)
    record = entry.record
    if state is None or record.state is not LedgerState.SUBMITTED:
        return False
    job = record.job.job_id if record.job is not None else "?"
    reason = None if state is LedgerState.COMPLETED else f"job {job} ended {status.value}"
    return ledger.replace(entry, transitioned(record, state, reason=reason))


def _cancel_live(cluster: Cluster, record: LedgerRecord) -> str | None | WithdrawUnknown:
    """Cancel the attempt's job if it may still run; the job cancelled, if known."""
    if record.state.settled:
        return None  # its job ended; the cluster may no longer even know it
    observed = cluster.inspect(record.operation_id)
    if isinstance(observed, ClusterObservation):
        if observed.status.terminal:
            return None
        known: str | None = observed.job_id
    elif record.state is LedgerState.SUBMITTING:
        # No job is known under the attempt's identity: a tombstone keeps a submitter
        # that is still on its way from starting one.
        known = None
    else:
        return WithdrawUnknown(record.key, observed.reason)
    cancelled = cluster.cancel(record.operation_id)
    if isinstance(cancelled, ClusterUnknown):
        return WithdrawUnknown(record.key, cancelled.reason)
    return cancelled.job_id or known


def sweep_unleased(
    ledger: EvaluationLedger, cluster: Cluster, *, now: float, grace_seconds: float
) -> tuple[WithdrawOutcome, ...]:
    """Withdraw every live request no run has leased for longer than ``grace_seconds``.

    A settled request (completed, failed or withdrawn) costs one ledger read and
    no cluster call. A submitted job found finished is settled instead of
    withdrawn: nothing holds resources, and a later ask for a completed one
    collects its result instead of running it again.
    """
    outcomes: list[WithdrawOutcome] = []
    for key in ledger.list_keys():
        try:
            entry = ledger.read(key)
        except LedgerError as error:
            outcomes.append(WithdrawUnknown(key, str(error)))
            continue
        if entry is None or entry.record.state.settled:
            continue
        if isinstance(lease_verdict(entry, now=now, grace_seconds=grace_seconds), Leased):
            continue
        if entry.record.state is LedgerState.SUBMITTED:
            observed = cluster.inspect(entry.record.operation_id)
            if isinstance(observed, ClusterObservation) and observed.status.terminal:
                try:
                    settle(ledger, entry, observed.status)
                except LedgerError as error:
                    outcomes.append(WithdrawUnknown(key, str(error)))
                continue
        outcomes.append(
            withdraw(ledger, cluster, key, reason=f"no run leased it for {grace_seconds:.0f} s")
        )
    return tuple(outcomes)


__all__ = [
    "NotInLedger",
    "WithdrawOutcome",
    "WithdrawUnknown",
    "Withdrawn",
    "settle",
    "sweep_unleased",
    "withdraw",
]
