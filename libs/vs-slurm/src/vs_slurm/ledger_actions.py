"""Withdrawing ledger requests: by an operator, by the run that stops wanting them, or when no run has leased them for the grace period.

Every path goes through :func:`withdraw`, so a removed request and an abandoned one
end the same way: the job is cancelled if it is still queued or running, and the
attempt is recorded as withdrawn, by compare-and-swap, so a racing writer is
never overwritten.

A withdrawal takes two swaps. The first writes a withdrawal mark on the attempt
(``WithdrawalMark``, beside the record); from then on no run attaches to it
(``AwaitWithdrawal``). The withdrawer then reads the request again, and a run
that leased the request before that read, which every run does before it reads
the request to attach, makes it undo the mark: the withdrawal ends ``Kept`` and
the run keeps its job. Only then is the job cancelled, and the second swap
records the attempt withdrawn. The cancel's scancel is its first transport
call, and goes out only while the mark holds for it, on every clock, so it
never reaches a job a run attached to after the mark expired. A cancel whose
outcome is unknown also undoes the mark, so the attempt is never left marked by
a live withdrawer. A crash leaves the mark, or a cancelled job under a live
record, which the next withdrawal or a run attaching to it undoes or settles
once the mark outlived its window (``undo_expired_withdrawal``): never a
withdrawn record over a job seen live. A submitting attempt whose inspect found
no job under its name is the one exception: it is withdrawn first, and a job
its submitter starts late is cancelled by that submitter or by the sweep's
orphan check (``take_over_jobless``).
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .cluster_types import (
    OBSERVED_CANCEL_CALLS,
    ClusterObservation,
    ClusterUnknown,
    live_observation,
)
from .ledger import (
    MAX_WITHDRAWAL_WINDOW_SECONDS,
    LedgerEntry,
    LedgerError,
    LedgerState,
    Unleased,
    WithdrawalMark,
    WithdrawalUndone,
    held_by_others,
    lease_verdict,
    settlement,
    transitioned,
    withdrawal_expired,
)
from .ledger_listing import entry_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Clock

    from .api import Cluster, EvaluationLedger
    from .cluster_types import ClusterCancelOutcome, ClusterCollectOutcome, ClusterInspectOutcome
    from .ledger import LedgerRecord
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
    to withdraw it, even after it marked the attempt (``WithdrawalMark``).
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

CLOCK_SKEW_SECONDS = 60.0
"""How far apart the wall clocks of two processes sharing a ledger may be.

A withdrawal's window is stamped by its withdrawer's clock and read by any
other process's; a withdrawer acts only while its own window holds this much
longer, so no other clock already reads it expired.
"""

# The transport calls a withdrawer must still have time for when it cancels the
# job (the cancel's, up to its scancel, given the inspect's reading), when it
# inspects the job, and from a mark's stamp on: the mark's publish (mkdir, put,
# link) and the read that shows it come before the inspect.
_CALLS_AT_CANCEL = OBSERVED_CANCEL_CALLS
_CALLS_AT_INSPECT = 1 + _CALLS_AT_CANCEL
_CALLS_TO_CANCEL = 4 + _CALLS_AT_INSPECT


def withdrawal_window_seconds(call_seconds: float) -> float:
    """How long a withdrawal mark holds, from its stamp, when one transport call may take ``call_seconds``. Pure."""
    return (_CALLS_TO_CANCEL + 1) * call_seconds + CLOCK_SKEW_SECONDS


@dataclass(frozen=True, slots=True)
class WithdrawalTiming:
    """The clock a withdrawal is stamped by, and how long one transport call may take.

    ``clock`` is the withdrawer's wall clock, in epoch seconds: each mark is
    stamped from a fresh reading of it. ``call_seconds`` bounds one transport
    call to the cluster (``SlurmConfig.transport_timeout_seconds``). A mark
    holds long enough for the calls from its stamp to its cancel, each that
    long, plus ``CLOCK_SKEW_SECONDS``; the withdrawer cancels nothing once too
    little of it is left (``holds``). A timing whose mark would hold longer
    than ``MAX_WITHDRAWAL_WINDOW_SECONDS`` is refused: a build that cannot read
    the mark undoes it after that long.
    """

    clock: Clock
    call_seconds: float

    def __post_init__(self) -> None:
        """Refuse a call bound whose mark would outlast ``MAX_WITHDRAWAL_WINDOW_SECONDS``."""
        if self.window_seconds > MAX_WITHDRAWAL_WINDOW_SECONDS:
            message = (
                f"a withdrawal mark at transport_timeout_seconds={self.call_seconds:g} holds "
                f"{self.window_seconds:g} s ({_CALLS_TO_CANCEL + 1} calls plus "
                f"{CLOCK_SKEW_SECONDS:g} s), more than the {MAX_WITHDRAWAL_WINDOW_SECONDS:g} s "
                "any withdrawal may hold; lower transport_timeout_seconds"
            )
            raise LedgerError(message)

    @property
    def window_seconds(self) -> float:
        """How long a mark holds, from its stamp."""
        return withdrawal_window_seconds(self.call_seconds)

    def holds(self, mark: WithdrawalMark, *, calls: int) -> bool:
        """Whether ``mark`` holds, on every process's clock, for ``calls`` more transport calls."""
        needed = calls * self.call_seconds + CLOCK_SKEW_SECONDS
        return mark.until - self.clock.now() >= needed


