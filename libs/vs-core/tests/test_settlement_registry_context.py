"""Settlement receives declared registry facts, independent of available codecs."""

from typing import ClassVar, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

import vs_core.api as core


class JudgeOutput(core.Value):
    verdict: Literal["satisfied"] = "satisfied"


class JudgeTurn(core.OperationRequest):
    kind: Literal["registry.judge"] = "registry.judge"
    lifecycle: Literal[core.LifecycleClass.SESSION_TURN] = core.LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[BaseModel]] = JudgeOutput
    turn: core.TurnSpec


def normalize_judge(request: core.OperationRequest) -> core.TurnSpec:
    assert isinstance(request, JudgeTurn)
    return request.turn


def settlement_context(state: core.CoreState) -> core.SettlementContext:
    return core.SettlementContext(
        registry=state.registry,
        run=state.run,
        attempts=state.attempts,
        sessions=state.sessions,
        evaluation=state.evaluation,
        intents=state.intents,
    )


def envelope(state: core.CoreState) -> core.RunEnvelope[core.StrategyState]:
    return core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=0),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )


def test_registry_is_required_even_when_the_run_declares_no_operations() -> None:
    state = core.initial_state()
    context = settlement_context(state)
    assert context.registry == ()
    assert core.SettlementContext.model_fields["registry"].is_required()
    assert "registry" in core.SettlementContext.model_json_schema()["required"]
    with pytest.raises(ValidationError) as error:
        core.SettlementContext.model_validate(context.model_dump(exclude={"registry"}))
    assert [(row["loc"], row["type"]) for row in error.value.errors()] == [
        (("registry",), "missing")
    ]


@example(declared=False, request_version=1, outcome_version=1)
@given(
    declared=st.booleans(),
    request_version=st.integers(min_value=1, max_value=1000),
    outcome_version=st.integers(min_value=1, max_value=1000),
)
def test_declared_descriptor_survives_settlement_wake_and_envelope_reload(
    *, declared: bool, request_version: int, outcome_version: int
) -> None:
    descriptor = core.OperationDescriptor(
        kind="registry.judge",
        request_schema=core.SchemaRef(name="judge-turn", version=request_version),
        outcome_schema=core.SchemaRef(name="judge-output", version=outcome_version),
        lifecycle=core.LifecycleClass.SESSION_TURN,
        inspect=True,
        cancel=True,
        watch=True,
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor,
                request_model=JudgeTurn,
                outcome_model=JudgeOutput,
                normalize_turn=normalize_judge,
            ),
        )
    )
    registry = (descriptor,) if declared else ()
    state = core.initial_state().model_copy(update={"registry": registry})
    restored = codec.decode_envelope(
        core.RunEnvelope[core.StrategyState], codec.encode_envelope(envelope(state))
    )
    assert restored.core.registry == registry
    assert settlement_context(restored.core).registry == registry
    signal = core.SettlementDependencyResolved(
        decision_id=core.DecisionId(root="prerequisite"), status=core.CompletionStatus.SUCCEEDED
    )
    result = core.trace_step(
        restored.core,
        signal,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=signal, change=core.SettlementChange(state=restored.core.settlement)
                ),
            )
        ),
    )
    assert result.state.registry == registry
    assert result.requests == result.events == ()
    context = settlement_context(result.state)
    assert (
        core.SettlementContext.model_validate_json(context.model_dump_json()).registry == registry
    )
    assert codec.descriptors == (descriptor,)


@given(invalid_entry=st.text())
def test_public_step_validates_registry_before_entering_settlement(invalid_entry: str) -> None:
    # model_copy intentionally bypasses ingress, so step must construct its typed context.
    state = core.initial_state().model_copy(update={"registry": (invalid_entry,)})
    signal = core.SettlementDependencyResolved(
        decision_id=core.DecisionId(root="prerequisite"), status=core.CompletionStatus.SUCCEEDED
    )
    with pytest.raises(ValidationError) as error:
        core.step(state, signal)
    assert [row["loc"] for row in error.value.errors()] == [("registry", 0)]
