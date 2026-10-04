"""Public recovery properties for typed child correspondence and source authority."""

import json
from hashlib import sha256
from typing import ClassVar, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import BaseModel

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    Accepted,
    Access,
    ArtifactId,
    ArtifactRef,
    CancelOwnedResource,
    Capabilities,
    ChildLease,
    ChildObservationWatermark,
    CoreState,
    DecisionId,
    DecisionReceipt,
    EnsureSession,
    EvaluationState,
    EventCursor,
    EventId,
    ExecuteRegisteredOperation,
    HostFence,
    HostId,
    InspectRequest,
    Intent,
    IntentPhase,
    IntentsState,
    LifecycleClass,
    MeasurementPlan,
    MeasurementStage,
    Observation,
    ObservationStatus,
    Operation,
    OperationDescriptor,
    OperationId,
    OperationRegistration,
    OperationRegistry,
    OperationRequest,
    OwnedJob,
    PoolId,
    ReconciliationDeadline,
    RecoveryBarrier,
    RecoveryStarted,
    RegisteredOwnedJob,
    RequestId,
    ResourceId,
    RoleId,
    RunEnvelope,
    RunStatus,
    SchemaRef,
    Scope,
    SessionId,
    SessionSpec,
    StrategyState,
    SubmitMeasurement,
    Value,
    initial_state,
    step,
)


class JobOutcome(Value):
    """Public immutable registered job result."""

    accepted: bool


class JobRequest(OperationRequest):
    """Public owned-job codec for recovery proof generation."""

    kind: Literal["child.job"] = "child.job"
    lifecycle: Literal[LifecycleClass.OWNED_JOB] = LifecycleClass.OWNED_JOB
    outcome_model: ClassVar[type[BaseModel]] = JobOutcome


def pending_intent(identity: str, phase: IntentPhase = IntentPhase.DISPATCHED) -> Intent:
    """A canonical persisted setup request with unknown external acceptance."""
    state = initial_state()
    request_id = RequestId(root=identity)
    request = EnsureSession(
        request_id=request_id,
        scope=Scope(owner=state.run.run_id, generation=0),
        deadline_at=100.0,
        spec=SessionSpec(
            session_id=SessionId(root=identity),
            role_id=RoleId(root="worker"),
            policy="reuse",
            lifetime="owner",
            access=Access.WRITE_ARTIFACTS,
        ),
    )
    return Intent(
        request_id=request_id,
        request=request,
        payload_digest=fixture_digest(request),
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=phase,
        reconcile_deadline_at=100.0,
    )


def observed(record: Intent, **facts: object) -> Observation:
    """Persist supplied observation facts through the public contract."""
    return Observation.model_validate(
        {
            "event_id": EventId(root=f"observation-{record.request_id.root}"),
            "request_id": record.request_id,
            "scope": record.request.scope,
            "sequence": 1,
            "observed_at": 10.0,
            "status": ObservationStatus.UNKNOWN,
            **facts,
        }
    )


def recovering_state(*records: Intent) -> CoreState:
    """A paused persisted kernel state awaiting recovery."""
    state = initial_state()
    return state.model_copy(
        update={
            "intents": IntentsState(intents=records, recovery=RecoveryBarrier()),
            "run": state.run.model_copy(update={"status": RunStatus.PAUSED}),
        }
    )


