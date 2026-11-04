"""Recovery grants require canonical operation, declaration and budget proofs."""

import json
from hashlib import sha256
from typing import ClassVar, Literal

from pydantic import BaseModel

import vs_core.api as core


class Outcome(core.Value):
    complete: bool


class RegisteredTurn(core.OperationRequest):
    kind: Literal["proof.turn"] = "proof.turn"
    lifecycle: Literal[core.LifecycleClass.SESSION_TURN] = core.LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[BaseModel]] = Outcome
    turn: core.TurnSpec


class Cleanup(core.OperationRequest):
    kind: Literal["proof.cleanup"] = "proof.cleanup"
    lifecycle: Literal[core.LifecycleClass.IDEMPOTENT_WRITE] = core.LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = Outcome


def _normalize_turn(request: core.OperationRequest) -> core.TurnSpec:
    assert isinstance(request, RegisteredTurn)
    return request.turn


def _digest(value: core.Value) -> str:
    """Canonical fixture bytes have only ordered sequences and scalar fields."""
    return sha256(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _spec() -> core.SessionSpec:
    return core.SessionSpec(
        session_id=core.SessionId(root="session"),
        role_id=core.RoleId(root="worker"),
        policy="reuse",
        lifetime="owner",
        access=core.Access.WRITE_ARTIFACTS,
    )


def _record(request: core.Request, lifecycle: core.LifecycleClass) -> core.RequestRecord:
    assert request.request_id is not None
    return core.RequestRecord(
        request_id=request.request_id,
        request=request,
        payload_digest=_digest(request),
        lifecycle=lifecycle,
        phase=core.RequestPhase.DISPATCHED,
        until=core.Until(at=100.0),
        observation=core.Observation(
            event_id=core.EventId(root="fact"),
            request_id=request.request_id,
            scope=request.scope,
            admission_id=request.admission_id,
            sequence=1,
            observed_at=1.0,
            status=core.ObservationStatus.PENDING,
            accepted=True,
            resource_id=core.ResourceId(root="resource"),
            children_complete=True,
        ),
        addressed_by=(),
    )


def _recovering(record: core.RequestRecord) -> core.CoreState:
    state = core.initial_state()
    anchor_request = core.EnsureSession(
        request_id=core.RequestId(root="anchor"),
        scope=record.request.scope,
        admission_id=record.request.admission_id,
        spec=_spec(),
    )
    anchor = _record(anchor_request, core.LifecycleClass.IDEMPOTENT_WRITE).model_copy(
        update={"observation": None}
    )
    return state.model_copy(
        update={
            "run": state.run.model_copy(update={"status": core.RunStatus.PAUSED}),
            "records": core.RecordsState(records=(record, anchor)),
        }
    )


def _registered_turn(decision_root: str = "decision") -> tuple[core.CoreState, core.RequestRecord]:
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    turn = core.TurnSpec(
        session=_spec(),
        invocation_id=core.InvocationId(root="invocation"),
        workspace=scope,
        prompts=(),
        output_schema=core.SchemaRef(name="out", version=1),
        charge_class="free",
    )
    descriptor = core.OperationDescriptor(
        kind="proof.turn",
        request_schema=core.SchemaRef(name="turn", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
        lifecycle=core.LifecycleClass.SESSION_TURN,
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor,
                request_model=RegisteredTurn,
                outcome_model=Outcome,
                normalize_turn=_normalize_turn,
            ),
        )
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root=decision_root),
            scope=scope,
            request=RegisteredTurn(turn=turn),
        )
    )
    identity = core.RequestId(root="execution")
    request = core.ExecuteRegisteredOperation(
        request_id=identity,
        scope=scope,
        decision_id=decision.decision_id,
        operation_id=core.OperationId(root=f"operation:{decision_root}"),
        operation=codec.encode(decision.request),
        retry_limit=0,
    )
    record = _record(request, core.LifecycleClass.SESSION_TURN)
    invocation = core.Invocation(
        until=core.Until(at=100.0),
        invocation=core.InvocationRef(
            session_id=turn.session.session_id,
            invocation_id=turn.invocation_id,
            generation=0,
        ),
        scope=scope,
        turn=turn,
        phase=core.SessionPhase.EXECUTING,
        registered_operation=request.operation_id,
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=_digest(decision),
        feedback=core.Accepted(decision_id=decision.decision_id, request_ids=(identity,)),
        request_ids=(identity,),
    )
    state = _recovering(record)
    return state.model_copy(
        update={
            "registry": (descriptor,),
            "run": state.run.model_copy(
                update={
                    "receipts": (receipt,),
                    "capabilities": core.Capabilities(operations=(descriptor,)),
                }
            ),
            "sessions": core.SessionsState(invocations=(invocation,)),
        }
    ), record


