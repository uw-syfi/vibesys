"""Strategy events carry validated owning-library outcomes and durable wire data."""

from typing import ClassVar, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

import vs_core.api as core


class Outcome(core.Value):
    status: Literal["succeeded"] = "succeeded"
    result: tuple[str, ...]


class Query(core.OperationRequest):
    kind: Literal["test.typed"] = "test.typed"
    lifecycle: Literal[core.LifecycleClass.QUERY] = core.LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = Outcome


@given(st.lists(st.text(), max_size=5))
def test_strategy_operation_event_keeps_typed_outcome_through_wire(values: list[str]) -> None:
    descriptor = core.OperationDescriptor(
        kind="test.typed",
        lifecycle=core.LifecycleClass.QUERY,
        request_schema=core.SchemaRef(name="query", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=Query, outcome_model=Outcome
            ),
        )
    )
    state = core.initial_state()
    observation = core.Observation(
        event_id=core.EventId(root="event"),
        request_id=core.RequestId(root="request"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
    )
    schema = codec.encode(Query()).schema_ref
    event = core.OperationResult(
        operation_id=core.OperationId(root="operation"),
        observation=observation,
        outcome_schema=descriptor.outcome_schema,
        operation_schema=schema,
        outcome=Outcome(result=tuple(values)),
    )
    validated = codec.validate_event(event)
    assert type(validated.outcome) is Outcome
    wire = codec.encode_event(validated)
    restored = codec.decode_event(wire)
    assert restored == validated
    assert type(restored.outcome) is Outcome
    assert isinstance(restored.outcome, Outcome)
    assert restored.outcome.result == tuple(values)
    with pytest.raises((core.ContractError, ValidationError), match="registered"):
        core.OperationResult.model_validate_json(wire)


def callback_fixture() -> tuple[core.OperationRegistry, core.OperationResult]:
    descriptor = core.OperationDescriptor(
        kind="test.typed",
        lifecycle=core.LifecycleClass.QUERY,
        request_schema=core.SchemaRef(name="query", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=Query, outcome_model=Outcome
            ),
        )
    )
    state = core.initial_state()
    observation = core.Observation(
        event_id=core.EventId(root="event"),
        request_id=core.RequestId(root="request"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
    )
    event = core.OperationResult(
        operation_id=core.OperationId(root="operation"),
        observation=observation,
        outcome_schema=descriptor.outcome_schema,
        operation_schema=codec.encode(Query()).schema_ref,
        outcome=Outcome(result=("typed",)),
    )
    return codec, event


def test_callback_codec_rejects_missing_owner_payload() -> None:
    codec, event = callback_fixture()
    payload = event.model_dump(mode="python", exclude={"outcome_json"})
    with pytest.raises(ValidationError, match="registered"):
        core.OperationResult.model_validate(payload, context={"operation_registry": codec})


class MutableOutcome(BaseModel):
    payload: list[str]


def test_unregistered_mutable_callback_never_reaches_strategy() -> None:
    _, event = callback_fixture()
    unregistered = event.model_copy(update={"outcome": MutableOutcome(payload=["mutable"])})
    state = core.initial_state()
    clock = core.ClockAdvanced(now_at=1.0)
    with pytest.raises(core.ContractError, match="registered"):
        core.trace_step(
            state,
            clock,
            core.ReducerTrace(
                frames=(
                    core.TraceFrame(
                        signal=clock,
                        change=core.SchedulingChange(
                            state=state.scheduling, events=(unregistered,)
                        ),
                    ),
                )
            ),
        )


def test_copied_callback_schema_cannot_reuse_registered_proof() -> None:
    codec, event = callback_fixture()
    registered = codec.validate_event(event)
    changed = registered.model_copy(
        update={"outcome_schema": core.SchemaRef(name="wrong", version=99)}
    )
    assert not changed.outcome_is_registered
    state = core.initial_state()
    clock = core.ClockAdvanced(now_at=1.0)
    with pytest.raises(core.ContractError, match="registered"):
        core.trace_step(
            state,
            clock,
            core.ReducerTrace(
                frames=(
                    core.TraceFrame(
                        signal=clock,
                        change=core.SchedulingChange(state=state.scheduling, events=(changed,)),
                    ),
                )
            ),
        )