def registered_intent(
    lifecycle: LifecycleClass, identity: str
) -> tuple[Intent, OperationDescriptor]:
    """Encode an actual typed operation rather than synthesizing its wire data."""
    assert lifecycle == LifecycleClass.OWNED_JOB
    descriptor = OperationDescriptor(
        kind="child.job",
        lifecycle=lifecycle,
        request_schema=SchemaRef(name="child.job-request", version=1),
        outcome_schema=SchemaRef(name="child.job-outcome", version=1),
        resource_pool=PoolId(root="jobs"),
        inspect=True,
        cancel=True,
    )
    codec = OperationRegistry(
        (
            OperationRegistration(
                descriptor=descriptor, request_model=JobRequest, outcome_model=JobOutcome
            ),
        )
    )
    original = pending_intent(identity)
    request = ExecuteRegisteredOperation(
        request_id=original.request_id,
        scope=original.request.scope,
        deadline_at=100.0,
        decision_id=DecisionId(root=identity),
        operation_id=OperationId(root=f"operation:{identity}"),
        operation=codec.encode(JobRequest()),
        retry_limit=0,
    )
    return original.model_copy(
        update={
            "request": request,
            "lifecycle": lifecycle,
            "payload_digest": fixture_digest(request),
        }
    ), descriptor


def fixture_digest(value: Value) -> str:
    """Canonical bytes for scalar and ordered fixture contracts."""
    return sha256(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def with_registered_origins(state: CoreState) -> CoreState:
    """Supply independent codec-bound accepted origins for eligible fixtures."""
    codec = OperationRegistry(
        tuple(
            OperationRegistration(
                descriptor=descriptor,
                request_model=JobRequest,
                outcome_model=JobRequest.outcome_model,
            )
            for descriptor in state.registry
        )
    )
    receipts = list(state.run.receipts)
    for record in state.intents.intents:
        request = record.request
        if not isinstance(request, ExecuteRegisteredOperation):
            continue
        assert request.decision_id is not None
        decision = codec.validate_decision(
            Operation(
                decision_id=request.decision_id,
                scope=request.scope,
                request=codec.decode(request.operation),
                deadline_at=request.deadline_at,
            )
        )
        receipts.append(
            DecisionReceipt(
                decision_id=decision.decision_id,
                decision=decision,
                payload_digest=fixture_digest(decision),
                request_ids=(record.request_id,),
                feedback=Accepted(
                    decision_id=decision.decision_id, request_ids=(record.request_id,)
                ),
            )
        )
    return state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "receipts": tuple(receipts),
                    "capabilities": Capabilities(operations=state.registry),
                }
            )
        }
    )


def reload(state: CoreState) -> CoreState:
    """Roundtrip retained facts through the public registered envelope codec."""
    codec = OperationRegistry(
        tuple(
            OperationRegistration(
                descriptor=descriptor, request_model=JobRequest, outcome_model=JobOutcome
            )
            for descriptor in state.registry
        )
    )
    envelope = RunEnvelope[StrategyState](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        core=state,
        fence=HostFence(host_id=HostId(root="child-host"), epoch=state.intents.recovery.epoch),
        event_cursor=EventCursor(sequence=state.revision),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        strategy=StrategyState(schema_version=state.run.declaration.state_schema.version),
    )
    decoded = codec.decode_envelope(RunEnvelope[StrategyState], codec.encode_envelope(envelope))
    assert decoded == envelope
    return decoded.core


def released_parent(resource: ResourceId) -> Intent:
    """A completed parent retains responsibility for a discovered child."""
    parent = pending_intent(identity="parent", phase=IntentPhase.COMPLETED)
    return parent.model_copy(
        update={
            "observation": observed(
                parent,
                resource_id=ResourceId(root="parent-resource"),
                status=ObservationStatus.SUCCEEDED,
                accepted=True,
                terminal=True,
                released=True,
                children_complete=True,
                children=(resource,),
            )
        }
    )


def measurement_plan() -> MeasurementPlan:
    """Canonical built-in submission payload, distinct from registered jobs."""
    return MeasurementPlan(
        purpose="official",
        candidate=initial_state().run.facts.baseline,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        stages=(MeasurementStage(stage_id="measure", execution_budget=10.0),),
        policy="ordered",
        recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
        submitted_at=0.0,
        queue_allowance=0.0,
        deadline_at=10.0,
    )


