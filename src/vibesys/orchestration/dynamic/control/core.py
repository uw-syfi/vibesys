"""Deterministic scheduling core of the dynamic loop: events and actions in, effects out.

``HostCore`` owns slot accounting, the ready queue, the start budget, stop and
end-of-search, and the retry decision for finished workers. It never awaits,
performs no I/O and reads no clock: every input carries its time, and every
consequence is returned as an effect for the async shell to execute.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum


class WorkerOutcome(StrEnum):
    """How one worker task ended, as classified by the shell from durable state."""

    COMPLETED = "completed"  # Finished and recorded its own result.
    REFUNDED = "refunded"  # Finished without using its start (an unsupported profile).
    RETRYABLE = "retryable"  # The attempt failed and its retry budget remains.
    EXHAUSTED = "exhausted"  # The attempt failed with its retry budget spent.
    FATAL = "fatal"  # An error the loop cannot absorb; the run stops.


class StopReason(StrEnum):
    """Why the core stopped starting work."""

    REQUESTED = "requested"  # An operator stop (or pause failure) landed at a checkpoint.
    TURN_FAULTS_EXHAUSTED = "turn_faults_exhausted"  # Driver turns faulted ``turn_attempts`` times.
    WORKER_FAILED = "worker_failed"  # A worker ended ``FATAL``.


class Attempt(StrEnum):
    """Why a ``StartWorker`` effect starts a worker."""

    NEW = "new"  # Submitted by the driver; charged one start.
    RECOVERED = "recovered"  # Durably scheduled before a restart; already charged.
    RETRY = "retry"  # Its previous attempt failed with retry budget left.


class SearchEnd(StrEnum):
    """How the search ended; the shell derives the run status from it."""

    FINISHED = "finished"  # The driver finished and every started worker settled.
    STOPPED = "stopped"  # A stop landed and every started worker settled.


class Refusal(StrEnum):
    """Why an action was refused; a refused action changes nothing."""

    STOPPED = "stopped"
    SEARCH_FINISHED = "search_finished"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class WorkItem[P]:
    """One workstream the core schedules: a unique id and the driver's opaque plan."""

    worker_id: str
    plan: P


# Events: facts the shell observed.


@dataclass(frozen=True, slots=True)
class WorkerFinished:
    """A started worker's task ended with ``outcome`` at run-elapsed ``at_s``."""

    worker_id: str
    outcome: WorkerOutcome
    at_s: float


@dataclass(frozen=True, slots=True)
class StopRequested:
    """Stop starting work at ``at_s``; started workers drain."""

    reason: StopReason
    at_s: float


@dataclass(frozen=True, slots=True)
class TurnFaulted:
    """A driver turn failed at ``at_s`` (crash, timeout, or a reply still invalid)."""

    at_s: float


type HostEvent = WorkerFinished | StopRequested | TurnFaulted


# Actions: decisions a driver asks the core to apply.


@dataclass(frozen=True, slots=True)
class Recover[P]:
    """Resume workers durably scheduled before a restart; they are already charged."""

    items: tuple[WorkItem[P], ...]
    at_s: float


@dataclass(frozen=True, slots=True)
class Submit[P]:
    """Start new workers, one budget unit each; those without a free slot queue."""

    items: tuple[WorkItem[P], ...]
    at_s: float


@dataclass(frozen=True, slots=True)
class FinishSearch:
    """The driver will submit nothing more; the search ends once started work settles."""

    at_s: float


type HostAction[P] = Recover[P] | Submit[P] | FinishSearch


@dataclass(frozen=True, slots=True)
class Accepted:
    """The action applied; ``started`` got a slot now, ``queued`` waits for one."""

    started: tuple[str, ...] = ()
    queued: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Refused:
    """The action changed nothing; ``worker_id`` names the offending item, if any."""

    code: Refusal
    worker_id: str | None = None


# Effects: what the shell must do.


@dataclass(frozen=True, slots=True)
class StartWorker[P]:
    """Start a task for ``item``; it occupies a slot until ``WorkerFinished``."""

    item: WorkItem[P]
    attempt: Attempt


