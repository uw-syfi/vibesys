"""Withdrawing ledger requests: by an operator, by the run that stops wanting them, or when no run has leased them for the grace period.

Every path goes through :func:`withdraw`, so a removed request and an abandoned one
end the same way: the job is cancelled if it is still queued or running, and the
attempt is recorded as withdrawn, by compare-and-swap, so a racing writer is
never overwritten.

A withdrawal takes two swaps. The first marks the attempt ``withdrawing``; from
then on no run attaches to it (``AwaitWithdrawal``). The withdrawer then reads
the leases again, and a run that leased the request before that read, which
every run does before it reads the record to attach, sends the attempt back to
its state: the withdrawal ends ``Kept`` and the run keeps its job. Only then is
the job cancelled, and the second swap records the attempt withdrawn. A cancel
whose outcome is unknown also sends it back, so the attempt is never left
marked by a live withdrawer. A crash leaves the mark, or a cancelled job under
a live record, which the next withdrawal or a run attaching to it undoes or
settles once the mark outlived ``WITHDRAWAL_WINDOW_SECONDS``: never a withdrawn
record over a live job.
"""

from __future__ import annotations

import dataclasses
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
    held_by_others,
    lease_verdict,
    marked_withdrawing,
    settlement,
    transitioned,
    undone_withdrawal,
    withdrawal_expired,
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

    Only a conditional withdrawal (the sweep's, or a run's relinquish) ends this
    way: a run leased the request, or its job settled, after the caller decided
    to withdraw it, even after the attempt was marked ``withdrawing``.
    """

    key: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class NotInLedger:
    """The ledger holds no request with this key."""

    key: str


@dataclass(frozen=True, slots=True)
class WithdrawUnknown:
    """The job's state or the cancel could not be established, or another withdrawal is under way.

    Nothing was recorded: the attempt is in the state it was read in. Trying
    again later finds the job's state, or the other withdrawal's end.
    """

    key: str
    reason: str


WithdrawOutcome = Withdrawn | Kept | NotInLedger | WithdrawUnknown


# Each lost compare-and-swap costs one more read; a race is lost only to a writer
# that moved the attempt forward, and an attempt has at most four states ahead.
_ROUNDS = 8

WITHDRAWAL_WINDOW_SECONDS = 600.0
"""How long a withdrawal may take before it is taken to have died, and is undone.

A withdrawal is two ledger swaps, a lease read and two cluster calls (inspect
and cancel), each bounded by its transport's timeout; a run waits this long, at
most, on one whose withdrawer crashed.
"""


@dataclass(frozen=True, slots=True)
class Withdrawer:
    """Who withdraws a request, at what time, and why: what its mark and its end say.

    ``by`` is a holder name (a run's, or the operator command's); ``now`` is the
    withdrawer's wall clock, in epoch seconds, which the mark's window starts at.
    """

    by: str
    now: float
    reason: str


def withdraw(
    ledger: EvaluationLedger,
    cluster: Cluster,
    key: str,
    withdrawer: Withdrawer,
    *,
    still: Callable[[LedgerEntry], bool] | None = None,
) -> WithdrawOutcome:
    """Cancel the request's job if it is live, then record its attempt as withdrawn.

    ``withdrawer`` is named in the ``withdrawing`` mark and the withdrawn record.
    The record changes by compare-and-swap: when a submitter records its job
    between this read and this write, the write loses, and the next round
    starts again from what it recorded. A failed or withdrawn attempt is final.
    An attempt another withdrawal is under way on is ``WithdrawUnknown`` until
    that one ends, or outlives its window and is undone.

    ``still`` is the condition the withdrawal holds under (the sweep's: no run
    leases the request and its job has not settled; a relinquish's: no other
    run wants it). It is tested on the read the mark is decided on, and again
    on the leases read after the mark, before anything is cancelled; once it
    fails the mark is undone and the withdrawal ends ``Kept``, so a run that
    leased the request, or a collector that settled it, keeps its job and its
    result.
    """
    for _ in range(_ROUNDS):
        outcome = _withdraw_once(ledger, cluster, key, withdrawer, still=still)
        if outcome is not None:
            return outcome
    return WithdrawUnknown(key, "the request kept changing while it was withdrawn")


def _withdraw_once(
    ledger: EvaluationLedger,
    cluster: Cluster,
    key: str,
    withdrawer: Withdrawer,
    *,
    still: Callable[[LedgerEntry], bool] | None,
) -> WithdrawOutcome | None:
    """One read, check and mark, then the second phase; None when another writer won a swap."""
    try:
        entry = ledger.read(key)
        if entry is None:
            return NotInLedger(key)
        marked = _mark(ledger, entry, withdrawer, still=still)
    except LedgerError as error:
        return WithdrawUnknown(key, str(error))
    if not isinstance(marked, LedgerRecord):
        return marked
    return _withdraw_marked(ledger, cluster, marked, still=still)


def _mark(
    ledger: EvaluationLedger,
    entry: LedgerEntry,
    withdrawer: Withdrawer,
    *,
    still: Callable[[LedgerEntry], bool] | None,
) -> LedgerRecord | WithdrawOutcome | None:
    """Mark the attempt withdrawing: the mark written, or how the round ended without one."""
    record = entry.record
    if record.state.final:
        return Withdrawn(record.key, record.operation_id, None, already=True)
    if record.withdrawal is not None:
        if not withdrawal_expired(record, now=withdrawer.now):
            return WithdrawUnknown(record.key, f"{record.withdrawal.by} is withdrawing it")
        # Its withdrawer died part way: undo its mark, then decide afresh.
        ledger.replace(entry, undone_withdrawal(record))
        return None
    if still is not None and not still(entry):
        return Kept(record.key, record.operation_id)
    marked = marked_withdrawing(
        record,
        by=withdrawer.by,
        since=withdrawer.now,
        until=withdrawer.now + WITHDRAWAL_WINDOW_SECONDS,
        reason=withdrawer.reason,
    )
    return marked if ledger.replace(entry, marked) else None


def _withdraw_marked(
    ledger: EvaluationLedger,
    cluster: Cluster,
    marked: LedgerRecord,
    *,
    still: Callable[[LedgerEntry], bool] | None,
) -> WithdrawOutcome | None:
    """Read the leases again, then undo the mark or cancel the job and record it withdrawn."""
    key = marked.key
    before = undone_withdrawal(marked)
    try:
        entry = ledger.read(key)
        if entry is None or entry.record != marked:
            return None  # undone as expired by another process: decide afresh
        if still is not None and not still(dataclasses.replace(entry, record=before)):
            ledger.replace(entry, before)
            return Kept(key, marked.operation_id)
        cancelled = _cancel_live(cluster, before)
        if isinstance(cancelled, WithdrawUnknown):
            ledger.replace(entry, before)
            return cancelled
        withdrawal = marked.withdrawal
        reason = withdrawal.reason if withdrawal is not None else "withdrawn"
        won = ledger.replace(entry, transitioned(marked, LedgerState.WITHDRAWN, reason=reason))
    except LedgerError as error:
        _undo_quietly(ledger, marked)
        return WithdrawUnknown(key, str(error))
    return Withdrawn(key, marked.operation_id, cancelled) if won else None


def _undo_quietly(ledger: EvaluationLedger, marked: LedgerRecord) -> None:
    """Undo this withdrawal's mark if it still stands; one that cannot be undone expires."""
    try:
        entry = ledger.read(marked.key)
        if entry is not None and entry.record == marked:
            ledger.replace(entry, undone_withdrawal(marked))
    except LedgerError:
        _LOG.warning(
            "evaluation ledger request %s stays marked withdrawing for up to %.0f s",
            entry_id(marked.key),
            WITHDRAWAL_WINDOW_SECONDS,
        )


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