@dataclass(frozen=True, slots=True)
class Withdrawer:
    """Who withdraws a request, and why, by which timing: what its mark and its end say.

    ``by`` is a holder name (a run's, or the operator command's). ``writer``
    tells this withdrawer's marks from every other's, even one with the same
    name at the same instant: a mark that names it is this withdrawer's own,
    whether its write's answer was lost or an earlier call left it standing.
    A run keeps one for all its withdrawals.
    """

    by: str
    reason: str
    timing: WithdrawalTiming
    writer: str = field(default_factory=lambda: uuid.uuid4().hex)

    def wrote(self, mark: WithdrawalMark | None) -> bool:
        """Whether ``mark`` is this withdrawer's own."""
        return mark is not None and (mark.by, mark.writer) == (self.by, self.writer)

    def stamped(self, attempt: int) -> WithdrawalMark:
        """A mark on ``attempt``, holding for one window from now."""
        now = self.timing.clock.now()
        return WithdrawalMark(
            attempt=attempt,
            by=self.by,
            writer=self.writer,
            since=now,
            until=now + self.timing.window_seconds,
            reason=self.reason,
        )


def withdraw(
    ledger: EvaluationLedger,
    cluster: Cluster,
    key: str,
    withdrawer: Withdrawer,
    *,
    still: Callable[[LedgerEntry], bool] | None = None,
) -> WithdrawOutcome:
    """Cancel the request's job if it is live, then record its attempt as withdrawn.

    ``withdrawer`` is named in the mark and the withdrawn record. Each round
    reads the request and moves it one step: it writes this withdrawal's mark,
    or, once a read shows the mark standing, cancels the job and records the
    attempt withdrawn by compare-and-swap. When a submitter records its job
    between that read and that write, the write loses, and the next round
    starts again from what it recorded. A failed or withdrawn attempt is final.
    An attempt another withdrawal is under way on is ``WithdrawUnknown`` until
    that one ends, or outlives its window and is undone. A mark write whose
    answer was lost is decided by the next read, which shows the mark or not;
    a mark that names this withdrawer as its writer is its own (``Withdrawer.wrote``),
    even one an earlier call of it left standing.

    ``still`` is the condition the withdrawal holds under (the sweep's: no run
    leases the request and its job has not settled; a relinquish's: no other
    run wants it). It is tested on the read the mark is decided on, and again
    on the read that shows the mark standing, before anything is cancelled;
    once it fails the mark is undone and the withdrawal ends ``Kept``, so a run
    that leased the request, or a collector that settled it, keeps its job and
    its result.
    """
    for round_ in range(_ROUNDS):
        try:
            entry = ledger.read(key)
        except LedgerError as error:
            if round_ > 0:
                _undo_quietly(ledger, key, withdrawer)  # a mark this call wrote, if any
            return WithdrawUnknown(key, str(error))
        if entry is None:
            return NotInLedger(key)
        record = entry.record
        if record.state.final:
            # A mark left on a final attempt is never read: no run waits on it.
            return Withdrawn(key, record.operation_id, None, already=True)
        if withdrawer.wrote(entry.withdrawal):
            outcome = _withdraw_marked(ledger, cluster, entry, withdrawer, still=still)
        else:
            outcome = _mark(ledger, entry, withdrawer, still=still)
        if outcome is not None:
            return outcome
    _undo_quietly(ledger, key, withdrawer)
    return WithdrawUnknown(key, "the request kept changing while it was withdrawn")


