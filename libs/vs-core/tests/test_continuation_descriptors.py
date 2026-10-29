"""Registered continuation proofs require the run's declared operation schema."""

from itertools import product
from typing import Literal

import pytest

import vs_core.api as core

from .guard_rows import guard_rows
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
    record = state.records.records[0]
    codec = operation_codec()
    payload = RegisteredTurn(turn=invocation.turn)
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="registered-turn"),
            scope=invocation.scope,
            request=payload,
        )
    )
    request = core.ExecuteRegisteredOperation(
        request_id=record.request_id,
        operation_id=core.OperationId(root="operation:registered-turn"),
        decision_id=decision.decision_id,
        scope=invocation.scope,
        admission_id=record.request.admission_id,
        operation=codec.encode(payload),
        retry_limit=state.run.limits.request_retries.limit,
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest="registered-yield",
        feedback=core.Accepted(decision_id=decision.decision_id),
        request_ids=(record.request_id,),
    )
    return state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "receipts": (receipt, *state.run.receipts),
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                }
            ),
            "records": state.records.model_copy(
                update={
                    "records": (
                        record.model_copy(update={"request": request}),
                        *state.records.records[1:],
                    )
                }
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


ROUTES = ("turn", "wake", "reopen", "reopened")
DECLARATIONS = (
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
)
LIFECYCLES = tuple(core.LifecycleClass)
PROOFS = tuple(product((True, False), repeat=3))
# Past counterexamples: a lifecycle declaration under each proof that once slipped through.
PINNED_LIFECYCLES = (
    (core.LifecycleClass.QUERY, (False, True, True)),
    (core.LifecycleClass.OWNED_JOB, (True, False, True)),
    (core.LifecycleClass.IDEMPOTENT_WRITE, (True, True, True)),
    (core.LifecycleClass.SESSION_TURN, (True, False, True)),
)


def _cases() -> list[tuple[object, ...]]:
    rows = guard_rows(
        ROUTES,
        DECLARATIONS,
        LIFECYCLES,
        PROOFS,
        positive=("turn", "exact", core.LifecycleClass.QUERY, (True, True, True)),
        joint=(0, 1),
    )
    pinned = [
        (route, "lifecycle", lifecycle, proof)
        for route in ROUTES
        for lifecycle, proof in PINNED_LIFECYCLES
    ]
    return list(dict.fromkeys([*rows, *pinned]))


@pytest.mark.parametrize(("route", "declaration", "lifecycle", "proof"), _cases())
def test_registered_continuation_authority_requires_exact_run_declaration(
    *,
    route: Route,
    declaration: Declaration,
    lifecycle: core.LifecycleClass,
    proof: tuple[bool, bool, bool],
) -> None:
    registry_present, reload, accepted = proof
    version = 2
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
                    "receipts": (receipt, *state.run.receipts[1:]),
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
        # The public kernel forwards the proven reopen to Attempts B. These
        # fixtures carry no release proof for the parked scope's session and
        # jobs, so B declines without effect. Its accept path is covered in
        # test_attempt_retirement.
        result = core.step(state, event)
        assert result.requests == result.events == ()
        assert result.state.scheduling == state.scheduling
        assert result.state.attempts == state.attempts
    else:
        result = core.step(state, event)
        # Only a first suspension is announced to the strategy before its feedback.
        announced = route == "turn"
        assert len(result.events) == 1 + announced
        assert isinstance(result.events[0], core.TurnSuspended) == announced
        assert isinstance(result.events[-1], core.ResumeAuthorized)
    assert state.model_dump_json() == original
