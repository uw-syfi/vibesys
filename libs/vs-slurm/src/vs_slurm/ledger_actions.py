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
    settlement,
    transitioned,
)

if TYPE_CHECKING:
    from .api import Cluster, EvaluationLedger
    from .cluster_types import ClusterCollectOutcome
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


def settle(
    ledger: EvaluationLedger,
    entry: LedgerEntry,
    status: SlurmJobStatus,
    collected: ClusterCollectOutcome | None = None,
) -> bool:
    """Record how the attempt's job ended (``settlement``); False if it cannot settle yet or a writer won.

    ``collected`` is what a run collected from the ended job, None when nothing
    was: a COMPLETED job settles only with its collected results. A settled
    attempt is never inspected on the cluster again: a completed one is reused
    by every later ask, a failed one makes the next ask start anew.
    """
    record = entry.record
    if record.state is not LedgerState.SUBMITTED or record.job is None:
        return False
    settled = settlement(status, collected, job=record.job)
    if settled is None:
        return False
    return ledger.replace(entry, transitioned(record, settled.state, reason=settled.reason))


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


_ORPHANING_STATES = frozenset({LedgerState.FAILED, LedgerState.WITHDRAWN})


def _cancel_orphans(cluster: Cluster, entry: LedgerEntry) -> tuple[WithdrawOutcome, ...]:
    """Cancel every live job named for an attempt that failed or was withdrawn with no job.

    Such a job is an orphan: its submitter sbatched it after the attempt was
    taken over or withdrawn (past the tombstone) and died before it could
    cancel it itself. No record names it, so only its name finds it. Each such
    attempt costs one cluster call per sweep; one that named its job does not,
    since that job was seen ended (failed) or cancelled first (withdrawn).
    """
    outcomes: list[WithdrawOutcome] = []
    for record in (*entry.earlier, entry.record):
        if record.state not in _ORPHANING_STATES or record.job is not None:
            continue
        observed = cluster.inspect(record.operation_id)
        if not isinstance(observed, ClusterObservation) or observed.status.terminal:
            continue
        cancelled = cluster.cancel(record.operation_id)
        if isinstance(cancelled, ClusterUnknown):
            outcomes.append(WithdrawUnknown(record.key, cancelled.reason))
        else:
            job = cancelled.job_id or observed.job_id
            outcomes.append(Withdrawn(record.key, record.operation_id, job, already=True))
    return tuple(outcomes)


def sweep_unleased(
    ledger: EvaluationLedger, cluster: Cluster, *, now: float, grace_seconds: float
) -> tuple[WithdrawOutcome, ...]:
    """Withdraw every live request no run has leased for longer than ``grace_seconds``.

    It also cancels orphans: live jobs named for an attempt that failed or was
    withdrawn before naming a job (``_cancel_orphans``), whatever the leases say.

    A settled request (completed, failed or withdrawn) costs one ledger read,
    plus one cluster call per attempt it failed or withdrew before naming a job. A submitted job found ended is not withdrawn, since it holds
    no resources: one that ended unsuccessfully settles ``failed``, and one that
    COMPLETED is left for the next ask, which collects it (only a collected
    result settles ``completed``; it costs one cluster call per sweep until then).
    """
    outcomes: list[WithdrawOutcome] = []
    for key in ledger.list_keys():
        try:
            entry = ledger.read(key)
        except LedgerError as error:
            outcomes.append(WithdrawUnknown(key, str(error)))
            continue
        if entry is None:
            continue
        outcomes.extend(_cancel_orphans(cluster, entry))
        if entry.record.state.settled:
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
                continue  # settled failed, or completed and waiting for a run to collect it
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