def _mark(
    ledger: EvaluationLedger,
    entry: LedgerEntry,
    withdrawer: Withdrawer,
    *,
    still: Callable[[LedgerEntry], bool] | None,
) -> WithdrawOutcome | None:
    """Write this withdrawal's mark, or end; None when the next round's read decides.

    A mark write that fails, or whose answer is lost, is decided by the next
    round's read; one that loses its swap is too.
    """
    record = entry.record
    standing = entry.withdrawal
    if standing is not None:
        if not withdrawal_expired(standing, now=withdrawer.timing.clock.now()):
            return WithdrawUnknown(record.key, f"{standing.by} is withdrawing it")
        # Its withdrawer died part way: undo its mark, then decide afresh.
        try:
            undo_expired_withdrawal(ledger, entry, now=withdrawer.timing.clock.now())
        except LedgerError as error:
            return WithdrawUnknown(record.key, str(error))
        return None
    if still is not None and not still(entry):
        return Kept(record.key, record.operation_id)
    # Stamped now, not when the withdrawal began: its window starts at this write.
    mark = withdrawer.stamped(record.attempt)
    try:
        ledger.write_withdrawal(entry, mark)
    except LedgerError:
        _LOG.info("the mark on %s may not be written; reading it back", entry_id(record.key))
    return None


def _withdraw_marked(
    ledger: EvaluationLedger,
    cluster: Cluster,
    entry: LedgerEntry,
    withdrawer: Withdrawer,
    *,
    still: Callable[[LedgerEntry], bool] | None,
) -> WithdrawOutcome | None:
    """On a read that shows this withdrawal's mark: undo it, or cancel the job and record it withdrawn.

    A run that leased the request before ``entry`` was read is in it, and
    keeps its job; one that leases it later reads the mark, and waits, but
    only while the mark holds: past its window another process undoes it, and
    a run may attach. So the job is inspected, and then cancelled, only while
    the mark still holds for the calls left (``WithdrawalTiming.holds``);
    otherwise the mark is undone and nothing is cancelled. The cancel is given
    the inspect's reading, so its scancel is its first call; a job seen ending
    is not cancelled at all, and a submitting attempt no job was seen under is
    withdrawn before its identity is cancelled (``_withdraw_unseen``).
    """
    record = entry.record
    mark = entry.withdrawal
    if mark is None:
        return None  # not this withdrawal's: the next round decides
    key = record.key
    try:
        # ``still`` is the condition as it was before this withdrawal's own mark.
        if still is not None and not still(dataclasses.replace(entry, withdrawal=None)):
            _undo(ledger, entry)
            return Kept(key, record.operation_id)
        cancelled = _cancel_while_marked(ledger, cluster, entry, mark, withdrawer.timing)
        if not isinstance(cancelled, _Cancelled):
            return cancelled
        won = ledger.replace(entry, transitioned(record, LedgerState.WITHDRAWN, reason=mark.reason))
    except LedgerError as error:
        _undo_quietly(ledger, key, withdrawer)
        return WithdrawUnknown(key, str(error))
    return Withdrawn(key, record.operation_id, cancelled.job) if won else None


@dataclass(frozen=True, slots=True)
class _Cancelled:
    """The job is cancelled, or needs no cancel; ``job`` is the one cancelled, if one was live."""

    job: str | None


