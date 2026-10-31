"""What reaches core besides the results of steps: the search, the operator and the clock.

``CoreInputs`` wraps the run's ``StepLog``. Every method passes through to the log, except the
two the loop sleeps on: ``next_input`` also reads the operator's controls, stops the run at its
deadline, delivers the clock to core when that changes anything and wakes the search when it
asked to be, and ``stalled`` says why a run that waits on nothing can never go on. The search
itself is heard by the log, after each append, through ``SearchRun``.

A run is never left silent: with no runner in flight, no timer and no wake-up to come, the
loop asks ``stalled`` and raises what it answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import TYPE_CHECKING, Any, Protocol

from vs_core.api import (
    ClockAdvanced,
    ContinuationPhase,
    ControlId,
    ControlInput,
    ControlRefusedError,
    RunControlEvent,
    RunStatus,
    SearchDriver,
    SessionPhase,
    StopMode,
    StrategyState,
    orphan_waits,
    project,
    step,
)
from vs_core.api.recovery import (
    Advancing,
    Progress,
    advance_run_time,
    advance_search,
    checkpoint_search,
)
from vs_runtime._saved_run_record import restore_log

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from vs_core.api import CoreEvent, CoreState, OperationRegistry, RequestId, Transition
    from vs_core.api.recovery import (
        End,
        Folded,
        Interruption,
        OpenStep,
        RecordRefusal,
        Run,
        StepResult,
    )
    from vs_proto.api import Record, StrategyAdvanced
    from vs_runtime._run_log_step_log import RunLogStepLog
    from vs_runtime._step_loop import StepRecord
    from vs_sim.api import SleepingClock


class Controls(Protocol):
    """The operator's side of a run: what the bridge of ``vs_runtime.api.control`` offers."""

    def poll(self, core: CoreState, now_at: float, /) -> tuple[CoreEvent, ...]:
        """The controls requested since the last poll, as core events."""
        ...

    def settled(
        self, core: CoreState, /, *, running: bool, refused: Sequence[ControlRefusedError] = ()
    ) -> None:
        """Tell the operator what core committed and which of its controls it refused."""
        ...


class SearchRun[S: StrategyState, C]:
    """The search of one run as the log hears it: its progress, and what to append next."""

    def __init__(
        self,
        search: SearchDriver[S, C],
        config: C,
        registry: OperationRegistry,
        progress: Progress[S] | None,
    ) -> None:
        """Run ``search`` on ``config``, from the ``progress`` the log's records leave."""
        self._search = search
        self._config = config
        self._registry = registry
        self._progress = progress
        self._pending: Advancing[S] | None = None

    @classmethod
    def restored(
        cls,
        records: Sequence[Record],
        search: SearchDriver[S, C],
        config: C,
        registry: OperationRegistry,
    ) -> SearchRun[S, C]:
        """The search as the log's advance records left it; fresh when they hold none."""
        with _search_generators():
            progress = restore_log(records, search, registry)
        return cls(search, config, registry, progress)

    @property
    def progress(self) -> Progress[S] | None:
        """Where the search stands, None before its first advance."""
        return self._progress

    def advance(self, folded: Folded, now: float) -> StrategyAdvanced | None:
        """The advance for what ``folded`` owes the search and the wake-ups due at ``now``."""
        with _search_generators():
            self._pending = advance_search(
                self._search,
                self._config,
                self._progress,
                folded.state,
                folded.owed,
                now,
                self._registry,
            )
        return None if self._pending is None else self._pending.record

    def rest(self) -> StrategyAdvanced | None:
        """The record that logs the search whole as the run parks or ends; None when it is."""
        self._pending = None if self._progress is None else checkpoint_search(self._progress)
        return None if self._pending is None else self._pending.record

    def appended(self) -> None:
        """The record last returned is in the log: the search stands where it left it."""
        if self._pending is not None:
            self._progress = self._pending.progress
            self._pending = None


