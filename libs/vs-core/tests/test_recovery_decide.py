"""The recovery rule over generated logs: crash at every record, and decide never blocks.

A log is generated from a model of the loop (steps are prepared, started, interrupted, finished
and restarted), so the model says what is true after every record and the rule is held to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import (
    Action,
    Cancel,
    Cancelled,
    CancelReason,
    End,
    Failed,
    Finished,
    Interruption,
    InterruptPolicy,
    Run,
    RunRest,
    Settle,
    StepAttempts,
    StepResult,
    StepsState,
    Wait,
    at_rest,
    decide,
    finished_message,
    fold,
    input_message,
    interrupted_message,
    interruption_of,
    request_interrupt_policy,
    started_message,
)
from vs_proto.api import (
    AttemptInterrupted,
    CoreInput,
    EpochStarted,
    Oneof,
    Record,
    StepFinished,
    StepStarted,
    Timestamp,
)
from vs_sim.api.testing import examples

from .test_owner_event_precedence import dispatched

if TYPE_CHECKING:
    from collections.abc import Iterator

type Body = (
    Oneof[Literal["core_input"], CoreInput]
    | Oneof[Literal["step_started"], StepStarted]
    | Oneof[Literal["step_finished"], StepFinished]
    | Oneof[Literal["attempt_interrupted"], AttemptInterrupted]
    | Oneof[Literal["epoch_started"], EpochStarted]
)

SCHEMA = core.OperationSchemaRef(
    kind="project.artifact.put",
    request_schema=core.SchemaRef(name="artifact-put", version=1),
    outcome_schema=core.SchemaRef(name="artifact-put-outcome", version=1),
    lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
)


def seed_state() -> core.CoreState:
    state = core.initial_state()
    descriptor = core.OperationDescriptor(**SCHEMA.model_dump(mode="python"), inspect=True)
    run = state.run.model_copy(update={"capabilities": core.Capabilities(operations=(descriptor,))})
    return state.model_copy(update={"registry": (descriptor,), "run": run})


SEED = seed_state()


def request_of(name: str, retries: int) -> core.ExecuteRegisteredOperation:
    return core.ExecuteRegisteredOperation(
        request_id=core.RequestId(root=name),
        scope=core.Scope(owner=SEED.run.run_id, generation=0),
        operation_id=core.OperationId(root=f"operation-{name}"),
        operation=core.OperationWire(schema_ref=SCHEMA, payload_json='{"content":"x"}'),
        retry_limit=retries,
    )


def success(request: core.ExecuteRegisteredOperation) -> core.Observation:
    assert request.request_id is not None
    return core.Observation(
        event_id=core.EventId(root=f"done-{request.request_id.root}"),
        request_id=request.request_id,
        scope=request.scope,
        sequence=0,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
    )


@dataclass
class Step:
    """What the model knows of one step; the rule never reads it."""

    request: core.ExecuteRegisteredOperation
    started: int = 0
    uncharged: int = 0
    running: bool = False
    done: bool = False

    @property
    def tag(self) -> core.RequestId:
        assert self.request.request_id is not None
        return self.request.request_id

    @property
    def spent(self) -> bool:
        """Whether its attempts used the bound: a pause is no debit, a crash or a raise is one."""
        return self.started - self.uncharged > self.request.retry_limit

    def snapshot(self) -> Step:
        return Step(self.request, self.started, self.uncharged, self.running, self.done)


@dataclass
class Script:
    """A log, and for every length of it what the model says of the steps."""

    records: list[Record] = field(default_factory=list)
    epochs: list[int] = field(default_factory=list)
    open_steps: list[list[Step]] = field(default_factory=list)


class Builder:
    """Appends the records of one loop action and keeps the model in step with them."""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.script = Script(open_steps=[[]])
        self.epoch = 1

    def append(self, body: Body) -> None:
        seq = len(self.script.records) + 1
        record = Record(
            seq=seq, epoch=self.epoch, at=Timestamp(seconds=seq), clock=float(seq), body=body
        )
        self.script.records.append(record)
        self.script.epochs.append(self.epoch)
        self.script.open_steps.append([step.snapshot() for step in self.steps if not step.done])

    def prepare(self, retries: int) -> None:
        step = Step(request_of(f"step-{len(self.steps)}", retries))
        self.steps.append(step)
        event = core.RequestPrepared(
            request=step.request,
            until=core.Until(at=100.0),
            lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        )
        self.append(Oneof("core_input", input_message(event)))

    def start(self, step: Step) -> None:
        step.started += 1
        step.running = True
        run = Run(step.request, step.tag, step.started, 100.0)
        self.append(Oneof("step_started", started_message(run)))

    def interrupt(self, step: Step, why: Interruption) -> None:
        step.running = False
        step.uncharged += 0 if why.charged else 1
        self.append(Oneof("attempt_interrupted", interrupted_message(step.tag, step.started, why)))

    def end(self, step: Step, result: StepResult) -> None:
        step.running = False
        step.done = True
        self.append(Oneof("step_finished", finished_message(step.tag, step.started, result)))

    def restart(self) -> None:
        self.epoch += 1
        for step in self.steps:
            step.running = False
        self.append(Oneof("epoch_started", EpochStarted(epoch=self.epoch)))


@st.composite
def scripts(draw: st.DrawFn) -> Script:
    build = Builder()
    for _ in range(draw(st.integers(0, 14))):
        open_steps = [step for step in build.steps if not step.done]
        running = [step for step in open_steps if step.running]
        idle = [step for step in open_steps if not step.running]
        choices = ["restart", *(["prepare"] if len(build.steps) < 3 else [])]
        choices += ["start"] if idle else []
        choices += ["interrupt", "finish", "cancel"] if running else []
        match draw(st.sampled_from(choices)):
            case "prepare":
                build.prepare(draw(st.integers(0, 3)))
            case "start":
                build.start(draw(st.sampled_from(idle)))
            case "interrupt":
                build.interrupt(draw(st.sampled_from(running)), draw(st.sampled_from(Interruption)))
            case "finish":
                step = draw(st.sampled_from(running))
                build.end(step, Finished(observation=success(step.request)))
            case "cancel":
                step = draw(st.sampled_from(running))
                build.end(step, Cancelled(reason=CancelReason.WITHDRAWN))
            case _:
                build.restart()
    return build.script


def restarted(script: Script, length: int) -> Record:
    """The first record of the epoch after a crash at ``length`` records."""
    epoch = script.epochs[length - 1] if length else 1
    return Record(
        seq=length + 1,
        epoch=epoch + 1,
        at=Timestamp(seconds=length + 1),
        clock=float(length + 1),
        body=Oneof("epoch_started", EpochStarted(epoch=epoch + 1)),
    )


def states(script: Script) -> Iterator[tuple[core.CoreState, list[Step]]]:
    """The state after a crash at every length of the log, with what the model says is open."""
    state = SEED
    yield fold([], state), []
    for length, record in enumerate(script.records, 1):
        state = fold([record], state)
        yield state, script.open_steps[length]


def clock_of(length: int) -> float:
    return float(length + 1)


def kinds(actions: tuple[Action, ...]) -> set[type]:
    return {type(action) for action in actions}


@settings(max_examples=examples(pr=12, nightly=200))
@given(script=scripts())
def test_a_crash_at_every_record_leaves_every_open_step_to_run_again(script: Script) -> None:
    for length, (state, open_steps) in enumerate(states(script)):
        crashed = fold([restarted(script, length)], state)
        actions = decide(crashed, clock_of(length))
        assert kinds(actions) <= {Run, Cancel, Settle, Wait, End}
        runs = {action.tag: action for action in actions if isinstance(action, Run)}
        settled = {action.tag: action for action in actions if isinstance(action, Settle)}
        assert not [action for action in actions if isinstance(action, Cancel)]
        for step in open_steps:
            if step.spent:
                assert settled[step.tag].result == Failed(
                    reason=core.FailureReason.ATTEMPTS_EXHAUSTED
                )
                assert step.tag not in runs
            else:
                assert runs[step.tag].attempt == step.started + 1
                assert runs[step.tag].step == step.request
                assert step.tag not in settled
        assert set(runs) | set(settled) == {step.tag for step in open_steps}


@settings(max_examples=examples(pr=12, nightly=200))
@given(script=scripts())
def test_a_running_attempt_is_left_alone_until_its_limit(script: Script) -> None:
    for length, (state, open_steps) in enumerate(states(script)):
        actions = decide(state, clock_of(length))
        running = {step.tag for step in open_steps if step.running}
        assert not {action.tag for action in actions if isinstance(action, Run | Settle)} & running
        late = decide(state, 1000.0)
        assert {action.tag for action in late if isinstance(action, Cancel)} == running
        assert all(
            action.reason is CancelReason.DEADLINE for action in late if isinstance(action, Cancel)
        )


@settings(max_examples=examples(pr=12, nightly=200))
@given(script=scripts(), split=st.integers(0, 14))
def test_folding_a_cached_state_forward_equals_folding_the_whole_log(
    script: Script, split: int
) -> None:
    cut = min(split, len(script.records))
    whole = fold(script.records, SEED)
    cached = fold(script.records[cut:], fold(script.records[:cut], SEED))
    assert cached == whole
    assert fold(script.records, SEED) == whole


@settings(max_examples=examples(pr=12, nightly=200))
@given(script=scripts())
def test_a_rerun_attempt_is_not_a_debit_of_any_other_budget(script: Script) -> None:
    """Interruptions and later attempts change the step's own record, never the round's accounts."""
    started: set[str] = set()
    kept: list[Record] = []
    for record in script.records:
        body = record.body
        assert body is not None
        if body.field in {"attempt_interrupted", "epoch_started"}:
            continue
        if body.field == "step_started":
            if body.value.tag in started:
                continue
            started.add(body.value.tag)
        kept.append(record)
    reduced = fold(kept, SEED)
    full = fold(script.records, SEED)
    for area in ("attempts", "sessions", "scheduling", "evaluation", "settlement"):
        assert getattr(full, area) == getattr(reduced, area)
    assert full.run.receipts == reduced.run.receipts
    assert [row.retry_count for row in full.records.records] == [0] * len(full.records.records)


def turn_state(attempts: StepAttempts | None) -> tuple[core.CoreState, Step]:
    """A state holding one open session turn that asks to be cancelled when interrupted."""
    schema = SCHEMA.model_copy(
        update={
            "kind": "project.chat.turn",
            "lifecycle": core.LifecycleClass.SESSION_TURN,
            "on_interrupt": InterruptPolicy.CANCEL,
        }
    )
    request = request_of("turn", 3).model_copy(
        update={"operation": core.OperationWire(schema_ref=schema, payload_json="{}")}
    )
    record = core.RequestRecord(
        request_id=core.RequestId(root="turn"),
        request=request,
        payload_digest="digest",
        lifecycle=core.LifecycleClass.SESSION_TURN,
        phase=core.RequestPhase.DISPATCHED if attempts else core.RequestPhase.PREPARED,
        until=core.Until(at=100.0),
        addressed_by=(),
    )
    rows = () if attempts is None else (attempts,)
    state = SEED.model_copy(
        update={
            "records": SEED.records.model_copy(update={"records": (record,)}),
            "steps": StepsState(epoch=1, attempts=rows),
        }
    )
    return state, Step(request)


def epoch_record(epoch: int) -> Record:
    return Record(
        seq=99,
        epoch=epoch,
        at=Timestamp(seconds=99),
        clock=99.0,
        body=Oneof("epoch_started", EpochStarted(epoch=epoch)),
    )


@settings(max_examples=examples(pr=12, nightly=100))
@given(
    started=st.integers(1, 4),
    uncharged=st.integers(0, 4),
    ended=st.booleans(),
    restarted=st.booleans(),
)
def test_a_step_that_asks_to_be_cancelled_settles_as_interrupted_never_runs_again(
    *, started: int, uncharged: int, ended: bool, restarted: bool
) -> None:
    tag = core.RequestId(root="turn")
    attempts = StepAttempts(
        tag=tag, started=started, ended=ended, uncharged=min(uncharged, started)
    )
    state, _ = turn_state(attempts)
    state = fold([epoch_record(2)], state) if restarted else state
    actions = decide(state, 5.0)
    if ended or restarted:
        assert actions == (Settle(tag=tag, result=Cancelled(reason=CancelReason.INTERRUPTED)),)
    else:
        assert not [action for action in actions if isinstance(action, Settle | Run)]


def test_a_step_that_asks_to_be_cancelled_still_runs_its_first_attempt() -> None:
    state, step = turn_state(None)
    assert decide(state, 5.0) == (
        Run(step=step.request, tag=step.tag, attempt=1, deadline_at=100.0),
    )


PAUSES = st.sampled_from([core.RunStatus.PAUSED, core.RunStatus.DRAINING, core.RunStatus.PARKED])


def paused(
    state: core.CoreState,
    status: core.RunStatus,
    *,
    deadline_at: float | None = None,
    immediate: bool = False,
) -> core.CoreState:
    standing = core.RunPark(
        control_id=core.ControlId(root="pause"),
        epoch=0,
        immediate=immediate,
        deadline_at=deadline_at,
    )
    run = state.run.model_copy(update={"status": status, "park": standing})
    return state.model_copy(update={"run": run})


@settings(max_examples=examples(pr=12, nightly=200))
@given(script=scripts(), status=PAUSES)
def test_a_paused_run_starts_no_step_and_stops_what_runs_only_at_its_drain_deadline(
    script: Script, status: core.RunStatus
) -> None:
    for length, (state, open_steps) in enumerate(states(script)):
        now = clock_of(length)
        running = {step.tag for step in open_steps if step.running}
        draining = paused(state, status, deadline_at=now + 10.0)
        assert not [a for a in decide(draining, now) if isinstance(a, Run)]
        assert not [a for a in decide(draining, now + 5.0) if isinstance(a, Cancel)]
        expired = decide(draining, now + 10.0)
        assert {a.tag for a in expired if isinstance(a, Cancel)} == running
        assert {a.reason for a in expired if isinstance(a, Cancel)} <= {CancelReason.DRAIN_EXPIRED}
        unbounded = paused(state, status)
        assert not [a for a in decide(unbounded, now + 1e3) if isinstance(a, Cancel | Run)] or {
            a.reason for a in decide(unbounded, now + 1e3) if isinstance(a, Cancel)
        } == {CancelReason.DEADLINE}


@settings(max_examples=examples(pr=12, nightly=200))
@given(script=scripts())
def test_a_pause_now_cancels_every_running_step_and_starts_none(script: Script) -> None:
    for length, (state, open_steps) in enumerate(states(script)):
        now = clock_of(length)
        actions = decide(paused(state, core.RunStatus.DRAINING, immediate=True), now)
        assert {a.tag for a in actions if isinstance(a, Cancel)} == {
            step.tag for step in open_steps if step.running
        }
        assert {a.reason for a in actions if isinstance(a, Cancel)} <= {CancelReason.PAUSE_NOW}
        assert not [a for a in actions if isinstance(a, Run)]


@settings(max_examples=examples(pr=12, nightly=200))
@given(
    retries=st.integers(0, 3),
    count=st.integers(1, 3),
    action=st.sampled_from(["pause", "pause_now"]),
    drain_seconds=st.floats(min_value=10.0, max_value=50.0),
)
def test_a_pause_parks_once_the_attempts_it_interrupted_are_back_to_unstarted(
    retries: int, count: int, action: Literal["pause", "pause_now"], drain_seconds: float
) -> None:
    """A Pause now parks at once, over attempts the loop cancels; a pause drains until they end.

    The loop cancels the attempts and a call that cannot be stopped ends with the process, so a
    drain that cancels does not wait for them (`_run_park`); a pause that does not waits for every
    step to be unstarted or ended, and an interrupted attempt is neither until the interruption
    sends its step back to the state of one no one has started, with no result, no failure and
    no retry spent. That alone lets the drain park the run, so the next epoch runs the step again.
    The drain's deadline is further off than the records of the test (a second each) reach.
    """
    build = Builder()
    for _ in range(count):
        build.prepare(retries)
    for step in build.steps:
        build.start(step)
    state = fold(build.script.records, SEED)
    pause = core.RunControlEvent(
        control=core.ControlInput(
            control_id=core.ControlId(root="pause"),
            action=action,
            owner=core.PauseOwner.OPERATOR if action == "pause" else None,
        ),
        now_at=state.run.now_at,
        drain_seconds=drain_seconds,
    )
    build.append(Oneof("core_input", input_message(pause)))
    state = fold(build.script.records[-1:], state)
    waiting = core.RunStatus.PARKED if action == "pause_now" else core.RunStatus.DRAINING
    assert state.run.status is waiting
    for number, step in enumerate(build.steps, 1):
        build.interrupt(step, Interruption.PAUSE)
        state = fold(build.script.records[-1:], state)
        record = state.records.record(step.tag)
        assert record is not None
        assert record.phase is core.RequestPhase.PREPARED
        assert record.observation is None
        assert record.retry_count == 0
        attempts = state.steps.of(step.tag)
        assert attempts is not None
        assert attempts.uncharged == 1
        assert (state.run.status is core.RunStatus.PARKED) == (
            action == "pause_now" or number == count
        )
    assert at_rest(state) is RunRest.PARKED
    assert {row.phase for row in state.records.records} == {core.RequestPhase.PREPARED}


@settings(max_examples=examples(pr=12, nightly=200))
@given(script=scripts())
def test_an_ended_run_cancels_what_is_open_then_ends_and_never_fails(script: Script) -> None:
    result = core.RunResultProposal(outcome="cancelled", reason="done")
    for length, (state, open_steps) in enumerate(states(script)):
        run = state.run.model_copy(update={"result": result, "status": core.RunStatus.TERMINAL})
        actions = decide(state.model_copy(update={"run": run}), clock_of(length))
        if not open_steps:
            assert actions == (End(result=result),)
            continue
        assert {a.tag for a in actions if isinstance(a, Cancel)} == {
            step.tag for step in open_steps if step.running
        }
        assert {a.tag for a in actions if isinstance(a, Settle)} == {
            step.tag for step in open_steps if not step.running
        }
        assert not [a for a in actions if isinstance(a, Run | End)]


SETTLED = st.one_of(
    st.sampled_from(CancelReason).map(lambda reason: Cancelled(reason=reason)),
    st.just(Failed(reason=core.FailureReason.ATTEMPTS_EXHAUSTED)),
)


@settings(max_examples=examples(pr=12, nightly=100))
@given(result=SETTLED, started=st.booleans())
def test_every_result_core_settles_ends_its_step_and_keeps_its_reason(
    result: Cancelled | Failed, *, started: bool
) -> None:
    build = Builder()
    build.prepare(0)
    step = build.steps[0]
    if started:
        build.start(step)
    build.end(step, result)
    closed = fold(build.script.records, SEED).records.records[0]
    assert closed.phase is core.RequestPhase.COMPLETED
    assert closed.observation is not None
    assert closed.observation.diagnostic == result.reason.value
    assert closed.observation.terminal


def test_a_log_the_rule_did_not_write_is_refused_rather_than_guessed_at() -> None:
    build = Builder()
    build.prepare(1)
    step = build.steps[0]
    build.start(step)
    skipped = Run(step.request, step.tag, step.started + 2, 100.0)
    ghost = Run(request_of("ghost", 0), core.RequestId(root="ghost"), 1, 100.0)
    next_while_running = Run(step.request, step.tag, step.started + 1, 100.0)
    for bad in (skipped, ghost, Run(step.request, step.tag, 1, 100.0), next_while_running):
        records = [
            *build.script.records,
            Record(
                seq=len(build.script.records) + 1,
                epoch=1,
                at=Timestamp(seconds=9),
                clock=9.0,
                body=Oneof("step_started", started_message(bad)),
            ),
        ]
        with pytest.raises(ValueError, match="log"):
            fold(records, SEED)


@settings(max_examples=examples(pr=12, nightly=100))
@given(retries=st.integers(0, 3))
def test_a_step_whose_runner_always_raises_runs_its_bound_and_then_fails(retries: int) -> None:
    build = Builder()
    build.prepare(retries)
    step = build.steps[0]
    for attempt in range(1, retries + 2):
        (action,) = decide(fold(build.script.records, SEED), clock_of(len(build.script.records)))
        assert isinstance(action, Run)
        assert action.attempt == attempt
        build.start(step)
        build.interrupt(step, Interruption.RAISED)
    (action,) = decide(fold(build.script.records, SEED), clock_of(len(build.script.records)))
    assert action == Settle(
        tag=step.tag, result=Failed(reason=core.FailureReason.ATTEMPTS_EXHAUSTED)
    )


@settings(max_examples=examples(pr=12, nightly=100))
@given(
    retries=st.integers(0, 3),
    why=st.sampled_from([Interruption.PAUSE, Interruption.SHUTDOWN]),
    cycles=st.integers(1, 8),
)
def test_a_pause_or_shutdown_never_wears_a_step_out(
    retries: int, why: Interruption, cycles: int
) -> None:
    build = Builder()
    build.prepare(retries)
    step = build.steps[0]
    for _ in range(cycles):
        build.start(step)
        build.interrupt(step, why)
    (action,) = decide(fold(build.script.records, SEED), clock_of(len(build.script.records)))
    assert isinstance(action, Run)
    assert action.attempt == cycles + 1


@settings(max_examples=examples(pr=12, nightly=100))
@given(script=scripts())
def test_a_later_epoch_ends_every_running_attempt_and_charges_it(script: Script) -> None:
    state = fold(script.records, SEED)
    reopened = fold([epoch_record(state.steps.epoch + 1)], state)
    assert reopened.steps.epoch == state.steps.epoch + 1
    assert [row.tag for row in reopened.steps.attempts] == [row.tag for row in state.steps.attempts]
    for before, after in zip(state.steps.attempts, reopened.steps.attempts, strict=True):
        assert after.ended
        assert (after.started, after.uncharged) == (before.started, before.uncharged)
    assert fold([epoch_record(state.steps.epoch)], state).steps == state.steps


@pytest.mark.parametrize("status", list(core.RunStatus))
def test_a_run_is_at_rest_only_when_it_ended_or_parked(status: core.RunStatus) -> None:
    run = SEED.run.model_copy(update={"status": status})
    expected = {
        core.RunStatus.TERMINAL: RunRest.ENDED,
        core.RunStatus.PARKED: RunRest.PARKED,
    }.get(status)
    assert at_rest(SEED.model_copy(update={"run": run})) is expected


@pytest.mark.parametrize("reason", list(CancelReason))
def test_only_a_pause_turns_a_cancelled_attempt_into_an_interruption(reason: CancelReason) -> None:
    pause = {CancelReason.PAUSE_NOW, CancelReason.DRAIN_EXPIRED}
    assert interruption_of(reason) is (Interruption.PAUSE if reason in pause else None)


@settings(max_examples=examples(pr=12, nightly=200))
@given(script=scripts(), status=PAUSES)
def test_core_settles_only_with_reasons_that_are_results_and_cancels_with_either(
    script: Script, status: core.RunStatus
) -> None:
    for length, (state, _) in enumerate(states(script)):
        for standing in (state, paused(state, status, deadline_at=0.5, immediate=True)):
            for action in decide(standing, clock_of(length)):
                if isinstance(action, Settle) and isinstance(action.result, Cancelled):
                    assert interruption_of(action.result.reason) is None


@pytest.mark.parametrize("resume", [False, True], ids=["dispatch", "resume"])
@pytest.mark.parametrize("policy", list(InterruptPolicy))
def test_a_turn_declares_what_an_interruption_means_on_its_turn_spec(
    policy: InterruptPolicy, *, resume: bool
) -> None:
    state, request = dispatched()
    assert request.request_id is not None
    turn = request.turn.model_copy(update={"on_interrupt": policy})
    asked: core.DispatchTurn | core.ResumeSessionTurn = request.model_copy(update={"turn": turn})
    if resume:
        asked = core.ResumeSessionTurn(
            **asked.model_dump(exclude={"kind"}),
            continuation_id=core.ContinuationId(root="continuation"),
        )
    assert request_interrupt_policy(asked) is policy
    records = tuple(
        row.model_copy(update={"request": asked}) if row.request_id == asked.request_id else row
        for row in state.records.records
    )
    attempts = StepAttempts(tag=request.request_id, started=1, ended=True, uncharged=1)
    state = state.model_copy(
        update={
            "records": state.records.model_copy(update={"records": records}),
            "steps": StepsState(epoch=1, attempts=(attempts,)),
        }
    )
    (action,) = decide(state, 5.0)
    if policy is InterruptPolicy.CANCEL:
        assert action == Settle(
            tag=request.request_id, result=Cancelled(reason=CancelReason.INTERRUPTED)
        )
    else:
        assert isinstance(action, Run)
        assert action.attempt == 2
