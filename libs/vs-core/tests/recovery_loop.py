"""The runner loop as the recovery properties model it: the records it writes, and the inputs it takes.

The model writes, for each action `decide` returns, the record the loop writes (a step started, a
step finished, an attempt interrupted, the run ended), and plays what happens around the loop: an
operator's pause, stop and resume, a quota pause, a clock that jumps and is folded as an input, a
runner that finishes or raises, a process that dies. The loop does not catch up after every move
unless the trajectory says so (it lags), so the state a move leaves is the state the next input
meets with the loop's attempts still running.

Invariants are the functions at the end. They hold in every state the model reaches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Action,
    Cancel,
    Cancelled,
    CancelReason,
    End,
    Finished,
    Interruption,
    RecordRefusal,
    RecordRefusedError,
    RefusalReason,
    Run,
    Settle,
    StepEffect,
    Tick,
    Wait,
    decide,
    ended_message,
    finished_message,
    fold,
    input_message,
    interrupted_message,
    interruption_of,
    may_start,
    request_effect,
    start_due,
    started_message,
)
from vs_proto.api import EpochStarted, Oneof, Record, Timestamp

if TYPE_CHECKING:
    from vs_proto.api import (
        AttemptInterrupted,
        CoreInput,
        RunEnded,
        StepFinished,
        StepStarted,
    )

type Body = (
    Oneof[Literal["epoch_started"], EpochStarted]
    | Oneof[Literal["core_input"], CoreInput]
    | Oneof[Literal["step_started"], StepStarted]
    | Oneof[Literal["step_finished"], StepFinished]
    | Oneof[Literal["attempt_interrupted"], AttemptInterrupted]
    | Oneof[Literal["run_ended"], RunEnded]
)


class RunDidNotRestError(AssertionError):
    """The cooperative loop kept finding work."""

    def __init__(self) -> None:
        super().__init__("the run did not come to rest")


class RefusedRecordError(AssertionError):
    """The fold refused the record of an action `decide` returned."""

    def __init__(self, action: Action, error: RecordRefusedError) -> None:
        super().__init__(f"the fold refused the record of {action!r}: {error}")


class UnservedStepError(AssertionError):
    """Core opened a step that no runner serves."""

    def __init__(self, row: core.RequestRecord) -> None:
        super().__init__(
            f"core opened {row.request.kind} {row.request_id.root}, which no runner serves"
        )


class IneffectiveControlError(AssertionError):
    """Core took a control into the log and the run is as it was: nobody was told it did nothing."""

    def __init__(self, control: core.ControlInput, status: core.RunStatus) -> None:
        super().__init__(f"{control.action} was taken and the run is {status.value}")


@dataclass
class Loop:
    """The records the runner loop writes for each action, and the state they fold to."""

    state: core.CoreState
    registry: core.OperationRegistry
    records: list[Record] = field(default_factory=list)
    epoch: int = 0
    clock: float = 0.0
    operator: int = 0
    refusals: list[core.ControlRefusedError] = field(default_factory=list)
    """The controls core refused, which the sender hears about and the log never holds."""
    deadlines: dict[core.RequestId, float] = field(default_factory=dict)
    """The limit each step was first started with: a step's limit is its own, whatever happens."""
    reopened: int = 0
    """How many records the log held when a resume last reopened an ended run."""

    def add(self, body: Body) -> None:
        record = Record(
            seq=len(self.records) + 1,
            epoch=self.epoch,
            at=Timestamp(seconds=len(self.records) + 1),
            clock=self.clock,
            body=body,
        )
        folded = fold([record], self.state, registry=self.registry)
        self.records.append(record)
        self.state = folded

    def open_epoch(self) -> None:
        """The process died, or a new one started: its first record opens the next epoch.

        What a parked run held ended with the pause, so no attempt of it is charged like a crash.
        """
        parked = self.state.run.status is core.RunStatus.PARKED
        owed = {row.tag: row.charged - (0 if row.ended else 1) for row in self.state.steps.attempts}
        self.epoch += 1
        self.add(Oneof("epoch_started", EpochStarted(epoch=self.epoch)))
        if parked:
            after = {row.tag: row.charged for row in self.state.steps.attempts}
            assert after == owed, "an attempt a parked run held was charged to the step"

    def running(self) -> list[core.RequestId]:
        return [row.tag for row in self.state.steps.attempts if not row.ended]

    def body_of(self, action: Action) -> Body | None:
        match action:
            case Run():
                return Oneof("step_started", started_message(action))
            case Cancel(tag=tag, reason=reason):
                return self._ended(tag, reason)
            case Settle(tag=tag, result=result):
                return Oneof("step_finished", finished_message(tag, self._attempt(tag), result))
            case Tick(at=at):
                return Oneof("core_input", input_message(core.ClockAdvanced(now_at=at)))
            case End():
                return Oneof("run_ended", ended_message(action))
            case Wait():
                return None

    def _attempt(self, tag: core.RequestId) -> int:
        started = self.state.steps.of(tag)
        return 0 if started is None else started.started

    def _ended(self, tag: core.RequestId, reason: CancelReason) -> Body:
        """How a cancelled attempt ends: interrupted, or the step's cancelled result."""
        why = interruption_of(reason)
        if why is not None:
            return Oneof("attempt_interrupted", interrupted_message(tag, self._attempt(tag), why))
        return Oneof(
            "step_finished", finished_message(tag, self._attempt(tag), Cancelled(reason=reason))
        )

    def apply(self, action: Action) -> None:
        """Record what the loop records for ``action``; the state must move."""
        if isinstance(action, Wait):
            assert action.until is None or action.until > self.clock, action
            return
        body = self.body_of(action)
        assert body is not None
        if isinstance(action, Run):
            self.closes_only_a_finished_scope(action)
            self.runs_only_what_a_runner_serves(action)
        before = self.state
        try:
            self.add(body)
        except RecordRefusedError as error:
            raise RefusedRecordError(action, error) from error
        assert self.state != before, f"{action!r} was recorded and changed nothing"
        if isinstance(action, Run):
            self.deadlines.setdefault(action.tag, action.deadline_at)
        if isinstance(action, End):
            assert self.state.run.status is core.RunStatus.TERMINAL, (
                "the end was recorded and the run is not over"
            )

    def runs_only_what_a_runner_serves(self, run: Run) -> None:
        """The rule never starts a request of the prove-or-block layer: no runner serves one.

        A step of such a kind has nothing to clear and nothing to run, so the loop waits on it for
        good and the run it holds open never ends.
        """
        assert not isinstance(run.step, RECOVERY_ONLY), (
            f"the rule starts {run.step.kind}, which no runner serves"
        )

    def closes_only_a_finished_scope(self, run: Run) -> None:
        """A scope is closed when every other step of it has a result, and not before."""
        if request_effect(run.step, self.state.registry) is not StepEffect.CLOSES:
            return
        open_beside = [
            row.request_id.root
            for row in self.state.records.records
            if row.phase in OPEN
            and row.request.scope == run.step.scope
            and row.request_id != run.tag
            and run.tag not in row.request.depends_on
        ]
        assert not open_beside, f"the scope is closed with {open_beside} still open"

    def tick(self) -> int:
        """Apply every action `decide` returns now; how many were not a wait."""
        actions = decide(self.state, self.clock)
        done = 0
        for action in actions:
            if isinstance(action, Run) and not may_start(self.state, action.tag, self.clock):
                continue  # an action before it moved the run: the flight asks again, and waits
            self.apply(action)
            done += not isinstance(action, Wait)
        return done

    def finish_running(self, index: int) -> None:
        running = self.running()
        if not running:
            return
        tag = running[index % len(running)]
        record = self.state.records.record(tag)
        assert record is not None
        started = self.state.steps.of(tag)
        assert started is not None
        self.add(
            Oneof(
                "step_finished",
                finished_message(tag, started.started, self.success(tag, record.request)),
            )
        )

    def success(self, tag: core.RequestId, request: core.Request) -> Finished:
        """What a runner reports for a step that worked: a job names the resource it released."""
        record = self.state.records.record(tag)
        owned = record is not None and record.lifecycle is core.LifecycleClass.OWNED_JOB
        observation = core.Observation(
            event_id=core.EventId(root=f"done-{tag.root}-{len(self.records)}"),
            request_id=tag,
            scope=request.scope,
            admission_id=request.admission_id,
            sequence=0,
            observed_at=self.clock,
            status=core.ObservationStatus.SUCCEEDED,
            resource_id=core.ResourceId(root=f"resource-{tag.root}") if owned else None,
            accepted=True,
            terminal=True,
            released=True,
            children_complete=True,
        )
        return Finished(observation=observation)

    def finish_late(self, index: int) -> bool:
        """A runner reports a finish of an attempt that was replaced; whether one could be."""
        late = [
            tag
            for tag in self.running()
            if (row := self.state.steps.of(tag)) is not None and row.started > 1
        ]
        if not late:
            return False
        tag = late[index % len(late)]
        row = self.state.steps.of(tag)
        record = self.state.records.record(tag)
        assert row is not None
        assert record is not None
        before = self.state
        try:
            self.add(
                Oneof(
                    "step_finished",
                    finished_message(tag, row.started - 1, self.success(tag, record.request)),
                )
            )
        except RecordRefusedError as refused:
            reason = refused.refusal.reason
        else:
            message = f"the fold took the finish of attempt {row.started - 1} of {tag.root}"
            raise AssertionError(message + f" while attempt {row.started} runs")
        assert reason is RefusalReason.ATTEMPT_MISMATCH
        assert self.state == before
        return True

    def cancel_running(self, why: Interruption) -> None:
        """The loop cancels the one running attempt and records how it ended."""
        (tag,) = self.running()
        started = self.state.steps.of(tag)
        assert started is not None
        self.add(Oneof("attempt_interrupted", interrupted_message(tag, started.started, why)))

    def raise_running(self, index: int) -> None:
        running = self.running()
        if running:
            tag = running[index % len(running)]
            started = self.state.steps.of(tag)
            assert started is not None
            self.add(
                Oneof(
                    "attempt_interrupted",
                    interrupted_message(tag, started.started, Interruption.RAISED),
                )
            )

    def offer(self, event: core.CoreEvent) -> bool:
        """An input from outside; whether core took it.

        The shell hands a control core refuses back to its sender and writes nothing, so the log
        never holds it and the model keeps it in ``refusals``. One core takes must do what it says.
        """
        before = self.state
        try:
            self.add(Oneof("core_input", input_message(event)))
        except core.ControlRefusedError as refused:
            self.refusals.append(refused)
            return False
        if isinstance(event, core.RunControlEvent):
            effective(self.state, event.control)
            if before.run.status is core.RunStatus.TERMINAL and self.state.run.result is None:
                self.reopened = len(self.records)
        return True

    def control(
        self,
        action: core.ControlAction,
        *,
        owner: core.PauseOwner | None = None,
        mode: core.StopMode | None = None,
        when: core.ResumeWhen | None = None,
    ) -> core.ControlInput:
        self.operator += 1
        return core.ControlInput(
            control_id=core.ControlId(root=f"operator-{self.operator}"),
            action=action,
            owner=owner,
            mode=mode,
            when=when,
        )