@given(
    mismatch=st.sampled_from(
        [
            "operation",
            "pool",
            "registered-kind",
            "builtin-plan",
            "builtin-kind",
            "builtin-owner-kind",
        ]
    ),
    generation=st.integers(min_value=0, max_value=1000),
    sequence=st.integers(min_value=1, max_value=1000),
    released=st.booleans(),
    payload=st.text(alphabet="abc", min_size=1, max_size=16),
)
@example(mismatch="operation", generation=0, sequence=1, released=False, payload="a")
def test_child_transfer_requires_the_same_typed_correspondence_as_reattachment(
    *, mismatch: str, generation: int, sequence: int, released: bool, payload: str
) -> None:
    """Kind, operation, pool and payload mismatches retain ownership and inspect."""
    resource = ResourceId(root="child")
    parent = released_parent(resource)
    assert parent.observation is not None
    scope = parent.request.scope.model_copy(update={"generation": generation})
    parent = parent.model_copy(
        update={
            "request": parent.request.model_copy(update={"scope": scope}),
            "observation": parent.observation.model_copy(update={"scope": scope}),
        }
    )
    submission, descriptor = registered_intent(LifecycleClass.OWNED_JOB, identity="submission")
    request = submission.request
    assert isinstance(request, ExecuteRegisteredOperation)
    if mismatch == "registered-kind":
        request = pending_intent(identity="submission").request
    elif mismatch in ("builtin-plan", "builtin-kind"):
        request = SubmitMeasurement(
            request_id=submission.request_id,
            scope=scope,
            deadline_at=100.0,
            plan=measurement_plan().model_copy(update={"evaluator_digest": payload}),
        )
    request = request.model_copy(update={"scope": scope})
    submission = submission.model_copy(update={"request": request})
    proof = observed(
        submission,
        resource_id=resource,
        accepted=True,
        terminal=released,
        released=released,
        status=ObservationStatus.SUCCEEDED if released else ObservationStatus.PENDING,
        children_complete=True,
        sequence=sequence,
    )
    submission = submission.model_copy(update={"observation": proof})
    if mismatch in ("builtin-plan", "builtin-owner-kind"):
        plan = request.plan if isinstance(request, SubmitMeasurement) else measurement_plan()
        owner = OwnedJob(
            resource_id=resource,
            submission_id=submission.request_id,
            scope=scope,
            observation=proof,
            plan=plan.model_copy(update={"evaluator_digest": payload + "-foreign"}),
            status=proof.status,
        )
        evaluation = EvaluationState(jobs=(owner,))
    else:
        owner = RegisteredOwnedJob(
            operation_id=OperationId(root="different")
            if mismatch == "operation"
            else OperationId(root="operation-submission"),
            request_id=submission.request_id,
            scope=scope,
            resource_pool=PoolId(root="different" if mismatch == "pool" else "jobs"),
            resource_id=resource,
            observation=proof,
        )
        evaluation = EvaluationState(registered_jobs=(owner,))
    state = recovering_state(parent, submission, pending_intent(identity="anchor"))
    state = state.model_copy(update={"registry": (descriptor,), "evaluation": evaluation})
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    assert tuple(child.resource_id for child in result.state.intents.children) == (resource,)
    parent_check = next(
        check for check in result.state.intents.recovery.checks if check.target == parent.request_id
    )
    assert parent_check.resolution == "pending"
    if not released:
        assert (
            next(
                check
                for check in result.state.intents.recovery.checks
                if check.target == submission.request_id
            ).resolution
            == "pending"
        )
    assert any(
        isinstance(request, InspectRequest) and request.resource_id == resource
        for request in result.requests
    )
    assert result.state.evaluation == state.evaluation