def measurement_fixture(
    suffix: str = "candidate",
) -> tuple[core.SubmitMeasurement, core.SubmissionBudget]:
    scope = core.Scope(owner=core.initial_state().run.run_id, generation=0)
    candidate = core.RevisionRef(revision_id=core.RevisionId(root=suffix), digest=suffix)
    plan = core.MeasurementPlan(
        purpose="official",
        candidate=candidate,
        evaluator_digest="eval",
        workload_digest="work",
        environment_digest="env",
        stages=(),
        policy="ordered",
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="recipe"),
        submitted_at=0.0,
        queue_allowance=1.0,
        accuracy_gated=(),
    )
    request = core.SubmitMeasurement(
        request_id=core.RequestId(root="submission"),
        scope=scope,
        plan=plan,
    )
    assert request.request_id is not None
    budget = core.SubmissionBudget(
        scope=scope,
        identity=core.MeasurementIdentity(
            purpose=plan.purpose,
            candidate=candidate,
            evaluator_digest=plan.evaluator_digest,
            workload_digest=plan.workload_digest,
            environment_digest=plan.environment_digest,
            recipe_digest=plan.recipe.digest,
            stages=(),
            accuracy_gated=(),
        ),
        limit=1,
        receipts=(core.PreparedSubmissionReceipt(request_id=request.request_id, ordinal=1),),
    )
    return request, budget


def cleanup_fixture() -> tuple[core.CoreState, core.RequestRecord, core.OperationDescriptor]:
    state = core.initial_state()
    owner_id = core.AttemptId(root="owner")
    scope = core.Scope(owner=owner_id, generation=0)
    episode = core.DecisionId(root="episode")
    descriptor = core.OperationDescriptor(
        kind="proof.cleanup",
        request_schema=core.SchemaRef(name="cleanup", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        revision_authority=core.RevisionAuthority.SNAPSHOT,
        inspect=True,
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor,
                request_model=Cleanup,
                outcome_model=Outcome,
            ),
        )
    )
    request = core.ExecuteRegisteredOperation(
        request_id=core.RequestId(root="cleanup"),
        scope=scope,
        admission_id=episode,
        operation_id=core.OperationId(root="operation:cleanup"),
        decision_id=core.DecisionId(root="cleanup"),
        operation=codec.encode(Cleanup()),
        retry_limit=0,
    )
    record = _record(request, core.LifecycleClass.IDEMPOTENT_WRITE).model_copy(
        update={
            "phase": core.RequestPhase.PREPARED,
            "observation": None,
        }
    )
    owner = core.AttemptView(
        attempt_id=owner_id,
        item_id=core.ItemId(root="item"),
        generation=0,
        phase=core.AttemptPhase.CLOSING,
        admission_id=episode,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
        closure=core.AttemptClosure(
            disposition="park",
            requested_at=1.0,
            authority=record.request_id,
            admission_id=episode,
        ),
    )
    state = _recovering(record)
    return (
        state.model_copy(
            update={
                "registry": (descriptor,),
                "attempts": core.AttemptsState(attempts=(owner,)),
                "run": state.run.model_copy(
                    update={"capabilities": core.Capabilities(operations=(descriptor,))}
                ),
            }
        ),
        record,
        descriptor,
    )


class RegisteredMeasurement(core.OperationRequest):
    kind: Literal["proof.measurement"] = "proof.measurement"
    lifecycle: Literal[core.LifecycleClass.OWNED_JOB] = core.LifecycleClass.OWNED_JOB
    outcome_model: ClassVar[type[BaseModel]] = Outcome
    measurement: core.MeasurementIdentity


def _normalize_measurement(request: core.OperationRequest) -> core.MeasurementIdentity:
    assert isinstance(request, RegisteredMeasurement)
    return request.measurement


def registered_measurement_fixture(
    suffix: str,
) -> tuple[core.CoreState, core.RequestRecord, core.RegisteredOwnedJob]:
    request, budget = measurement_fixture(suffix)
    descriptor = core.OperationDescriptor(
        kind="proof.measurement",
        lifecycle=core.LifecycleClass.OWNED_JOB,
        request_schema=core.SchemaRef(name="measurement", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
        resource_pool=core.PoolId(root="jobs"),
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor,
                request_model=RegisteredMeasurement,
                outcome_model=Outcome,
                normalize_measurement=_normalize_measurement,
            ),
        )
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="measurement"),
            scope=request.scope,
            request=RegisteredMeasurement(measurement=budget.identity),
        )
    )
    identity = core.RequestId(root="measurement-execution")
    execution = core.ExecuteRegisteredOperation(
        request_id=identity,
        scope=request.scope,
        decision_id=decision.decision_id,
        operation_id=core.OperationId(root="operation:measurement"),
        operation=codec.encode(decision.request),
        retry_limit=0,
    )
    record = _record(execution, core.LifecycleClass.OWNED_JOB)
    job = core.RegisteredOwnedJob(
        wait=core.WaitOn(requests=(identity,)),
        operation_id=execution.operation_id,
        request_id=identity,
        scope=execution.scope,
        resource_pool=core.PoolId(root="jobs"),
        expected_measurement=budget.identity,
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=_digest(decision),
        request_ids=(identity,),
        feedback=core.Accepted(decision_id=decision.decision_id, request_ids=(identity,)),
    )
    state = _recovering(record)
    return (
        state.model_copy(
            update={
                "registry": (descriptor,),
                "evaluation": core.EvaluationState(registered_jobs=(job,)),
                "run": state.run.model_copy(
                    update={
                        "receipts": (receipt,),
                        "capabilities": core.Capabilities(operations=(descriptor,)),
                    }
                ),
            }
        ),
        record,
        job,
    )
