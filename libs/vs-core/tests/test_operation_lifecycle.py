"""Registered operations use the same owning area lifecycle as built-ins."""

from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel, ValidationError

import vs_core.api as core


class Outcome(core.Value):
    status: Literal["succeeded"] = "succeeded"


class TurnRequest(core.OperationRequest):
    kind: Literal["test.turn"] = "test.turn"
    lifecycle: Literal[core.LifecycleClass.SESSION_TURN] = core.LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[BaseModel]] = Outcome
    max_turns: int


def descriptor() -> core.OperationDescriptor:
    return core.OperationDescriptor(
        kind="test.turn",
        lifecycle=core.LifecycleClass.SESSION_TURN,
        request_schema=core.SchemaRef(name="turn", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
        inspect=True,
        cancel=True,
        watch=True,
    )


def test_registered_turn_cannot_bypass_session_owner() -> None:
    registration = core.OperationRegistration(
        descriptor=descriptor(), request_model=TurnRequest, outcome_model=Outcome
    )
    if hasattr(core, "RevisionAuthority"):
        registration = core.OperationRegistration(
            descriptor=descriptor(),
            request_model=TurnRequest,
            outcome_model=Outcome,
            normalize_turn=normalize_turn,
        )
    codec = core.OperationRegistry((registration,))
    state = core.initial_state()
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
            decision_id=core.DecisionId(root="turn"),
            scope=core.Scope(owner=state.run.run_id, generation=0),
            request=TurnRequest(max_turns=1),
            deadline_at=100.0,
        )
    )
    result = core.step(state, core.DecisionSubmitted(decision=decision, expected_revision=0))
    assert result.requests == ()
    assert isinstance(result.events[0], core.Rejected)
    assert result.events[0].path == ("turn", "charge_class")


def normalize_turn(request: core.OperationRequest) -> core.TurnSpec:
    assert isinstance(request, TurnRequest)
    state = core.initial_state()
    return core.TurnSpec(
        session=core.SessionSpec(
            session_id=core.SessionId(root="session"),
            role_id=core.RoleId(root="role"),
            policy="fresh",
            lifetime="ephemeral",
            access=core.Access.READ_ONLY,
        ),
        invocation_id=core.InvocationId(root="invocation"),
        workspace=core.Scope(owner=state.run.run_id, generation=0),
        prompts=(),
        output_schema=core.SchemaRef(name="output", version=1),
        deadline_at=100.0,
        charge_class="paid",
        max_turns=request.max_turns,
    )


def test_turn_normalization_rejects_zero_turns() -> None:
    if hasattr(core, "RevisionAuthority"):
        registration = core.OperationRegistration(
            descriptor=descriptor(),
            request_model=TurnRequest,
            outcome_model=Outcome,
            normalize_turn=normalize_turn,
        )
    else:
        registration = core.OperationRegistration(
            descriptor=descriptor(), request_model=TurnRequest, outcome_model=Outcome
        )
    codec = core.OperationRegistry((registration,))
    state = core.initial_state()
    decision = core.Operation(
        decision_id=core.DecisionId(root="invalid-turn"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        request=TurnRequest(max_turns=0),
        deadline_at=100.0,
    )
    with pytest.raises((core.ContractError, ValidationError), match="max_turns"):
        codec.validate_decision(decision)


def test_owned_job_descriptor_requires_resource_pool() -> None:
    with pytest.raises(ValidationError, match="resource_pool"):
        core.OperationDescriptor(
            kind="job",
            lifecycle=core.LifecycleClass.OWNED_JOB,
            request_schema=core.SchemaRef(name="job", version=1),
            outcome_schema=core.SchemaRef(name="outcome", version=1),
            inspect=True,
            cancel=True,
            watch=True,
        )