@given(
    retained_sequence=st.integers(min_value=1, max_value=1000),
    foreign_sequence=st.integers(min_value=0, max_value=1000),
    foreign_released=st.booleans(),
    retained_released=st.booleans(),
)
@example(retained_sequence=1, foreign_sequence=0, foreign_released=False, retained_released=False)
@example(retained_sequence=10, foreign_sequence=1, foreign_released=True, retained_released=False)
def test_foreign_source_fact_cannot_transfer_a_retained_child_lease(
    *,
    retained_sequence: int,
    foreign_sequence: int,
    foreign_released: bool,
    retained_released: bool,
) -> None:
    """Foreign source proof, newer or stale, cannot replace retained authority."""
    resource = ResourceId(root="child")
    parent = released_parent(resource)
    submission, descriptor = registered_intent(LifecycleClass.OWNED_JOB, identity="submission")
    request = submission.request
    assert isinstance(request, ExecuteRegisteredOperation)
    retained = observed(
        parent,
        resource_id=resource,
        sequence=retained_sequence,
        status=ObservationStatus.SUCCEEDED if retained_released else ObservationStatus.PENDING,
        accepted=True,
        terminal=retained_released,
        released=retained_released,
        children_complete=True,
    )
    proof = observed(
        submission,
        resource_id=resource,
        sequence=foreign_sequence,
        status=ObservationStatus.SUCCEEDED if foreign_released else ObservationStatus.PENDING,
        accepted=True,
        terminal=foreign_released,
        released=foreign_released,
        children_complete=True,
    )
    submission = submission.model_copy(update={"observation": proof})
    assert parent.observation is not None
    assert parent.observation.resource_id is not None
    lease = ChildLease(
        resource_id=resource,
        scope=parent.request.scope,
        source_requests=(parent.request_id, submission.request_id),
        parent_resources=(parent.observation.resource_id,),
        observation=retained,
    )
    owner = RegisteredOwnedJob(
        operation_id=request.operation_id,
        request_id=submission.request_id,
        resource_pool=PoolId(root="jobs"),
        resource_id=resource,
        scope=parent.request.scope,
        observation=proof,
    )
    state = recovering_state(parent, submission, pending_intent(identity="anchor"))
    state = state.model_copy(
        update={
            "registry": (descriptor,),
            "evaluation": EvaluationState(registered_jobs=(owner,)),
            "intents": state.intents.model_copy(update={"children": (lease,)}),
        }
    )
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    assert result.state.intents.children == (lease,)
    assert any(
        isinstance(request, InspectRequest) and request.resource_id == resource
        for request in result.requests
    )
    assert (
        next(
            check
            for check in result.state.intents.recovery.checks
            if check.target == parent.request_id
        ).resolution
        == "pending"
    )
    assert result.state.evaluation == state.evaluation
    deadline = step(
        reload(result.state), ReconciliationDeadline(request_id=parent.request_id, now_at=100.0)
    )
    assert deadline.state.intents.children == (lease,)
    # A legacy aggregate lacks complete independent source history, including
    # when its selected source claims release. Keep cancellation debt.
    assert any(
        isinstance(request, CancelOwnedResource) and request.resource_id == resource
        for request in deadline.requests
    )


@given(released=st.booleans(), count=st.integers(min_value=2, max_value=5))
def test_incomplete_independent_source_history_cannot_transfer_to_a_typed_owner(
    *, released: bool, count: int
) -> None:
    resource = ResourceId(root="child")
    parent = released_parent(resource)
    submission, descriptor = registered_intent(LifecycleClass.OWNED_JOB, identity="submission")
    assert isinstance(submission.request, ExecuteRegisteredOperation)
    operation_id = submission.request.operation_id
    proof = observed(
        submission,
        resource_id=resource,
        accepted=True,
        terminal=released,
        released=released,
        children_complete=True,
        status=ObservationStatus.SUCCEEDED if released else ObservationStatus.PENDING,
    )
    submission = submission.model_copy(update={"observation": proof})
    sources = (
        parent,
        submission,
        *(pending_intent(f"source:{index}") for index in range(count - 2)),
    )
    lease = ChildLease(
        resource_id=resource,
        scope=parent.request.scope,
        source_requests=tuple(
            sorted((row.request_id for row in sources), key=lambda source: source.root)
        ),
        parent_resources=(ResourceId(root="parent-resource"),),
    )
    owner = RegisteredOwnedJob(
        operation_id=operation_id,
        request_id=submission.request_id,
        resource_pool=PoolId(root="jobs"),
        resource_id=resource,
        scope=parent.request.scope,
        observation=proof,
    )
    state = recovering_state(*sources)
    state = state.model_copy(
        update={
            "registry": (descriptor,),
            "evaluation": EvaluationState(registered_jobs=(owner,)),
            "intents": state.intents.model_copy(update={"children": (lease,)}),
        }
    )
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    assert result.state.intents.children == (lease,)
    assert any(
        isinstance(request, InspectRequest) and request.resource_id == resource
        for request in result.requests
    )
    assert result.state.evaluation == state.evaluation