SWEEP_WITHDRAWER = "vibesys-sweep"
"""How a sweep's withdrawal names its withdrawer: any attached run sweeps."""

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
        record = entry.record
        if record.state.settled:
            return False
        if record.withdrawal is not None and not withdrawal_expired(record, now=now):
            return False  # another withdrawal is under way; an expired one is undone
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
            withdrawer = Withdrawer(
                by=SWEEP_WITHDRAWER, now=now, reason=f"no run leased it for {grace_seconds:.0f} s"
            )
            outcome = _sweep_one(ledger, cluster, entry, withdrawer, still=sweepable)
            if outcome is not None:
                outcomes.append(outcome)
    return tuple(outcomes)


def relinquish(
    ledger: EvaluationLedger,
    cluster: Cluster,
    key: str,
    holder: Withdrawer,
    *,
    since: float,
) -> WithdrawOutcome:
    """Drop ``holder``'s lease on the request, and withdraw it unless another run still wants it.

    ``holder.by`` is the run, whose lease is dropped and who withdraws.
    ``since`` is now less the grace period: a run whose lease lapsed after it
    may still come back for the request (``held_by_others``).

    A run that stops wanting a request (it cancelled the evaluation, or it is
    stopping) calls this, so no job is left queued for nobody. The request
    stays, and so does its job, while another run wants it (``held_by_others``)
    or once its job has settled: ``Kept``. Otherwise its job is cancelled and
    the attempt withdrawn as the sweep does (``_sweep_one``): a job that already
    ended is settled instead, and that too is ``Kept``. The condition is tested
    again on every read the withdrawal makes, so a run that leases the request
    meanwhile keeps its job.
    """

    def unwanted(entry: LedgerEntry) -> bool:
        if entry.record.state.settled:
            return False
        return not held_by_others(entry, holder=holder.by, since=since)

    try:
        ledger.release(key, holder.by)
        entry = ledger.read(key)
    except LedgerError as error:
        return WithdrawUnknown(key, str(error))
    if entry is None:
        return NotInLedger(key)
    if not unwanted(entry):
        return Kept(key, entry.record.operation_id)
    outcome = _sweep_one(ledger, cluster, entry, holder, still=unwanted)
    return Kept(key, entry.record.operation_id) if outcome is None else outcome


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
    withdrawer: Withdrawer,
    *,
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
    outcome = withdraw(ledger, cluster, key, withdrawer, still=still)
    return None if isinstance(outcome, Kept) else outcome


__all__ = [
    "SWEEP_WITHDRAWER",
    "WITHDRAWAL_WINDOW_SECONDS",
    "Kept",
    "NotInLedger",
    "OrphanChecks",
    "WithdrawOutcome",
    "WithdrawUnknown",
    "Withdrawer",
    "Withdrawn",
    "relinquish",
    "settle",
    "sweep_unleased",
    "withdraw",
]
