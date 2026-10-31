"""The ``StepLog`` of a run: core's records appended to its run.log and folded, under one lock.

Every method appends one record through the confirmed log and folds it, so ``state`` is current
when the call returns, and a record the fold refuses is never appended. Nothing awaits between
the fold of a record and its append, so the appends of two flights, an input and the loop
never interleave: the verdict of ``step_finished`` on whether the run is parked is made on the
state its own append extends, not on the one the loop read earlier.

After each append the search hears what core published and answers (``SearchHearing``), still
inside the same step, so the loop never sees a state the search has not caught up with. A run
that parks or ends gets its search state logged whole.

A start reads ``state.pb`` if it is given a cache and folds only the records after it; the log
writes it again when the run comes to rest and now and then (``FoldKeeper``). The cache changes
no result: ``cache_start`` says whether it was used, or why the whole log was folded.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.recovery import (
    Interruption,
    RunRest,
    apply_record,
    at_rest,
    ended_message,
    finished_message,
    input_message,
    interrupted_message,
    may_start,
    open_step,
    seed_message,
    started_message,
)
from vs_runlog.api import Appended, CacheMiss
from vs_runtime._fold_keeper import CACHE_EVERY, FoldKeeper, start_fold
from vs_runtime._step_loop import Interrupted, Recorded, running_attempt

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreEvent, CoreState, OperationRegistry, RequestId
    from vs_core.api.recovery import End, Folded, OpenStep, Run, StepResult
    from vs_proto.api import Record, StrategyAdvanced
    from vs_runlog.api import ConfirmedLog, CoreMessage, FoldCache
    from vs_runtime._fold_keeper import CacheUsed
    from vs_runtime._step_loop import StepRecord
    from vs_sim.api import Clock


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
        keeper: FoldKeeper | None = None,
    ) -> None:
        """Hold ``folded``, the fold of everything ``log`` confirmed, and append as ``epoch``."""
        self._log = log
        self._folded = folded
        self._keeper = keeper
        self._registry = registry
        self._clock = clock
        self._epoch = epoch
        self._search = search
        self._stamp = stamp
        self._arrived = 0
        self._appended = 0
        self._delivered = 0
        self._wake = asyncio.Event()
        self._changed = asyncio.Event()

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
        cache: FoldCache | None = None,
        cache_every: int = CACHE_EVERY,
    ) -> RunLogStepLog:
        """Take over the run ``log`` holds, in ``epoch``, whose header the writer already appended.

        A log that holds no start of the run gets ``seed`` as its first record: the state every
        later process folds from, with the run's deadline in it. The search then hears whatever
        the dead process left it owed, and a run still to begin starts here.

        With a ``cache`` the start folds only the records after it, if it is the cache of this
        log's own first records; the log goes on writing it, every ``cache_every`` core records
        and when the run comes to rest.
        """
        with log.transaction():
            folded, keeper = start_fold(cache, log.records(), registry, every=cache_every)
            first = None
            if folded is None:
                if seed is None:
                    message = "a log with no start of the run needs the state to start it from"
                    raise ValueError(message)
                first = log.next_record(
                    seed_message(seed), epoch=epoch, at=stamp(), clock=clock.now()
                )
                folded = fold_run((first,), registry=registry)
            opened = cls(
                log, folded, registry, clock, epoch=epoch, search=search, stamp=stamp, keeper=keeper
            )
            if first is not None:
                opened._confirm(first)
        opened._settle()
        return opened

    @property
    def state(self) -> CoreState:
        """The fold of everything appended, by this process or an earlier one."""
        return self._folded.state

    @property
    def cache_start(self) -> CacheUsed | CacheMiss:
        """Whether the start used ``state.pb``, or why it folded the whole log."""
        return CacheMiss.ABSENT if self._keeper is None else self._keeper.start

    def open_step(self, tag: RequestId) -> OpenStep | None:
        """The step ``tag`` as the state has it, while it has no result."""
        return open_step(self.state, tag)

    async def step_started(self, run: Run) -> StepRecord:
        """Append ``StepStarted`` if the run still lets the step start.

        The verdict is made on the state the append extends: a pause that landed since the loop
        last looked is already in it.
        """
        with self._log.transaction():
            if not may_start(self.state, run.tag, self._clock.now()):
                return Interrupted()
            self._commit(started_message(run))
            return Recorded()

    async def step_finished(self, tag: RequestId, attempt: int, result: StepResult) -> StepRecord:
        """Append ``StepFinished``; for a parked run, ``AttemptInterrupted`` for its attempt.

        Core drops the result of a step in a parked epoch, so it is never appended there. The
        verdict is made on the state the append extends, under the lock every append takes.
        """
        with self._log.transaction():
            if at_rest(self.state) is RunRest.PARKED:
                if running_attempt(self.state, tag, attempt):
                    self._commit(interrupted_message(tag, attempt, Interruption.PAUSE))
                return Interrupted()
            self._commit(finished_message(tag, attempt, result))
            return Recorded()

    async def attempt_interrupted(self, tag: RequestId, attempt: int, why: Interruption) -> None:
        """Append ``AttemptInterrupted``."""
        with self._log.transaction():
            self._commit(interrupted_message(tag, attempt, why))

    async def run_ended(self, end: End) -> None:
        """Log the search whole, then append the end of the run.

        The whole state comes first, so the end is the last record of the run and a log that
        ends cleanly always holds the search as it finished.
        """
        with self._log.transaction():
            self._rest()
            self._commit(ended_message(end))

    def input(self, event: CoreEvent) -> None:
        """Append an input from outside the loop (an operator control, the clock) and wake it."""
        with self._log.transaction():
            self._commit(input_message(event))
            self._arrived += 1
        self._wake.set()

    @property
    def records_appended(self) -> int:
        """How many records this process appended, to tell whether a step appended any."""
        return self._appended

    def hear(self) -> None:
        """Let the search hear what is due now (a wake-up) and answer, though nothing was appended."""
        with self._log.transaction():
            before = self._appended
            self._settle()
            self._keep()
            heard = self._appended != before
            self._arrived += int(heard)
        if heard:
            self._wake.set()

    async def changed(self) -> None:
        """Return once a record was appended since this last returned, whoever appended it."""
        await self._changed.wait()
        self._changed.clear()

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
        self._keep()

    def _keep(self) -> None:
        """Write the cache if it is due."""
        if self._keeper is not None:
            self._keeper.keep(
                self._log,
                self._registry,
                appended=self._appended,
                resting=at_rest(self.state) is not None,
            )

    def _record(self, message: CoreMessage) -> None:
        """Fold the next record, and only then append it: a record the fold refuses stays out."""
        with self._log.transaction():
            record = self._log.next_record(
                message, epoch=self._epoch, at=self._stamp(), clock=self._clock.now()
            )
            self._folded = apply_record(self._folded, record, self._registry)
            self._confirm(record)

    def _confirm(self, record: Record) -> None:
        confirmed = self._log.extend(record)
        if not isinstance(confirmed, Appended):
            raise RunLogUncertainError
        self._appended += 1
        self._changed.set()

    def _settle(self) -> None:
        """The search hears what core published and answers, until it has nothing more to say."""
        search = self._search
        if search is None:
            return
        while (advance := search.advance(self._folded, self._clock.now())) is not None:
            self._record(advance)
            search.appended()
        if at_rest(self.state) is not None:
            self._rest()

    def _rest(self) -> None:
        """Log the search whole, unless the log already holds it so."""
        if self._search is not None and (rest := self._search.rest()) is not None:
            self._record(rest)
            self._search.appended()