@pytest.mark.parametrize("mutated_source", ["parent", "typed"])
@pytest.mark.parametrize("typed_first", [True, False])
@example(flags=(False, False, True, ObservationStatus.PENDING))
@example(flags=(True, True, True, ObservationStatus.SUCCEEDED))
@given(
    flags=st.tuples(
        st.booleans(), st.booleans(), st.booleans(), st.sampled_from(tuple(ObservationStatus))
    )
)
def test_complete_source_history_transfers_only_after_every_independent_release(
    mutated_source: str, *, typed_first: bool, flags: tuple[bool, bool, bool, ObservationStatus]
) -> None:
    terminal, released, complete, status = flags
    resource = ResourceId(root="child")
    parent = released_parent(resource)
    submission, descriptor = registered_intent(
        LifecycleClass.OWNED_JOB, identity="a-typed" if typed_first else "z-typed"
    )
    assert isinstance(submission.request, ExecuteRegisteredOperation)
    operation_id = submission.request.operation_id
    observations = []
    for source in (parent, submission):
        changed = source == (parent if mutated_source == "parent" else submission)
        observations.append(
            observed(
                source,
                resource_id=resource,
                accepted=True,
                terminal=terminal if changed else True,
                released=released if changed else True,
                children_complete=complete if changed else True,
                status=status if changed else ObservationStatus.SUCCEEDED,
            )
        )
    _, typed_fact = observations
    submission = submission.model_copy(update={"observation": typed_fact})
    marks = tuple(
        sorted(
            (
                ChildObservationWatermark(source_request=row.request_id, observation=row)
                for row in observations
            ),
            key=lambda mark: mark.source_request.root,
        )
    )
    lease = ChildLease(
        resource_id=resource,
        scope=parent.request.scope,
        source_requests=tuple(mark.source_request for mark in marks),
        parent_resources=(ResourceId(root="parent-resource"),),
        observation=typed_fact,
        observation_watermarks=marks,
        watermark_history_complete=True,
    )
    owner = RegisteredOwnedJob(
        operation_id=operation_id,
        request_id=submission.request_id,
        resource_pool=PoolId(root="jobs"),
        resource_id=resource,
        scope=parent.request.scope,
        observation=typed_fact,
    )
    state = recovering_state(parent, submission, pending_intent("anchor"))
    state = state.model_copy(
        update={
            "registry": (descriptor,),
            "evaluation": EvaluationState(registered_jobs=(owner,)),
            "intents": state.intents.model_copy(update={"children": (lease,)}),
        }
    )
    state = with_registered_origins(state)
    result = step(reload(state), RecoveryStarted(epoch=1, now_at=11.0))
    conclusive = (
        terminal
        and released
        and complete
        and status not in (ObservationStatus.PENDING, ObservationStatus.UNKNOWN)
    )
    assert result.state.intents.children == (() if conclusive else (lease,))
    assert result.state.evaluation == state.evaluation
    if not conclusive:
        deadline = step(
            reload(result.state), ReconciliationDeadline(request_id=parent.request_id, now_at=100.0)
        )
        assert any(
            isinstance(request, CancelOwnedResource) and request.resource_id == resource
            for request in deadline.requests
        )
