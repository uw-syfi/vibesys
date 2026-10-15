"""Withdrawing ledger requests: by an operator, or when no run has leased them for the grace period.

Both paths go through :func:`withdraw`, so a removed request and an abandoned one
end the same way: the job is cancelled if it is still queued or running, and the
attempt is recorded as withdrawn, by compare-and-swap, so a racing writer is
never overwritten. The cancel comes first, so a crash between the
two leaves a cancelled job under a live record, which the next withdrawal or a
run attaching to it settles, never a withdrawn record over a live job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .cluster_types import ClusterObservation, ClusterUnknown
from .ledger import (
    LedgerEntry,
    LedgerError,
    LedgerRecord,
    LedgerState,
    Unleased,
    lease_verdict,
    settlement,
    transitioned,
)
from .ledger_listing import entry_id

if TYPE_CHECKING:
    from collections.abc import Callable

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
class Kept:
    """The request no longer met the withdrawal's condition when it was read; nothing changed.

    Only a conditional withdrawal (the sweep's) ends this way: a run leased the
    request, or its job settled, after the caller decided to withdraw it.
    """

    key: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class NotInLedger:
    """The ledger holds no request with this key."""

    key: str


@dataclass(frozen=True, slots=True)
class WithdrawUnknown:
    """The job's state or the cancel could not be established; nothing was recorded."""

    key: str
    reason: str


WithdrawOutcome = Withdrawn | Kept | NotInLedger | WithdrawUnknown


# Each lost compare-and-swap costs one more read; a race is lost only to a writer
# that moved the attempt forward, and an attempt has at most three states ahead.
_ROUNDS = 8


def withdraw(
    ledger: EvaluationLedger,
    cluster: Cluster,
    key: str,
    *,
    reason: str,
    still: Callable[[LedgerEntry], bool] | None = None,
) -> WithdrawOutcome:
    """Cancel the request's job if it is live, then record its attempt as withdrawn.

    The record changes by compare-and-swap: when a submitter records its job
    between this read and this write, the write loses, and the next round
    cancels the job it recorded. A failed or withdrawn attempt is final.

    ``still`` is the condition the withdrawal holds under (the sweep's: no run
    leases the request and its job has not settled). Every round tests it on
    its own fresh read, before it cancels anything, and ends with ``Kept`` once
    it fails, so a run that leased the request, or a collector that settled it,
    after the caller decided keeps its job and its result.
    """
    for _ in range(_ROUNDS):
        outcome = _withdraw_once(ledger, cluster, key, reason=reason, still=still)
        if outcome is not None:
            return outcome
    return WithdrawUnknown(key, "the request kept changing while it was withdrawn")


def _withdraw_once(
    ledger: EvaluationLedger,
    cluster: Cluster,
    key: str,
    *,
    reason: str,
    still: Callable[[LedgerEntry], bool] | None,
) -> WithdrawOutcome | None:
    """One read, check, cancel and swap; None when another writer won the swap."""
    try:
        entry = ledger.read(key)
        if entry is None:
            return NotInLedger(key)
        record = entry.record
        if still is not None and not still(entry):
            return Kept(key, record.operation_id)
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


_LOG = logging.getLogger(__name__)

_ORPHANING_STATES = frozenset({LedgerState.FAILED, LedgerState.WITHDRAWN})


@dataclass
class OrphanChecks:
    """Which jobless ended attempts one process's sweeps still inspect for an orphan job.

    An orphan starts only while a submitter that got past the attempt's
    cancellation tombstone is between its sbatch and its own cancel, a few
    transport round trips. So an attempt is inspected only for
    ``window_seconds`` (the claim window, far longer) from when this process
    first saw it ended, and never again once its job was seen ended. A fresh
    process inspects each old attempt for one window, then never: the cost of a
    sweep no longer grows with the ledger's history.
    """

    window_seconds: float
    _first_seen: dict[str, float] = field(default_factory=dict)
    _done: set[str] = field(default_factory=set)

    def due(self, operation_id: str, *, now: float) -> bool:
        """Whether the sweep at ``now`` inspects the attempt ``operation_id``."""
        if operation_id in self._done:
            return False
        if now - self._first_seen.setdefault(operation_id, now) <= self.window_seconds:
            return True
        self.ended(operation_id)
        return False

    def ended(self, operation_id: str) -> None:
        """No job can start under the attempt's name any more: never inspect it again."""
        self._first_seen.pop(operation_id, None)
        self._done.add(operation_id)


def _cancel_orphans(
    cluster: Cluster, entry: LedgerEntry, orphans: OrphanChecks, *, now: float
) -> tuple[WithdrawOutcome, ...]:
    """Cancel every live job named for an attempt that failed or was withdrawn with no job.

    Such a job is an orphan: its submitter sbatched it after the attempt was
    taken over or withdrawn (past the tombstone) and died before it could
    cancel it itself. No record names it, so only its name finds it. Each such
    attempt costs one cluster call per sweep while ``orphans`` says it is due;
    one that named its job costs none, since that job was seen ended (failed)
    or cancelled first (withdrawn).
    """
    outcomes: list[WithdrawOutcome] = []
    for record in (*entry.earlier, entry.record):
        if record.state not in _ORPHANING_STATES or record.job is not None:
            continue
        if not orphans.due(record.operation_id, now=now):
            continue
        observed = cluster.inspect(record.operation_id)
        if not isinstance(observed, ClusterObservation):
            continue  # no job under its name yet; inspected again while due
        if observed.status.terminal:
            orphans.ended(record.operation_id)
            continue
        cancelled = cluster.cancel(record.operation_id)
        if isinstance(cancelled, ClusterUnknown):
            outcomes.append(WithdrawUnknown(record.key, cancelled.reason))
        else:
            job = cancelled.job_id or observed.job_id
            outcomes.append(Withdrawn(record.key, record.operation_id, job, already=True))
    return tuple(outcomes)


def sweep_unleased(
    ledger: EvaluationLedger,
    cluster: Cluster,
    *,
    now: float,
    grace_seconds: float,
    orphans: OrphanChecks,
) -> tuple[WithdrawOutcome, ...]:
    """Withdraw every live request no run has leased for longer than ``grace_seconds``.

    It also cancels orphans: live jobs named for an attempt that failed or was
    withdrawn before naming a job (``_cancel_orphans``), whatever the leases
    say, for as long as ``orphans`` (the calling process's own) says.

    A settled request (completed, failed or withdrawn) costs one ledger read,
    plus one cluster call per attempt still due an orphan check. A submitted
    job found ended is not withdrawn, since it holds no resources: one that
    ended unsuccessfully settles ``failed``, and one that COMPLETED is left for
    the next ask, which collects it (only a collected result settles
    ``completed``; it costs one cluster call per sweep until then).
    """

    def sweepable(entry: LedgerEntry) -> bool:
        if entry.record.state.settled:
            return False
        return isinstance(lease_verdict(entry, now=now, grace_seconds=grace_seconds), Unleased)

    outcomes: list[WithdrawOutcome] = []
    for key in ledger.list_keys():
        try:
            entry = ledger.read(key)
        except LedgerError as error:
            outcomes.append(WithdrawUnknown(key, str(error)))
            continue
        if entry is None:
            continue
        outcomes.extend(_cancel_orphans(cluster, entry, orphans, now=now))
        if entry.unreadable_leases and not entry.record.state.settled:
            _warn_unreadable(entry)
        if sweepable(entry):
            reason = f"no run leased it for {grace_seconds:.0f} s"
            outcome = _sweep_one(ledger, cluster, entry, reason=reason, still=sweepable)
            if outcome is not None:
                outcomes.append(outcome)
    return tuple(outcomes)


def _warn_unreadable(entry: LedgerEntry) -> None:
    """Say that a lease this build cannot read holds a request, and how to free it now."""
    entry_name = entry_id(entry.record.key)
    for lease in entry.unreadable_leases:
        _LOG.warning(
            "evaluation ledger request %s has a lease from %s that this build cannot read "
            "(a newer build's, or a damaged file); it holds the request until %.0f s after "
            "its file was last written (epoch %.0f). `vibesys evals remove %s` withdraws it now",
            entry_name,
            lease.holder,
            entry.claim_window,
            lease.written_at,
            entry_name,
        )


def _sweep_one(
    ledger: EvaluationLedger,
    cluster: Cluster,
    entry: LedgerEntry,
    *,
    reason: str,
    still: Callable[[LedgerEntry], bool],
) -> WithdrawOutcome | None:
    """Settle an unleased request whose job ended, or withdraw it; None when nothing changed."""
    key = entry.record.key
    if entry.record.state is LedgerState.SUBMITTED:
        observed = cluster.inspect(entry.record.operation_id)
        if isinstance(observed, ClusterObservation) and observed.status.terminal:
            try:
                settle(ledger, entry, observed.status)
            except LedgerError as error:
                return WithdrawUnknown(key, str(error))
            return None  # settled failed, or completed and waiting for a run to collect it
    # Decided on ``entry``; ``withdraw`` tests ``still`` again on each of its own reads.
    outcome = withdraw(ledger, cluster, key, reason=reason, still=still)
    return None if isinstance(outcome, Kept) else outcome


__all__ = [
    "Kept",
    "NotInLedger",
    "OrphanChecks",
    "WithdrawOutcome",
    "WithdrawUnknown",
    "Withdrawn",
    "settle",
    "sweep_unleased",
    "withdraw",
]
