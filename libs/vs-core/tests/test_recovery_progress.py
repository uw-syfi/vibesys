"""Every action `decide` returns can be recorded, the fold accepts its record, and the run moves.

This is the class property behind the recovery rule: whatever state a run reaches, the actions
core asks for are actions the log can hold. A model of the runner loop writes, for each action, the
record the loop writes (a step started, a step finished, an attempt interrupted, the run ended),
and trajectories of operator pauses and stops, runner results and failures, crashes, resumes and
clock jumps drive the run through its states. Three things must hold in every one:

* the fold accepts the record of every action `decide` returns, in a state of any kind;
* applying an action changes the state, so no action repeats with nothing done, and a `Wait` is
  never already due;
* a run left to cooperative runners reaches rest: ended, parked, or idle with nothing open. A
  draining run never stays draining once its attempts have ended.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal

from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Action,
    Cancel,
    Cancelled,
    End,
    Finished,
    Interruption,
    Run,
    RunRest,
    Settle,
    Wait,
    at_rest,
    decide,
    ended_message,
    finished_message,
    fold,
    input_message,
    interrupted_message,
    interruption_of,
    may_start,
    started_message,
)
from vs_proto.api import EpochStarted, Oneof, Record, Timestamp
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable

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


class QueryOutcome(core.Value):
    status: Literal["succeeded"] = "succeeded"


class Query(core.OperationRequest):
    kind: Literal["test.query"] = "test.query"
    lifecycle: Literal[core.LifecycleClass.QUERY] = core.LifecycleClass.QUERY
    outcome_model: ClassVar[type[QueryOutcome]] = QueryOutcome
    value: int


def seeded(
    limits: tuple[float, ...], retries: int
) -> tuple[core.CoreState, core.OperationRegistry]:
    """A run with one open query step per limit, each allowed that many seconds."""
    descriptor = core.OperationDescriptor(
        kind="test.query",
        lifecycle=core.LifecycleClass.QUERY,
        request_schema=core.SchemaRef(name="query", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
    )
    registry = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=Query, outcome_model=QueryOutcome
            ),
        )
    )
    state = core.initial_state()
    bound = state.run.limits.request_retries.model_copy(update={"limit": retries})
    run = state.run.model_copy(
        update={
            "capabilities": core.Capabilities(operations=registry.descriptors),
            "limits": state.run.limits.model_copy(update={"request_retries": bound}),
        }
    )
    state = state.model_copy(update={"registry": registry.descriptors, "run": run})
    scope = core.Scope(owner=state.run.run_id, generation=0)
    for index, limit in enumerate(limits):
        decision = registry.validate_decision(
            core.Operation(
                decision_id=core.DecisionId(root=f"d:{index}"),
                scope=scope,
                request=Query(value=index),
            )
        )
        submitted = core.DecisionSubmitted(
            decision=core.Wait(request=decision, until=core.Within(limit=limit)),
            expected_revision=state.revision,
        )
        state = core.step(state, submitted).state
    return state, registry


class RunDidNotRestError(AssertionError):
    """The cooperative loop kept finding work."""

    def __init__(self) -> None:
        super().__init__("the run did not come to rest")


class RefusedRecordError(AssertionError):
    """The fold refused the record of an action `decide` returned."""

    def __init__(self, action: Action, error: ValueError) -> None:
        super().__init__(f"the fold refused the record of {action!r}: {error}")


@dataclass
class Loop:
    """The records the runner loop writes for each action, and the state they fold to."""

    state: core.CoreState
    registry: core.OperationRegistry
    records: list[Record] = field(default_factory=list)
    epoch: int = 0
    clock: float = 0.0
    operator: int = 0

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
        self.epoch += 1
        self.add(Oneof("epoch_started", EpochStarted(epoch=self.epoch)))

    def running(self) -> list[core.RequestId]:
        return [row.tag for row in self.state.steps.attempts if not row.ended]

    def body_of(self, action: Action) -> Body | None:
        match action:
            case Run():
                return Oneof("step_started", started_message(action))
            case Cancel(tag=tag, reason=reason):
                started = self.state.steps.of(tag)
                attempt = 0 if started is None else started.started
                why = interruption_of(reason)
                if why is not None:
                    return Oneof("attempt_interrupted", interrupted_message(tag, attempt, why))
                return Oneof(
                    "step_finished",
                    finished_message(tag, attempt, Cancelled(reason=reason)),
                )
            case Settle(tag=tag, result=result):
                started = self.state.steps.of(tag)
                attempt = 0 if started is None else started.started
                return Oneof("step_finished", finished_message(tag, attempt, result))
            case End():
                return Oneof("run_ended", ended_message(action))
            case Wait():
                return None

    def apply(self, action: Action) -> None:
        """Record what the loop records for ``action``; the state must move."""
        if isinstance(action, Wait):
            assert action.until is None or action.until > self.clock, action
            return
        body = self.body_of(action)
        assert body is not None
        before = self.state
        try:
            self.add(body)
        except ValueError as error:
            raise RefusedRecordError(action, error) from error
        assert self.state != before, f"{action!r} was recorded and changed nothing"

    def tick(self) -> int:
        """Apply every action `decide` returns now; how many were not a wait."""
        actions = decide(self.state, self.clock)
        for action in actions:
            self.apply(action)
        return sum(1 for action in actions if not isinstance(action, Wait))

    def finish_running(self, index: int) -> None:
        running = self.running()
        if not running:
            return
        tag = running[index % len(running)]
        record = self.state.records.record(tag)
        assert record is not None
        started = self.state.steps.of(tag)
        assert started is not None
        observation = core.Observation(
            event_id=core.EventId(root=f"done-{tag.root}-{started.started}"),
            request_id=tag,
            scope=record.request.scope,
            admission_id=record.request.admission_id,
            sequence=0,
            observed_at=self.clock,
            status=core.ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=True,
            children_complete=True,
        )
        result = Finished(observation=observation)
        self.add(Oneof("step_finished", finished_message(tag, started.started, result)))

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

    def offer(self, event: core.CoreEvent) -> None:
        """An input from outside; the shell refuses one the run cannot take, and so does this."""
        with contextlib.suppress(core.ControlRefusedError):
            self.add(Oneof("core_input", input_message(event)))

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


type Move = tuple[str, float | int | None]

MOVES: st.SearchStrategy[Move] = st.one_of(
    st.just(("tick", None)),
    st.tuples(st.just("finish"), st.integers(0, 3)),
    st.tuples(st.just("raise"), st.integers(0, 3)),
    st.tuples(st.just("clock"), st.sampled_from([0.5, 3.0, 40.0, 500.0])),
    st.tuples(st.just("pause"), st.sampled_from([None, 1.0, 20.0])),
    st.tuples(st.just("pause_now"), st.sampled_from([None, 1.0])),
    st.tuples(st.just("stop"), st.sampled_from([0, 1])),
    st.just(("crash", None)),
    st.just(("relaunch_resume", None)),
)


def never_idle_draining(loop: Loop) -> None:
    """A draining run with no attempt running has finished draining: it is parked, or has work."""
    if loop.state.run.status is not core.RunStatus.DRAINING or loop.running():
        return
    actions = decide(loop.state, loop.clock)
    assert any(not isinstance(action, Wait) for action in actions), (
        "a draining run with nothing running and nothing to do never parks"
    )


def may_start_agrees_with_decide(loop: Loop) -> None:
    """The step a flight asks about may start exactly when the rule would run it."""
    runs = {a.tag for a in decide(loop.state, loop.clock) if isinstance(a, Run)}
    for row in loop.state.records.records:
        assert may_start(loop.state, row.request_id, loop.clock) is (row.request_id in runs), (
            row.request_id
        )


def play(loop: Loop, move: Move) -> None:
    name, argument = move
    match name:
        case "tick":
            loop.tick()
        case "finish":
            loop.finish_running(int(argument or 0))
        case "raise":
            loop.raise_running(int(argument or 0))
        case "clock":
            loop.clock += float(argument or 0.0)
        case "pause" | "pause_now":
            owner = core.PauseOwner.OPERATOR if name == "pause" else None
            control = loop.control(name, owner=owner)
            drain = None if argument is None else float(argument)
            loop.offer(
                core.RunControlEvent(control=control, now_at=loop.clock, drain_seconds=drain)
            )
        case "stop":
            mode = core.StopMode.DRAIN if argument == 1 else core.StopMode.CANCEL
            control = loop.control("stop", mode=mode)
            result = core.RunResultProposal(outcome="cancelled", reason="operator")
            loop.offer(core.RunControlEvent(control=control, now_at=loop.clock, result=result))
        case "crash":
            loop.open_epoch()
        case "relaunch_resume":
            loop.open_epoch()
            control = loop.control("resume", when="now")
            loop.offer(core.RunControlEvent(control=control, now_at=loop.clock))


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


def seeds() -> st.SearchStrategy[tuple[tuple[float, ...], int]]:
    limits = st.lists(st.sampled_from([2.0, 10.0, 100.0]), min_size=1, max_size=3).map(tuple)
    return st.tuples(limits, st.integers(0, 3))


# The trajectories each way a run is stopped, paused, crashed and resumed goes through, pinned so
# every run covers them whatever the generator draws.
PINNED: list[tuple[tuple[tuple[float, ...], int], list[Move]]] = [
    (((100.0,), 0), [("tick", None), ("pause", 1.0), ("clock", 3.0)]),
    (((100.0, 100.0), 1), [("tick", None), ("pause_now", None)]),
    (((100.0,), 3), [("tick", None), ("pause", None), ("raise", 0)]),
    (((100.0,), 0), [("tick", None), ("crash", None), ("pause", None)]),
    (((100.0,), 2), [("tick", None), ("pause", None), ("crash", None), ("relaunch_resume", None)]),
    (((100.0,), 2), [("tick", None), ("pause", 1.0), ("relaunch_resume", None), ("tick", None)]),
    (((2.0,), 0), [("tick", None), ("stop", 1)]),
    (((100.0, 2.0), 0), [("tick", None), ("stop", 0)]),
    (((2.0, 100.0), 0), [("clock", 3.0), ("tick", None), ("pause", None)]),
    (((100.0,), 0), [("stop", 0), ("crash", None), ("relaunch_resume", None)]),
]


def pinned[F: Callable[..., object]](test: F) -> F:
    for seed, moves in PINNED:
        test = example(seed=seed, moves=moves)(test)
    return test


@pinned
@settings(max_examples=examples(pr=10, nightly=300), deadline=None)
@given(seed=seeds(), moves=st.lists(MOVES, max_size=14))
def test_every_action_core_returns_can_be_recorded_and_moves_the_run(
    seed: tuple[tuple[float, ...], int], moves: list[Move]
) -> None:
    state, registry = seeded(*seed)
    loop = Loop(state, registry)
    loop.open_epoch()
    for move in moves:
        play(loop, move)
        never_idle_draining(loop)
        may_start_agrees_with_decide(loop)
        loop.tick()
        never_idle_draining(loop)


@pinned
@settings(max_examples=examples(pr=10, nightly=300), deadline=None)
@given(seed=seeds(), moves=st.lists(MOVES, max_size=14))
def test_a_run_left_to_cooperative_runners_comes_to_rest_and_never_idles_draining(
    seed: tuple[tuple[float, ...], int], moves: list[Move]
) -> None:
    state, registry = seeded(*seed)
    loop = Loop(state, registry)
    loop.open_epoch()
    for move in moves:
        play(loop, move)
    settle(loop)
    status = loop.state.run.status
    assert status is not core.RunStatus.DRAINING, "a draining run with nothing running never parks"
    rest = at_rest(loop.state)
    open_steps = [row for row in loop.state.records.records if row.phase.value != "completed"]
    if rest is None:
        assert not open_steps, "idle with a step open and nothing running"
    if loop.state.run.result is not None:
        assert rest is RunRest.ENDED, "a run with its result never published its end"
    if rest is RunRest.ENDED:
        recorded = [r for r in loop.records if r.body is not None and r.body.field == "run_ended"]
        assert recorded, "the run is ended and its end is in no record"
