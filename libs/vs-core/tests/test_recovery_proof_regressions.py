"""Recovery grants require canonical operation, declaration and budget proofs."""

import json
from hashlib import sha256
from typing import ClassVar, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
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


def _record(request: core.Request, lifecycle: core.LifecycleClass) -> core.Intent:
    assert request.request_id is not None
    return core.Intent(
        request_id=request.request_id,
        request=request,
        payload_digest=_digest(request),
        lifecycle=lifecycle,
        phase=core.IntentPhase.DISPATCHED,
        reconcile_deadline_at=100.0,
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
    )


def _recovering(record: core.Intent) -> core.CoreState:
    state = core.initial_state()
    anchor_request = core.EnsureSession(
        request_id=core.RequestId(root="anchor"),
        scope=record.request.scope,
        admission_id=record.request.admission_id,
        deadline_at=100.0,
        spec=_spec(),
    )
    anchor = _record(anchor_request, core.LifecycleClass.IDEMPOTENT_WRITE).model_copy(
        update={"observation": None}
    )
    return state.model_copy(
        update={
            "run": state.run.model_copy(update={"status": core.RunStatus.PAUSED}),
            "intents": core.IntentsState(intents=(record, anchor)),
        }
    )


def _resolution(state: core.CoreState, identity: core.RequestId) -> str:
    result = core.step(state, core.RecoveryStarted(epoch=1, now_at=2.0))
    assert result.state.sessions == state.sessions
    assert result.state.evaluation == state.evaluation
    return next(
        row.resolution for row in result.state.intents.recovery.checks if row.target == identity
    )


def _registered_turn(decision_root: str = "decision") -> tuple[core.CoreState, core.Intent]:
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    turn = core.TurnSpec(
        session=_spec(),
        invocation_id=core.InvocationId(root="invocation"),
        workspace=scope,
        prompts=(),
        output_schema=core.SchemaRef(name="out", version=1),
        deadline_at=100.0,
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
            deadline_at=100.0,
            request=RegisteredTurn(turn=turn),
        )
    )
    identity = core.RequestId(root="execution")
    request = core.ExecuteRegisteredOperation(
        request_id=identity,
        scope=scope,
        deadline_at=100.0,
        decision_id=decision.decision_id,
        operation_id=core.OperationId(root=f"operation:{decision_root}"),
        operation=codec.encode(decision.request),
        retry_limit=0,
    )
    record = _record(request, core.LifecycleClass.SESSION_TURN)
    invocation = core.Invocation(
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


@pytest.mark.parametrize(
    "corruption", ["exact", "absent", "rejected", "feedback", "decision", "membership", "lifecycle"]
)
def test_registered_reattachment_needs_accepted_exact_origin(corruption: str) -> None:
    """Rejected or misidentified operation receipts cannot reattach live turns."""
    state, record = _registered_turn()
    receipt = state.run.receipts[0]
    if corruption == "rejected":
        receipt = receipt.model_copy(
            update={
                "feedback": core.Rejected(
                    decision_id=receipt.decision_id,
                    code=core.RejectionCode.CAPABILITY,
                    path=(),
                    detail="denied",
                )
            }
        )
    elif corruption == "feedback":
        receipt = receipt.model_copy(
            update={
                "feedback": receipt.feedback.model_copy(
                    update={"decision_id": core.DecisionId(root="unrelated")}
                )
            }
        )
    elif corruption == "decision":
        receipt = receipt.model_copy(update={"decision_id": core.DecisionId(root="unrelated")})
    elif corruption == "membership":
        receipt = receipt.model_copy(update={"request_ids": ()})
    elif corruption == "lifecycle":
        record = record.model_copy(update={"lifecycle": core.LifecycleClass.IDEMPOTENT_WRITE})
        state = state.model_copy(
            update={
                "intents": state.intents.model_copy(
                    update={"intents": (record, state.intents.intents[1])}
                )
            }
        )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={"receipts": () if corruption == "absent" else (receipt,)}
            )
        }
    )
    assert _resolution(state, record.request_id) == (
        "reattached" if corruption == "exact" else "pending"
    )


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
        deadline_at=1.0,
    )
    request = core.SubmitMeasurement(
        request_id=core.RequestId(root="submission"),
        scope=scope,
        deadline_at=1.0,
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
        ),
        limit=1,
        receipts=(core.PreparedSubmissionReceipt(request_id=request.request_id, ordinal=1),),
    )
    return request, budget


@pytest.mark.parametrize(
    "field",
    [
        "exact",
        "candidate",
        "purpose",
        "evaluator_digest",
        "workload_digest",
        "environment_digest",
        "recipe_digest",
    ],
)
def test_budget_request_id_alone_cannot_prove_measurement_reattachment(field: str) -> None:
    request, budget = measurement_fixture()
    if field != "exact":
        wrong = "baseline" if field == "purpose" else "different"
        if field == "candidate":
            wrong = core.RevisionRef(revision_id=core.RevisionId(root="other"), digest="other")
        budget = budget.model_copy(
            update={"identity": budget.identity.model_copy(update={field: wrong})}
        )
    record = _record(request, core.LifecycleClass.OWNED_JOB)
    state = _recovering(record).model_copy(
        update={
            "evaluation": core.EvaluationState(submission_budgets=(budget,)),
        }
    )
    assert _resolution(state, record.request_id) == (
        "reattached" if field == "exact" else "pending"
    )


