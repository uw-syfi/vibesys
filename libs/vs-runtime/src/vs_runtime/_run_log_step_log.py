"""The ``StepLog`` of a run: core's records appended to its run.log and folded, under one lock.

Every method appends one record through the confirmed log and folds it, so ``state`` is current
when the call returns, and a record the fold refuses is never appended. Nothing awaits between
the fold of a record and its append, so the appends of two flights, an input and the loop
never interleave: the verdict of ``step_finished`` on whether the run is parked is made on the
state its own append extends, not on the one the loop read earlier.

After each append the search hears what core published and answers (``SearchHearing``), still
inside the same step, so the loop never sees a state the search has not caught up with. A run
that parks or ends gets its search state logged whole.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api import RequestPhase
from vs_core.api.recovery import (
    Interruption,
    RunRest,
    apply_record,
    at_rest,
    finished_message,
    fold_run,
    input_message,
    interrupted_message,
    seed_message,
    started_message,
)
from vs_runlog.api import Appended
from vs_runtime._step_loop import Interrupted, OpenStep, Recorded, running_attempt

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreEvent, CoreState, OperationRegistry, RequestId
    from vs_core.api.recovery import End, Folded, Run, StepResult
    from vs_proto.api import StrategyAdvanced
    from vs_runlog.api import ConfirmedLog, CoreMessage
    from vs_runtime._step_loop import StepRecord
    from vs_sim.api import Clock

_OPEN = (RequestPhase.PREPARED, RequestPhase.DISPATCHED, RequestPhase.RECONCILING)


class RunLogUncertainError(Exception):
    """An append could not be confirmed: the record may or may not be in the log.

    Nothing that depends on it may go on. It is a crash of this process: the next one folds
    the log and learns what happened.
    """

    def __init__(self) -> None:
        """Say what happened."""
        super().__init__("the run.log could not confirm an append")


class SearchHearing(Protocol):
    """The search, as the log sees it: it answers what core published, in records of its own."""

    def advance(self, folded: Folded, now: float) -> StrategyAdvanced | None:
        """The advance record for what ``folded`` owes the search; None when it has none."""
        ...

    def rest(self) -> StrategyAdvanced | None:
        """The record that logs the search whole as the run comes to rest; None if it is."""
        ...

    def appended(self) -> None:
        """The record last returned is in the log."""
        ...


class RunLogStepLog:
    """One process's view of a run.log: it appends core's records and folds them as it goes."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-204718 [PLR0913]; the confirmed log, the fold it holds, the registry, the clock, the stamp and the search are one log's six collaborators, each used by every append.
        # > A settings object would be built at the one call site (open) and read in the same
        # > place, so it adds a type without hiding anything.
        self,
        log: ConfirmedLog,
        folded: Folded,
        registry: OperationRegistry,
        clock: Clock,
        *,
        epoch: int,
        search: SearchHearing | None,
        stamp: Callable[[], Timestamp] = Timestamp,
    ) -> None:
        """Hold ``folded``, the fold of everything ``log`` confirmed, and append as ``epoch``."""
        self._log = log
        self._folded = folded
        self._registry = registry
        self._clock = clock
        self._epoch = epoch
        self._search = search
        self._stamp = stamp
        self._arrived = 0
        self._delivered = 0
        self._wake = asyncio.Event()

    @classmethod
    def open(  # noqa: PLR0913  # lint-waiver: LW-204719 [PLR0913]; opening reads the same six things the constructor holds, plus the seed a new run starts from.
        # > Same as the constructor: one call site, and each argument is what the log is made of.
        cls,
        log: ConfirmedLog,
        *,
        epoch: int,
        seed: CoreState | None,
        registry: OperationRegistry,
        clock: Clock,
        search: SearchHearing | None = None,
        stamp: Callable[[], Timestamp] = Timestamp,
    ) -> RunLogStepLog:
        """Take over the run ``log`` holds, in ``epoch``, whose header the writer already appended.

        A log that holds no start of the run gets ``seed`` as its first record: the state every
        later process folds from, with the run's deadline in it. The search then hears whatever
        the dead process left it owed, and a run still to begin starts here.
        """
        prefix = log.records()
        started = any(r.body is not None and r.body.field == "run_started" for r in prefix)
        folded = None if not started else fold_run(prefix, registry=registry)
        opened = cls(log, folded, registry, clock, epoch=epoch, search=search, stamp=stamp)
        if folded is None:
            if seed is None:
                message = "a log with no start of the run needs the state to start it from"
                raise ValueError(message)
            opened._start(seed)
        opened._settle()
        return opened

    def _start(self, seed: CoreState) -> None:
        """Append the seed as the first record of the run; the fold begins at it."""
        record = self._log.next_record(
            seed_message(seed), epoch=self._epoch, at=self._stamp(), clock=self._clock.now()
        )
        self._folded = apply_record(None, record, self._registry)
        self._confirm(record)

    @property
    def state(self) -> CoreState:
        """The fold of everything appended, by this process or an earlier one."""
        return self._folded.state

    def open_step(self, tag: RequestId) -> OpenStep | None:
        """The step ``tag`` as the state has it, while it has no result."""
        record = self.state.records.record(tag)
        if record is None or record.phase not in _OPEN:
            return None
        attempts = self.state.steps.of(tag)
        started = 0 if attempts is None else attempts.started
        return OpenStep(record.request, record.until.at, started)

    async def step_started(self, run: Run) -> None:
        """Append ``StepStarted``."""
        self._commit(started_message(run))

    async def step_finished(
        self, tag: RequestId, attempt: int, result: StepResult
    ) -> StepRecord:
        """Append ``StepFinished``; for a parked run, ``AttemptInterrupted`` for its attempt."""
        if at_rest(self.state) is RunRest.PARKED:
            if running_attempt(self.state, tag, attempt):
                self._commit(interrupted_message(tag, attempt, Interruption.PAUSE))
            return Interrupted()
        self._commit(finished_message(tag, attempt, result))
        return Recorded()

    async def attempt_interrupted(self, tag: RequestId, attempt: int, why: Interruption) -> None:
        """Append ``AttemptInterrupted``."""
        self._commit(interrupted_message(tag, attempt, why))

    async def run_ended(self, end: End) -> None:
        """Log the search whole, then append the end of the run."""
        self._settle()
        self._ended(end)

    def input(self, event: CoreEvent) -> None:
        """Append an input from outside the loop (an operator control, the clock) and wake it."""
        self._commit(input_message(event))
        self._arrived += 1
        self._wake.set()

    async def next_input(self) -> None:
        """Return once ``input`` was called since this last returned."""
        while self._delivered == self._arrived:
            self._wake.clear()
            await self._wake.wait()
        self._delivered = self._arrived

    def stalled(self) -> str | None:
        """Never: an input from outside can always arrive, and the owner of this log sends it."""
        return None

    def _commit(self, message: CoreMessage) -> None:
        """Append one record, then let the search hear what it caused."""
        self._record(message)
        self._settle()

    def _record(self, message: CoreMessage) -> None:
        """Fold the next record, and only then append it: a record the fold refuses stays out."""
        record = self._log.next_record(
            message, epoch=self._epoch, at=self._stamp(), clock=self._clock.now()
        )
        folded = apply_record(self._folded, record, self._registry)
        assert folded is not None  # a log with a start is always folded
        self._folded = folded
        self._confirm(record)

    def _confirm(self, record: object) -> None:
        confirmed = self._log.extend(record)  # type: ignore[arg-type]
        if not isinstance(confirmed, Appended):
            raise RunLogUncertainError

    def _settle(self) -> None:
        """The search hears what core published and answers, until it has nothing more to say."""
        search = self._search
        if search is None:
            return
        while (advance := search.advance(self._folded, self._clock.now())) is not None:
            self._record(advance)
            search.appended()
        if at_rest(self.state) is not None and (rest := search.rest()) is not None:
            self._record(rest)
            search.appended()

    def _ended(self, end: End) -> None:
        raise NotImplementedError(end)
