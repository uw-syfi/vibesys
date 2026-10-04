"""Registered continuation proofs require the run's declared operation schema."""

from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core

from .test_continuations import (
    RegisteredTurn,
    fixture,
    operation_codec,
    parked_fixture,
    reopened_fixture,
    roundtrip,
    with_wait,
)

type Route = Literal["turn", "wake", "reopen", "reopened"]
type Declaration = Literal[
    "exact",
    "absent",
    "kind",
    "request-name",
    "request-version",
    "outcome-name",
    "outcome-version",
    "lifecycle",
    "normalization",
    "duplicate",
]


def registered_yield() -> tuple[core.CoreState, core.Continuation]:
    state, continuation = fixture(settled=True)
    invocation = state.sessions.invocations[0]
    intent = state.intents.intents[0]
    codec = operation_codec()
    payload = RegisteredTurn(turn=invocation.turn)
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="registered-turn"),
            scope=invocation.scope,
            deadline_at=100.0,
            request=payload,
        )
    )
    request = core.ExecuteRegisteredOperation(
        request_id=intent.request_id,
        operation_id=core.OperationId(root="operation:registered-turn"),
        decision_id=decision.decision_id,
        scope=invocation.scope,
        deadline_at=100.0,
        admission_id=intent.request.admission_id,
        operation=codec.encode(payload),
        retry_limit=state.run.limits.max_retries,
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest="registered-yield",
        feedback=core.Accepted(decision_id=decision.decision_id),
        request_ids=(intent.request_id,),
    )
    return state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "receipts": (receipt,),
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                }
            ),
            "intents": state.intents.model_copy(
                update={"intents": (intent.model_copy(update={"request": request}),)}
            ),
            "sessions": state.sessions.model_copy(
                update={
                    "invocations": (
                        invocation.model_copy(
                            update={"registered_operation": request.operation_id}
                        ),
                    )
                }
            ),
        }
    ), continuation


def scenario(route: Route) -> tuple[core.CoreState, core.CoreEvent, str]:
    if route == "reopen":
        state, event = parked_fixture()
        return state, event, event.request.operation.schema_ref.kind
    if route == "reopened":
        state, _, event = reopened_fixture()
        return state, event, "evaluation.scope.reopen"
    state, continuation = registered_yield()
    if route == "wake":
        observation = state.evaluation.jobs[0].observation
        assert observation is not None
        return (
            with_wait(state, continuation),
            core.ContinuationJobsChanged(
                resource_id=continuation.jobs[0],
                observation=observation,
                previous=core.UnobservedJobFacts(resource_id=continuation.jobs[0]),
            ),
            "test.continuation-turn",
        )
    return state, core.TurnSuspended(continuation=continuation), "test.continuation-turn"


def declared_variation(
    descriptor: core.OperationDescriptor,
    declaration: Declaration,
    lifecycle: core.LifecycleClass,
    version: int,
) -> tuple[core.OperationDescriptor, ...]:
    match declaration:
        case "absent":
            return ()
        case "duplicate":
            return (descriptor, descriptor)
        case "kind":
            descriptor = descriptor.model_copy(update={"kind": f"other-{version}"})
        case "request-name" | "request-version" | "outcome-name" | "outcome-version":
            field = "request_schema" if declaration.startswith("request") else "outcome_schema"
            schema = getattr(descriptor, field)
            changed = schema.model_copy(
                update={"name": f"other-{version}"}
                if declaration.endswith("name")
                else {"version": version}
            )
            descriptor = descriptor.model_copy(update={field: changed})
        case "lifecycle":
            descriptor = core.OperationDescriptor.model_validate(
                {
                    **descriptor.model_dump(),
                    "lifecycle": lifecycle,
                    "normalization": descriptor.normalization
                    if lifecycle == descriptor.lifecycle
                    else core.OperationNormalizationKind.NONE,
                    "resource_pool": core.PoolId(root="other-pool")
                    if lifecycle == core.LifecycleClass.OWNED_JOB
                    else None,
                }
            )
        case "normalization":
            descriptor = descriptor.model_copy(
                update={"normalization": core.OperationNormalizationKind.NONE}
            )
        case "exact":
            pass
    return (descriptor,)


@pytest.mark.parametrize("route", ["turn", "wake", "reopen", "reopened"])
@pytest.mark.parametrize(
    "declaration",
    [
        "exact",
        "absent",
        "kind",
        "request-name",
        "request-version",
        "outcome-name",
        "outcome-version",
        "lifecycle",
        "normalization",
        "duplicate",
    ],
)
@given(
    lifecycle=st.sampled_from(tuple(core.LifecycleClass)),
    version=st.integers(min_value=2, max_value=100),
    proof=st.tuples(st.booleans(), st.booleans(), st.booleans()),
)
@example(
    lifecycle=core.LifecycleClass.QUERY,
    version=2,
    proof=(False, True, True),
)
@example(
    lifecycle=core.LifecycleClass.OWNED_JOB,
    version=2,
    proof=(True, False, True),
)
@example(
    lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
    version=2,
    proof=(True, True, True),
)
@example(
    lifecycle=core.LifecycleClass.SESSION_TURN,
    version=2,
    proof=(True, False, True),
)
def test_registered_continuation_authority_requires_exact_run_declaration(
    *,
    route: Route,
    declaration: Declaration,
    lifecycle: core.LifecycleClass,
    version: int,
    proof: tuple[bool, bool, bool],
) -> None:
    registry_present, reload, accepted = proof
    state, event, kind = scenario(route)
    descriptor = next(row for row in state.run.capabilities.operations if row.kind == kind)
    descriptors = declared_variation(descriptor, declaration, lifecycle, version)
    permitted = descriptors == (descriptor,) and accepted
    receipt = state.run.receipts[0]
    if not accepted:
        receipt = receipt.model_copy(
            update={
                "feedback": core.Rejected(
                    decision_id=receipt.decision_id,
                    code=core.RejectionCode.UNDECLARED_OPERATION,
                    path=("request", "kind"),
                    detail="not offered",
                )
            }
        )
    state = state.model_copy(
        update={
            "registry": state.registry if registry_present else (),
            "run": state.run.model_copy(
                update={
                    "capabilities": core.Capabilities(operations=descriptors),
                    "receipts": (receipt,),
                }
            ),
        }
    )
    if reload:
        state = roundtrip(state)
    original = state.model_dump_json()
    if route == "reopened" and not permitted:
        result = core.step(state, event)
        assert result.events == ()
        assert result.requests == ()
        assert result.state.evaluation.continuations == state.evaluation.continuations
    elif not permitted:
        with pytest.raises(core.ContractError):
            core.step(state, event)
    elif route == "reopen":
        # The public kernel forwards the proven reopen to its independently
        # owned Attempts B leaf, which remains a typed stub in this slice.
        with pytest.raises(core.KernelNotImplementedError) as error:
            core.step(state, event)
        assert error.value.event_kind == "scope_reopen_requested"
    else:
        result = core.step(state, event)
        assert len(result.events) == 1
        assert isinstance(result.events[0], core.ResumeAuthorized)
    assert state.model_dump_json() == original