def cleanup_fixture() -> tuple[core.CoreState, core.Intent, core.OperationDescriptor]:
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
        deadline_at=100.0,
        operation_id=core.OperationId(root="operation:cleanup"),
        decision_id=core.DecisionId(root="cleanup"),
        operation=codec.encode(Cleanup()),
        retry_limit=0,
    )
    record = _record(request, core.LifecycleClass.IDEMPOTENT_WRITE).model_copy(
        update={
            "phase": core.IntentPhase.PREPARED,
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


@pytest.mark.parametrize(
    "field",
    ["exact", "request_schema", "outcome_schema", "inspect", "offered_revision", "absent_offer"],
)
def test_registered_prepared_cleanup_needs_the_exact_descriptor(field: str) -> None:
    state, record, descriptor = cleanup_fixture()
    retained = descriptor
    offered = descriptor
    if field in ("request_schema", "outcome_schema"):
        retained = retained.model_copy(update={field: core.SchemaRef(name="foreign", version=2)})
    elif field == "inspect":
        retained = retained.model_copy(update={"inspect": False})
    elif field == "offered_revision":
        offered = offered.model_copy(update={"revision_authority": core.RevisionAuthority.NONE})
    state = state.model_copy(
        update={
            "registry": (retained,),
            "run": state.run.model_copy(
                update={
                    "capabilities": core.Capabilities(
                        operations=() if field == "absent_offer" else (offered,)
                    ),
                }
            ),
        }
    )
    assert _resolution(state, record.request_id) == (
        "safe-prepared" if field == "exact" else "pending"
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
) -> tuple[core.CoreState, core.Intent, core.RegisteredOwnedJob]:
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
            deadline_at=100.0,
            request=RegisteredMeasurement(measurement=budget.identity),
        )
    )
    identity = core.RequestId(root="measurement-execution")
    execution = core.ExecuteRegisteredOperation(
        request_id=identity,
        scope=request.scope,
        deadline_at=100.0,
        decision_id=decision.decision_id,
        operation_id=core.OperationId(root="operation:measurement"),
        operation=codec.encode(decision.request),
        retry_limit=0,
    )
    record = _record(execution, core.LifecycleClass.OWNED_JOB)
    job = core.RegisteredOwnedJob(
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


@pytest.mark.parametrize(
    "field",
    [
        "exact",
        "absent",
        "candidate",
        "purpose",
        "evaluator_digest",
        "workload_digest",
        "environment_digest",
        "recipe_digest",
    ],
)
@given(suffix=st.text(alphabet="abc123", min_size=1, max_size=12))
def test_registered_measurement_reattachment_requires_owning_normalization(
    field: str, suffix: str
) -> None:
    """Matching resource ownership cannot substitute a different measurement payload."""
    state, record, job = registered_measurement_fixture(suffix)
    assert job.expected_measurement is not None
    expectation = job.expected_measurement
    if field == "absent":
        expectation = None
    elif field != "exact":
        wrong = "baseline" if field == "purpose" else "foreign"
        if field == "candidate":
            wrong = core.RevisionRef(revision_id=core.RevisionId(root="foreign"), digest="foreign")
        expectation = expectation.model_copy(update={field: wrong})
    job = job.model_copy(update={"expected_measurement": expectation})
    state = state.model_copy(update={"evaluation": core.EvaluationState(registered_jobs=(job,))})
    assert _resolution(state, record.request_id) == (
        "reattached" if field == "exact" else "pending"
    )


@pytest.mark.parametrize("field", ["exact", "duplicate_invocation", "scope", "episode", "resource"])
def test_reattached_invocation_has_unique_exact_retained_source(field: str) -> None:
    state, record = _registered_turn()
    invocation = state.sessions.invocations[0]
    assert record.observation is not None
    previous = record.observation
    if field == "scope":
        previous = previous.model_copy(
            update={"scope": previous.scope.model_copy(update={"generation": 1})}
        )
    elif field == "episode":
        previous = previous.model_copy(update={"admission_id": core.DecisionId(root="foreign")})
    elif field == "resource":
        previous = previous.model_copy(update={"resource_id": core.ResourceId(root="foreign")})
    invocation = invocation.model_copy(update={"observation": previous})
    invocations = (invocation, invocation) if field == "duplicate_invocation" else (invocation,)
    state = state.model_copy(update={"sessions": core.SessionsState(invocations=invocations)})
    assert _resolution(state, record.request_id) == (
        "reattached" if field == "exact" else "pending"
    )