class CoreInputs:
    """A ``StepLog`` that also brings core the operator, the clock and the search's wake-ups."""

    def __init__(
        self,
        log: RunLogStepLog,
        search: SearchRun[Any, Any] | None,
        clock: SleepingClock,
        *,
        controls: Controls | None = None,
        control_poll_seconds: float = 1.0,
    ) -> None:
        """Wrap ``log``; ``controls`` is polled every ``control_poll_seconds`` while the loop waits."""
        self._log = log
        self._search = search
        self._clock = clock
        self._controls = controls
        self._poll_every = control_poll_seconds
        self._refused: list[ControlRefusedError] = []
        self._offered: list[CoreEvent] = []
        self._nudge = asyncio.Event()

    @property
    def state(self) -> CoreState:
        """The state the log folds to."""
        return self._log.state

    def open_step(self, tag: RequestId) -> OpenStep | None:
        """The step ``tag`` as the log has it, while it has no result."""
        return self._log.open_step(tag)

    async def step_started(self, run: Run) -> Run | RecordRefusal:
        """Record that an attempt is about to start."""
        return await self._log.step_started(run)

    async def step_finished(self, tag: RequestId, attempt: int, result: StepResult) -> StepRecord:
        """Record the one result of a step."""
        return await self._log.step_finished(tag, attempt, result)

    async def attempt_interrupted(self, tag: RequestId, attempt: int, why: Interruption) -> None:
        """Record that an attempt ended and its step did not."""
        await self._log.attempt_interrupted(tag, attempt, why)

    async def clock_advanced(self, at: float) -> None:
        """Record that the run clock reached ``at``."""
        await self._log.clock_advanced(at)

    async def run_ended(self, end: End) -> None:
        """Record the end of the run."""
        await self._log.run_ended(end)

    def admit(self, event: CoreEvent) -> Transition:
        """Commit an input a tool admitted mid-turn, and return core's answer to it.

        Core's answer is what the tool shows the agent, so the record is in the log before this
        returns. An input core refuses raises and is not recorded.
        """
        transition = step(self.state, event)
        self._log.input(event)
        return transition

    def leave(self) -> None:
        """Tell the operator what core committed and refused, once the loop has returned."""
        self._settle_controls()

    def offer(self, event: CoreEvent) -> None:
        """An input from outside the loop (an operator's control): appended at the next poll."""
        self._offered.append(event)
        self._nudge.set()

    async def next_input(self) -> None:
        """Return once an input was appended, by an operator, the clock or a wake-up included."""
        arrived = asyncio.ensure_future(self._log.next_input())
        try:
            while True:
                self._poll()
                waits: set[asyncio.Future[Any]] = {
                    arrived,
                    asyncio.ensure_future(self._log.changed()),
                    asyncio.ensure_future(self._nudge.wait()),
                }
                timer = self._due()
                if timer is not None:
                    waits.add(asyncio.ensure_future(self._clock.sleep(timer)))
                try:
                    await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for wait in waits - {arrived}:
                        wait.cancel()
                    await asyncio.gather(*(waits - {arrived}), return_exceptions=True)
                self._nudge.clear()
                if arrived.done():
                    return
        finally:
            arrived.cancel()
            await asyncio.gather(arrived, return_exceptions=True)

    def stalled(self) -> str | None:
        """Why the run can never go on, or None while a timer, a wake-up or the operator can move it.

        What is due now is brought in first (the clock, the deadline, the operator, the search's
        wake-ups), so a timer that came due is never reported as a stall: if that appended
        anything the run is not stalled, and the loop decides again. A paused run waits for its
        operator, and a run with a timer ahead (``_timers``: a search wake-up, core's clock, the
        deadline of a live run) waits for it. Any other run is stuck on the search, which stopped
        proposing, or on something core holds open that nothing ends.
        """
        before = self._log.records_appended
        self._poll()
        if self._log.records_appended != before:
            return None
        state = self.state
        if state.run.status in (RunStatus.PAUSED, RunStatus.DRAINING, RunStatus.PARKED):
            return None
        now = self._clock.now()
        if any(at > now for at in self._timers()):
            return None
        return _describe(state)

    def _poll(self) -> None:
        """Append what the operator, the deadline, the clock and the search are due to bring."""
        now = self._clock.now()
        self._controls_in(now)
        self._deadline(now)
        self._tick(now)
        self._log.hear()
        self._settle_controls()

    def _controls_in(self, now: float) -> None:
        offered, self._offered = self._offered, []
        polled = () if self._controls is None else self._controls.poll(self.state, now)
        for event in (*offered, *polled):
            try:
                self._log.input(event)
            except ControlRefusedError as refusal:
                self._refused.append(refusal)

    def _settle_controls(self) -> None:
        if self._controls is None:
            return
        refused, self._refused = tuple(self._refused), []
        running = any(not row.ended for row in self.state.steps.attempts)
        self._controls.settled(self.state, running=running, refused=refused)

    def _deadline(self, now: float) -> None:
        """Stop the run, draining, when its deadline passes; once per epoch."""
        at = self._deadline_at()
        if at is not None and now >= at:
            control = ControlInput(
                control_id=self._deadline_control(), action="stop", mode=StopMode.DRAIN
            )
            self._log.input(RunControlEvent(control=control, now_at=now))

    def _deadline_control(self) -> ControlId:
        return ControlId(root=f"deadline:{self.state.steps.epoch}")

    def _deadline_at(self) -> float | None:
        """When the deadline stops the run, None once it cannot: the run is not live, has its
        result, or this epoch's deadline control is already in."""
        state = self.state
        identity = self._deadline_control()
        live = state.run.status in (RunStatus.RUNNING, RunStatus.PAUSED)
        if (
            live
            and state.run.result is None
            and all(control.control_id != identity for control in state.run.controls)
        ):
            return state.run.deadline_at
        return None

    def _timers(self) -> tuple[float, ...]:
        """The times at which the run moves with no one's help: the single definition of "waiting".

        The search's next wake-up, the time core asked for (a paced poll, a wait's timer, a
        request's retry) and the deadline that stops a live run. ``stalled`` and ``_due`` both
        read it, so a run with no work in flight and a deadline ahead is waiting for it in both.
        """
        progress = None if self._search is None else self._search.progress
        times = (
            None if progress is None else progress.next_wake(),
            project(self.state).next_observe_at,
            self._deadline_at(),
        )
        return tuple(at for at in times if at is not None)

    def _tick(self, now: float) -> None:
        """Deliver the clock to core once a time it asked for has come, if that changes anything.

        The time core asks for is ``CoreView.next_observe_at``: a paced poll, a wait's timer, a
        request's retry. A delivery that changes nothing is not appended, so a run whose timer
        is due and moves nothing is a stall to report, not a record to write forever.
        """
        due = project(self.state).next_observe_at
        if due is None or due > now:
            return
        moved = advance_run_time(self.state, now)
        advanced = step(moved, ClockAdvanced(now_at=now))
        if advanced.requests or advanced.events or advanced.state != moved:
            self._log.input(ClockAdvanced(now_at=now))

    def _due(self) -> float | None:
        """Seconds until the next thing this wrapper must look at, None when none is set."""
        now = self._clock.now()
        times = self._timers()
        if self._controls is not None:
            times = (*times, now + self._poll_every)
        return min((t - now for t in times if t > now), default=None)