def _cancel_while_marked(
    ledger: EvaluationLedger,
    cluster: Cluster,
    entry: LedgerEntry,
    mark: WithdrawalMark,
    timing: WithdrawalTiming,
) -> _Cancelled | WithdrawOutcome | None:
    """Inspect the attempt's job and cancel it if live, each only while ``mark`` holds for it.

    The cancel is given the inspect's reading, so its scancel is its first
    transport call (``OBSERVED_CANCEL_CALLS``) and lands while the mark holds, on
    every clock. When too little of the mark is left, or the job's state or the
    cancel's outcome is unknown, the mark is undone and nothing is recorded. A
    submitting attempt no job was seen under ends here (``_withdraw_unseen``).
    """
    record = entry.record
    reason = "too little of the withdrawal's window was left to cancel"
    if timing.holds(mark, calls=_CALLS_AT_INSPECT):
        observed = None if record.state.settled else cluster.inspect(record.operation_id)
        if isinstance(observed, ClusterUnknown):
            if record.state is LedgerState.SUBMITTING:
                return _withdraw_unseen(ledger, cluster, entry, mark, observed)
            reason = observed.reason
        elif (live := live_observation(observed)) is None:
            return _Cancelled(None)  # its job ended, or is ending: nothing to stop
        elif timing.holds(mark, calls=_CALLS_AT_CANCEL):
            outcome = cluster.cancel(record.operation_id, observed=live)
            if not isinstance(outcome, ClusterUnknown):
                return _Cancelled(outcome.job_id or live.job_id)
            reason = outcome.reason
    _undo(ledger, entry)
    return WithdrawUnknown(record.key, reason)


def _withdraw_unseen(
    ledger: EvaluationLedger,
    cluster: Cluster,
    entry: LedgerEntry,
    mark: WithdrawalMark,
    observed: ClusterUnknown,
) -> WithdrawOutcome | None:
    """Withdraw a submitting attempt whose inspect found no job under its name, then cancel that name.

    With no job, there is none to cancel while the mark holds: the attempt is
    withdrawn first, and the cancel that follows has no deadline
    (``take_over_jobless``). An inspect that could not tell whether a job runs
    (a transport failure) undoes the mark and withdraws nothing, so the next
    ask resolves the attempt rather than starting a second job beside it.
    """
    record = entry.record
    withdrawn = transitioned(record, LedgerState.WITHDRAWN, reason=mark.reason)
    taken = take_over_jobless(ledger, cluster, entry, observed, withdrawn)
    if isinstance(taken, JobMayRun):
        _undo(ledger, entry)
        return WithdrawUnknown(record.key, taken.reason)
    if taken is None:
        return None
    if isinstance(taken.cancel, ClusterUnknown):
        _LOG.info(
            "evaluation ledger request %s is withdrawn; a job its submitter may yet start "
            "under it is left to the orphan check: %s",
            entry_id(record.key),
            taken.cancel.reason,
        )
        return Withdrawn(record.key, record.operation_id, None)
    return Withdrawn(record.key, record.operation_id, taken.cancel.job_id)


@dataclass(frozen=True, slots=True)
class EndedJobless:
    """The attempt ended by this call's swap; ``cancel`` is how the cancel of its name went."""

    cancel: ClusterCancelOutcome


@dataclass(frozen=True, slots=True)
class JobMayRun:
    """The inspect could not tell whether a job runs under the attempt's name; nothing changed."""

    reason: str


def take_over_jobless(
    ledger: EvaluationLedger,
    cluster: Cluster,
    entry: LedgerEntry,
    observed: ClusterInspectOutcome,
    ended: LedgerRecord,
) -> EndedJobless | JobMayRun | None:
    """End a submitting attempt no job runs under, by compare-and-swap, then cancel its name.

    ``observed`` is the inspect of the attempt's operation just taken; only
    one that found no job under its name (``ClusterUnknown.job_absent``) lets
    the attempt end as ``ended`` (failed, by a run taking over a lapsed claim;
    withdrawn, by a withdrawal). Any other reading is ``JobMayRun``, and the
    attempt stays: ending it while a job may run would let the next attempt
    start a second live job.

    The swap comes first, so a submitter that recorded its job meanwhile
    wins (None: read again) and its job is never cancelled here. Once the
    swap won, no run attaches to the attempt, and the cancel leaves a
    tombstone that keeps a late submitter from starting a job under its name;
    one that started a job anyway cancels it when its record write loses, and
    one that died first leaves it to the sweep's orphan check
    (``sweep_unleased``). Raises ``LedgerError`` when the ledger cannot be
    written.
    """
    if not isinstance(observed, ClusterUnknown) or not observed.job_absent:
        reason = (
            observed.reason
            if isinstance(observed, ClusterUnknown)
            else "a job runs under the attempt's name"
        )
        return JobMayRun(reason)
    if not ledger.replace(entry, ended):
        return None
    return EndedJobless(cluster.cancel(entry.record.operation_id))