def effective(after: core.CoreState, control: core.ControlInput) -> None:
    """A control the log holds changed the run as it says, or the run was already there."""
    status = after.run.status
    match control.action:
        case "pause" | "pause_now":
            ok = status in (core.RunStatus.DRAINING, core.RunStatus.PARKED, core.RunStatus.PAUSED)
        case "stop":
            ok = after.run.result is not None
        case "resume" if control.when != "capacity_returns":
            ok = status is core.RunStatus.RUNNING
        case _:
            ok = True
    if not ok:
        raise IneffectiveControlError(control, status)


def ending(state: core.CoreState) -> bool:
    """A run the operator stopped: it takes no pause, and only a resume lifts the stop."""
    return state.run.result is not None or state.run.status in (
        core.RunStatus.CLOSING,
        core.RunStatus.BLOCKED,
    )


OPEN = (core.RequestPhase.PREPARED, core.RequestPhase.DISPATCHED, core.RequestPhase.RECONCILING)
"""The phases of a request that has no result yet."""

RECOVERY_ONLY = (
    core.InspectRequest,
    core.InspectTurn,
    core.InspectOwnedJob,
    core.CancelOwnedResource,
    core.BlockRequest,
)
"""The requests of the prove-or-block layer, which the runner loop has no runner for."""