def _describe(core: CoreState) -> str:
    """Why nothing can proceed: the open records, then every wait nothing will end.

    A stalled run always names a cause. When core holds no open record and no orphaned wait, the
    search has unfinished work core is not waiting on, so the report lists the attempts,
    sessions and continuations that have not ended.
    """
    open_records = [
        f"{record.request.kind}:{record.phase.value}"
        for record in core.records.records
        if record.phase.value != "completed"
    ]
    text = (
        f"run {core.run.run_id.root} stalled at core revision {core.revision} "
        f"with status {core.run.status.value}; open records {open_records}"
    )
    orphans = [f"{wait.waiter.value} {wait.subject}" for wait in orphan_waits(core)]
    if orphans:
        text += f"; waits nothing will end {orphans}"
    if not open_records and not orphans:
        text += f"; {_unfinished(core)}"
    return text


def _unfinished(core: CoreState) -> str:
    """What has not ended when core waits on nothing: the work left to the search."""
    attempts = [
        f"{row.attempt_id.root} {row.phase.value}"
        for row in core.attempts.attempts
        if row.phase.value != "terminal"
    ]
    sessions = [
        f"{row.spec.session_id.root} {row.phase.value}"
        for row in core.sessions.sessions
        if row.phase not in (SessionPhase.TERMINAL, SessionPhase.IDLE)
    ]
    continuations = [
        f"{row.continuation_id.root} {row.phase.value}"
        for row in core.evaluation.continuations
        if row.phase in (ContinuationPhase.WAITING, ContinuationPhase.AUTHORIZED)
    ]
    return (
        "core waits on nothing, so the search is the one that has stopped proposing; "
        f"unfinished attempts {attempts}, sessions {sessions}, continuations {continuations}"
    )


def _drop(_generator: object) -> None:
    """Finalize an abandoned search generator by running none of its code."""


@contextlib.contextmanager
def _search_generators() -> Iterator[None]:
    """Keep the async generators a search starts (a flow's ``@resource`` bodies) from the loop.

    The search runs inside the loop's thread but is not driven by it. The loop registers every
    async generator first iterated inside it and closes the ones still alive at shutdown by
    running their release as a loop task, where each ``ctx`` wait is an awaitable the loop
    cannot run. A search's generator outlives its flow only with its run, when its process died,
    and a dead process's search must start no more work: the next one settles what it held
    from the log. So no loop hears of it, and it is finalized by running nothing.
    """
    previous = sys.get_asyncgen_hooks()
    sys.set_asyncgen_hooks(firstiter=None, finalizer=_drop)
    try:
        yield
    finally:
        sys.set_asyncgen_hooks(*previous)