@dataclass(frozen=True, slots=True)
class RecordGiveUp[P]:
    """Durably mark ``item`` failed with its retry budget spent."""

    item: WorkItem[P]


@dataclass(frozen=True, slots=True)
class EndSearch:
    """Nothing runs or will start; the shell leaves the loop."""

    end: SearchEnd
    stop: StopReason | None


type Effect[P] = StartWorker[P] | RecordGiveUp[P] | EndSearch


@dataclass(frozen=True, slots=True)
class HostLimits:
    """Hard limits the core enforces.

    ``start_budget`` is how many more workers the driver may submit, counted
    from durable state when the run opens; a ``REFUNDED`` outcome returns one.
    ``turn_attempts`` bounds consecutive faulted driver turns, the same bound
    a workstream gets for its own attempts (``max_retries_per_round``): a
    turn fault is retried until that many turns in a row faulted.
    """

    max_in_flight: int
    start_budget: int
    turn_attempts: int = 1

    def __post_init__(self) -> None:
        """Reject limits no schedule can satisfy."""
        for name in ("max_in_flight", "turn_attempts"):
            value = getattr(self, name)
            if value < 1:
                message = f"{name} must be at least 1, got {value}"
                raise ValueError(message)


@dataclass(slots=True)
class _Slot[P]:
    item: WorkItem[P]
    since_s: float