UNSERVED = (core.InspectTurn, core.InspectOwnedJob)
"""Those of them that are steps: the rule starts a step, where it lets a command lapse."""

type Move = tuple[str, float | int | None]

MOVES: st.SearchStrategy[Move] = st.one_of(
    st.just(("tick", None)),
    st.tuples(st.just("finish"), st.integers(0, 3)),
    st.tuples(st.just("late_finish"), st.integers(0, 3)),
    st.tuples(st.just("raise"), st.integers(0, 3)),
    st.tuples(st.just("clock"), st.sampled_from([0.5, 3.0, 40.0, 500.0])),
    st.tuples(st.just("tock"), st.sampled_from([0.5, 3.0, 40.0])),
    st.tuples(st.just("pause"), st.sampled_from([None, 1.0, 20.0])),
    st.tuples(st.just("pause_now"), st.sampled_from([None, 1.0])),
    st.tuples(st.just("stop"), st.sampled_from([0, 1])),
    st.just(("quota_pause", None)),
    st.just(("resume_now", None)),
    st.just(("capacity_returns", None)),
    st.just(("crash", None)),
    st.just(("relaunch_resume", None)),
)


def play(loop: Loop, move: Move) -> None:
    """One move: something the runners, the clock, the process or the operator does."""
    name, argument = move
    match name:
        case "tick":
            loop.tick()
        case "finish":
            loop.finish_running(int(argument or 0))
        case "late_finish":
            loop.finish_late(int(argument or 0))
        case "raise":
            loop.raise_running(int(argument or 0))
        case "clock":
            loop.clock += float(argument or 0.0)
        case "tock":
            loop.clock += float(argument or 0.0)
            loop.offer(core.ClockAdvanced(now_at=loop.clock))
        case "crash":
            loop.open_epoch()
        case "relaunch_resume":
            before = loop.state
            loop.open_epoch()
            taken = _operator(loop, "resume_now", None)
            assert taken or ending(before), (
                f"a resume in a new epoch was refused: {loop.refusals[-1]}"
            )
        case _:
            _operator(loop, name, argument)