def undo_expired_withdrawal(ledger: EvaluationLedger, entry: LedgerEntry, *, now: float) -> bool:
    """Undo the withdrawal under way on ``entry``'s attempt if it outlived its window, so its withdrawer died.

    True when this call undid it; False when there is none, it still holds, or
    another writer moved the withdrawal on since ``entry`` was read. A run
    waiting on the withdrawal and a withdrawer that finds it both call this.
    """
    mark = entry.withdrawal
    if mark is None or not withdrawal_expired(mark, now=now):
        return False
    return _undo(ledger, entry)


def _undo(ledger: EvaluationLedger, entry: LedgerEntry) -> bool:
    """Undo the withdrawal mark ``entry`` shows standing; False if another writer moved it on."""
    return ledger.write_withdrawal(entry, WithdrawalUndone(attempt=entry.record.attempt))


def _undo_quietly(ledger: EvaluationLedger, key: str, withdrawer: Withdrawer) -> None:
    """Undo this withdrawer's mark if one stands; one that cannot be undone expires."""
    try:
        entry = ledger.read(key)
        if entry is not None and withdrawer.wrote(entry.withdrawal):
            _undo(ledger, entry)
    except LedgerError:
        _LOG.warning(
            "evaluation ledger request %s may stay marked by %s for up to %.0f s",
            entry_id(key),
            withdrawer.by,
            withdrawer.timing.window_seconds,
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


def sweep_withdrawer(timing: WithdrawalTiming, *, grace_seconds: float) -> Withdrawer:
    """The withdrawer of a process's sweeps (``sweep_unleased``): build one per process, and keep it.

    Its writer id is the process's: a sweep finds a mark an earlier sweep of the
    process left standing (its write's answer lost, its undo failed) for its
    own, and finishes that withdrawal, as a run's relinquish does with its own.
    """
    return Withdrawer(
        by=SWEEP_WITHDRAWER, reason=f"no run leased it for {grace_seconds:.0f} s", timing=timing
    )


def sweep_unleased(
    ledger: EvaluationLedger,
    cluster: Cluster,
    *,
    sweeper: Withdrawer,
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
    ``sweeper`` is the process's sweep withdrawer (``sweep_withdrawer``), with
    the sweep's clock and transport bound.
    """
    timing = sweeper.timing
    now = timing.clock.now()

    def sweepable(entry: LedgerEntry) -> bool:
        now = timing.clock.now()
        record = entry.record
        if record.state.settled:
            return False
        mark = entry.withdrawal
        if mark is not None and not sweeper.wrote(mark) and not withdrawal_expired(mark, now=now):
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
            outcome = _sweep_one(ledger, cluster, entry, sweeper, still=sweepable)
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
        if holder.wrote(entry.withdrawal):
            # An earlier try of this relinquish left its mark: another run wants the
            # request now, so the mark goes, or that run waits until it expires.
            try:
                _undo(ledger, entry)
            except LedgerError as error:
                return WithdrawUnknown(key, str(error))
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
    "CLOCK_SKEW_SECONDS",
    "SWEEP_WITHDRAWER",
    "EndedJobless",
    "JobMayRun",
    "Kept",
    "NotInLedger",
    "OrphanChecks",
    "WithdrawOutcome",
    "WithdrawUnknown",
    "WithdrawalTiming",
    "Withdrawer",
    "Withdrawn",
    "relinquish",
    "settle",
    "sweep_unleased",
    "sweep_withdrawer",
    "take_over_jobless",
    "undo_expired_withdrawal",
    "withdraw",
    "withdrawal_window_seconds",
]