@dataclass(slots=True)
class HostCore[P]:
    """Pure scheduling state machine shared by every driver.

    Guarantees, for any interleaving of inputs:

    - at most ``max_in_flight`` workers run; a start without a free slot
      queues, and a freed slot starts the queue head before anything else;
    - submitted starts never exceed the start budget (refunds included);
    - after a stop, no effect starts work, and the queue is dropped (its
      items stay durable for resume);
    - every started worker settles exactly once: a retry restarts it in the
      same slot, any other outcome frees the slot;
    - a faulted driver turn stops the search only once ``turn_attempts``
      turns in a row faulted; an accepted submit resets the count;
    - ``EndSearch`` is emitted exactly once, when nothing runs or waits and
      the search is stopped or finished.

    A ``WorkerFinished`` for an id that is not running is a shell bug and
    raises ``ValueError``; so does time running backwards.
    """

    limits: HostLimits
    _running: dict[str, _Slot[P]] = field(default_factory=dict)
    _queue: deque[tuple[WorkItem[P], Attempt]] = field(default_factory=deque)
    _spent: int = 0
    _refunded: int = 0
    _stop: StopReason | None = None
    _finishing: bool = False
    _ended: bool = False
    _now_s: float = 0.0
    _slot_seconds: float = 0.0
    _turn_faults: int = 0

    @property
    def running(self) -> frozenset[str]:
        """Ids of the workers that hold a slot."""
        return frozenset(self._running)

    @property
    def queued(self) -> tuple[str, ...]:
        """Ids waiting for a slot, in start order."""
        return tuple(item.worker_id for item, _ in self._queue)

    @property
    def remaining_budget(self) -> int:
        """How many more workers may be submitted."""
        return self.limits.start_budget - self._spent + self._refunded

    @property
    def free_capacity(self) -> int:
        """How many workers a submit could start now: free slots within the budget."""
        if self._stop is not None or self._finishing:
            return 0
        free = self.limits.max_in_flight - len(self._running) - len(self._queue)
        return max(0, min(free, self.remaining_budget))

    @property
    def stopped(self) -> StopReason | None:
        """Why the core stopped, or ``None`` while it may start work."""
        return self._stop

    @property
    def ended(self) -> bool:
        """Whether ``EndSearch`` was emitted."""
        return self._ended

    @property
    def turn_faults(self) -> int:
        """Consecutive faulted driver turns since the last accepted submit."""
        return self._turn_faults

    @property
    def slot_seconds(self) -> float:
        """Slot occupancy of settled workers, in seconds of injected time."""
        return self._slot_seconds

    def on_event(self, event: HostEvent) -> tuple[Effect[P], ...]:
        """Apply one observed fact and return the effects it requires."""
        self._advance(event.at_s)
        match event:
            case WorkerFinished():
                return self._finished(event)
            case StopRequested():
                self._halt(event.reason)
                return self._maybe_end()
            case TurnFaulted():
                self._turn_faults += 1
                if self._turn_faults >= self.limits.turn_attempts:
                    self._halt(StopReason.TURN_FAULTS_EXHAUSTED)
                return self._maybe_end()

    def on_action(self, action: HostAction[P]) -> tuple[Accepted | Refused, tuple[Effect[P], ...]]:
        """Apply a driver decision; a refusal returns no effects and changes nothing."""
        self._check_time(action.at_s)
        if self._stop is not None:
            return Refused(Refusal.STOPPED), ()
        if self._finishing:
            return Refused(Refusal.SEARCH_FINISHED), ()
        match action:
            case FinishSearch():
                self._advance(action.at_s)
                self._finishing = True
                return Accepted(), self._maybe_end()
            case Recover():
                return self._admit(action.items, action.at_s, Attempt.RECOVERED, charge=False)
            case Submit():
                return self._admit(action.items, action.at_s, Attempt.NEW, charge=True)

    def _admit(
        self, items: tuple[WorkItem[P], ...], at_s: float, attempt: Attempt, *, charge: bool
    ) -> tuple[Accepted | Refused, tuple[Effect[P], ...]]:
        seen = set(self._running) | set(self.queued)
        for item in items:
            if item.worker_id in seen:
                return Refused(Refusal.DUPLICATE, item.worker_id), ()
            seen.add(item.worker_id)
        if charge and len(items) > self.remaining_budget:
            return Refused(Refusal.BUDGET_EXHAUSTED), ()
        self._advance(at_s)
        if charge:
            self._spent += len(items)
            self._turn_faults = 0
        effects: list[Effect[P]] = []
        started: list[str] = []
        queued: list[str] = []
        for item in items:
            if len(self._running) < self.limits.max_in_flight and not self._queue:
                self._running[item.worker_id] = _Slot(item, at_s)
                effects.append(StartWorker(item, attempt))
                started.append(item.worker_id)
            else:
                self._queue.append((item, attempt))
                queued.append(item.worker_id)
        return Accepted(tuple(started), tuple(queued)), tuple(effects)

    def _finished(self, event: WorkerFinished) -> tuple[Effect[P], ...]:
        slot = self._running.get(event.worker_id)
        if slot is None:
            message = f"worker {event.worker_id!r} finished but holds no slot"
            raise ValueError(message)
        if event.outcome is WorkerOutcome.RETRYABLE and self._stop is None:
            # The retry keeps its slot; its failed attempt is already charged
            # to the worker's own durable retry budget.
            return (StartWorker(slot.item, Attempt.RETRY),)
        del self._running[event.worker_id]
        self._slot_seconds += event.at_s - slot.since_s
        effects: list[Effect[P]] = []
        match event.outcome:
            case WorkerOutcome.REFUNDED:
                self._refunded += 1
            case WorkerOutcome.EXHAUSTED:
                effects.append(RecordGiveUp(slot.item))
            case WorkerOutcome.FATAL:
                self._halt(StopReason.WORKER_FAILED)
            case WorkerOutcome.COMPLETED | WorkerOutcome.RETRYABLE:
                pass
        if self._stop is None and self._queue:
            head, attempt = self._queue.popleft()
            self._running[head.worker_id] = _Slot(head, event.at_s)
            effects.append(StartWorker(head, attempt))
        return (*effects, *self._maybe_end())

    def _halt(self, reason: StopReason) -> None:
        if self._stop is None:
            self._stop = reason
            self._queue.clear()

    def _maybe_end(self) -> tuple[Effect[P], ...]:
        if self._ended or self._running or self._queue:
            return ()
        if self._stop is not None:
            self._ended = True
            return (EndSearch(SearchEnd.STOPPED, self._stop),)
        if self._finishing:
            self._ended = True
            return (EndSearch(SearchEnd.FINISHED, None),)
        return ()

    def _check_time(self, at_s: float) -> None:
        if at_s < self._now_s:
            message = f"time ran backwards: {at_s} after {self._now_s}"
            raise ValueError(message)

    def _advance(self, at_s: float) -> None:
        self._check_time(at_s)
        self._now_s = at_s