def _operator(loop: Loop, name: str, argument: float | int | None) -> bool:
    """What an operator, or the quota, sends the run; whether core took it."""
    result = core.RunResultProposal(outcome="cancelled", reason="operator")
    match name:
        case "pause" | "pause_now":
            owner = core.PauseOwner.OPERATOR if name == "pause" else None
            drain = None if argument is None else float(argument)
            event = core.RunControlEvent(
                control=loop.control(name, owner=owner), now_at=loop.clock, drain_seconds=drain
            )
        case "quota_pause":
            control = loop.control("pause", owner=core.PauseOwner.QUOTA)
            event = core.RunControlEvent(control=control, now_at=loop.clock)
        case "capacity_returns":
            control = loop.control("resume", when="capacity_returns")
            event = core.RunControlEvent(control=control, now_at=loop.clock)
        case "resume_now":
            control = loop.control("resume", when="now")
            event = core.RunControlEvent(control=control, now_at=loop.clock)
        case "stop":
            mode = core.StopMode.DRAIN if argument == 1 else core.StopMode.CANCEL
            control = loop.control("stop", mode=mode)
            event = core.RunControlEvent(control=control, now_at=loop.clock, result=result)
        case _:
            message = f"no such move: {name}"
            raise ValueError(message)
    return loop.offer(event)


