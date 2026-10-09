"""Core bounds the deadline of every request it issues to the run deadline.

A requester may ask for more time than the run has left (a role's turn budget, an
agent's wait, a measurement plan). Core must still issue the request, bounded by
the run deadline, and refuse only when no time is left at all. One property per
request kind, over the same requested-deadline space.
"""

from typing import ClassVar, Literal

from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core

from .test_continuations import fixture, prefixed
from .test_episode_fencing import attempt_state
from .test_measurements import plan

RUN_DEADLINE = 60.0
# Before, at and past the run deadline, and past the clock's start.
REQUESTED = st.floats(min_value=0.5, max_value=500.0, allow_nan=False)


class Written(core.Value):
    done: bool


class Write(core.OperationRequest):
    kind: Literal["test.deadline.write"] = "test.deadline.write"
    lifecycle: Literal[core.LifecycleClass.IDEMPOTENT_WRITE] = core.LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = Written


def _bounded_run(state: core.CoreState) -> core.CoreState:
    return state.model_copy(
        update={"run": state.run.model_copy(update={"deadline_at": RUN_DEADLINE})}
    )


def _accepted(result: core.Transition) -> bool:
    return any(isinstance(event, core.Accepted) for event in result.events)


def _submit(state: core.CoreState, decision: core.Decision) -> core.Transition:
    return core.step(
        state, core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
    )


@given(requested=REQUESTED)
def test_a_measurement_is_issued_within_the_run_deadline(requested: float) -> None:
    state, scope = attempt_state()
    state = _bounded_run(state)
    measurement = plan(deadline_at=min(requested, 100.0))
    decision = core.Measure(decision_id=core.DecisionId(root="m"), scope=scope, plan=measurement)
    result = _submit(state, decision)
    submitted = [r for r in result.requests if isinstance(r, core.SubmitMeasurement)]
    assert submitted
    assert all(r.deadline_at <= RUN_DEADLINE for r in submitted)
    assert submitted[0].deadline_at == min(measurement.deadline_at, RUN_DEADLINE)


@given(requested=REQUESTED)
def test_a_turn_is_dispatched_within_the_run_deadline(requested: float) -> None:
    state, scope = attempt_state()
    state = _bounded_run(state)
    turn = core.TurnSpec(
        session=core.SessionSpec(
            session_id=core.SessionId(root="session"),
            role_id=core.RoleId(root="role"),
            policy="fresh",
            lifetime="owner",
            access=core.Access.WRITE_CANDIDATE,
        ),
        invocation_id=core.InvocationId(root="invocation"),
        workspace=scope,
        prompts=(),
        output_schema=core.SchemaRef(name="output", version=1),
        deadline_at=requested,
        charge_class="free",
    )
    decision = core.RequestTurn(decision_id=core.DecisionId(root="turn"), scope=scope, turn=turn)
    result = _submit(state, decision)
    assert _accepted(result), result.events
    assert result.requests
    assert all(r.deadline_at <= RUN_DEADLINE for r in result.requests)


@given(requested=REQUESTED)
def test_an_operation_is_issued_within_the_run_deadline(requested: float) -> None:
    descriptor = core.OperationDescriptor(
        kind="test.deadline.write",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        request_schema=core.SchemaRef(name="write", version=1),
        outcome_schema=core.SchemaRef(name="written", version=1),
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=Write, outcome_model=Written
            ),
        )
    )
    state = _bounded_run(core.initial_state())
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={"capabilities": core.Capabilities(operations=codec.descriptors)}
            ),
        }
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="write"),
            scope=core.Scope(owner=state.run.run_id, generation=0),
            request=Write(),
            deadline_at=requested,
        )
    )
    result = _submit(state, decision)
    assert _accepted(result), result.events
    issued = [r for r in result.requests if isinstance(r, core.ExecuteRegisteredOperation)]
    assert issued
    assert all(r.deadline_at == min(requested, RUN_DEADLINE) for r in issued)
    stored = result.state.run.receipts[-1].decision
    assert isinstance(stored, core.Operation)
    assert stored.deadline_at <= RUN_DEADLINE
    # The raw proposal replays against the bounded record without a payload conflict.
    assert _submit(result.state, decision).events == ()


@given(deadline_at=st.floats(min_value=RUN_DEADLINE, max_value=500.0, allow_nan=False))
def test_a_request_with_no_time_left_is_refused_with_a_budget_rejection(
    deadline_at: float,
) -> None:
    descriptor = core.OperationDescriptor(
        kind="test.deadline.write",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        request_schema=core.SchemaRef(name="write", version=1),
        outcome_schema=core.SchemaRef(name="written", version=1),
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=Write, outcome_model=Written
            ),
        )
    )
    state = core.initial_state()
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "now_at": RUN_DEADLINE,
                    "deadline_at": RUN_DEADLINE,
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                }
            ),
        }
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="write"),
            scope=core.Scope(owner=state.run.run_id, generation=0),
            request=Write(),
            deadline_at=deadline_at,
        )
    )
    result = _submit(state, decision)
    rejected = [event for event in result.events if isinstance(event, core.Rejected)]
    assert [item.code for item in rejected] == [core.RejectionCode.BUDGET]
    assert result.requests == ()


@given(requested=st.floats(min_value=2.0, max_value=500.0), exhausted=st.booleans())
def test_a_wait_is_refused_only_when_the_run_has_no_time_left(
    *, requested: float, exhausted: bool
) -> None:
    state, continuation = fixture(settled=True)
    run = state.run.model_copy(
        update={"deadline_at": RUN_DEADLINE, "now_at": RUN_DEADLINE if exhausted else 1.0}
    )
    state = prefixed(state.model_copy(update={"run": run}))
    wait = continuation.model_copy(update={"deadline_at": requested})
    expected = core.SuspensionRefusal.DEADLINE_EXCEEDED if exhausted else None
    assert core.suspension_refusal(state, wait.invocation, wait) == expected