def settle(loop: Loop, rounds: int = 80) -> None:
    """Run the loop with cooperative runners until `decide` has nothing left to do, or fail."""
    for _ in range(rounds):
        never_idle_draining(loop)
        if loop.running():
            loop.finish_running(0)
            continue
        actions = decide(loop.state, loop.clock)
        if any(not isinstance(action, Wait) for action in actions):
            loop.tick()
            continue
        until = next((a.until for a in actions if isinstance(a, Wait)), None)
        if until is None:
            return
        loop.clock = until
    raise RunDidNotRestError


def never_idle_draining(loop: Loop) -> None:
    """A draining run with no attempt running has finished draining: it is parked, or has work."""
    if loop.state.run.status is not core.RunStatus.DRAINING or loop.running():
        return
    actions = decide(loop.state, loop.clock)
    assert any(not isinstance(action, Wait) for action in actions), (
        "a draining run with nothing running and nothing to do never parks"
    )


def may_start_agrees_with_decide(loop: Loop) -> None:
    """The step a flight asks about may start exactly when the rule would run it, as that run.

    A step that may not is refused by one of the two reasons a race explains.
    """
    runs = {a.tag: a for a in decide(loop.state, loop.clock) if isinstance(a, Run)}
    for row in loop.state.records.records:
        tag = row.request_id
        due = start_due(loop.state, tag, loop.clock)
        assert may_start(loop.state, tag, loop.clock) is (tag in runs), tag
        if tag in runs:
            assert due == runs[tag], tag
        else:
            assert isinstance(due, RecordRefusal), tag
            assert due.reason in {RefusalReason.STEP_NOT_OPEN, RefusalReason.START_NOT_DUE}, tag


def parked_cancels_what_runs(loop: Loop) -> None:
    """A parked run holds no attempt that nothing will end.

    The kernel parks a run whose drain cancels before the loop has stopped what runs. `decide`
    cancels each such attempt, and one that ignores the cancel ends with the process, as the next
    epoch records (`parked_epoch_charges_nothing`).
    """
    if loop.state.run.status is core.RunStatus.PARKED:
        cancelled = {a.tag for a in decide(loop.state, loop.clock) if isinstance(a, Cancel)}
        assert set(loop.running()) <= cancelled, "the run is parked and an attempt runs unasked"


def limits_are_kept(loop: Loop) -> None:
    """A step waiting to run has the limit it was first started with.

    A drain caps the attempt it cannot interrupt, and the cap ends with the attempt: whatever
    drained, ran past it or was returned, the step runs again with its own limit.
    """
    for row in loop.state.records.records:
        first = loop.deadlines.get(row.request_id)
        if first is not None and row.phase is core.RequestPhase.PREPARED:
            assert row.until.at == first, f"{row.request_id.root} had {first}, has {row.until.at}"


def opens_no_unserved_step(loop: Loop) -> None:
    """Core opens no inspection of a job or a turn: it is a step, and no runner serves it.

    An inspection, a cancel of a resource and a block are commands the fold lapses; these two
    are not, and the rule would start them. What the rule itself opens would be a step nothing
    can run or clear, and the run it holds closing would wait for it for good.
    """
    for row in loop.state.records.records:
        if isinstance(row.request, UNSERVED):
            raise UnservedStepError(row)


def invariants(loop: Loop) -> None:
    opens_no_unserved_step(loop)
    never_idle_draining(loop)
    may_start_agrees_with_decide(loop)
    parked_cancels_what_runs(loop)
    limits_are_kept(loop)
