"""Properties of authoritative frozen wave-1 value contracts."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from vs_core.api import (
    Access,
    ArtifactId,
    ArtifactRef,
    AttemptBudget,
    AttemptCheckpoint,
    AttemptId,
    AttemptPhase,
    AttemptRef,
    AttemptView,
    ChargeId,
    ChargeKind,
    ChargeReceipt,
    ContinuationId,
    DecisionId,
    EventId,
    HistoricalSubmissionReceipt,
    InputDelivered,
    InputDropped,
    InputDropReason,
    InputId,
    InputRecord,
    InvocationId,
    InvocationInputTarget,
    InvocationRef,
    ItemId,
    JobObserved,
    JobProgress,
    JobTimeout,
    LifecycleClass,
    MeasurementIdentity,
    MeasurementStageIdentity,
    Observation,
    ObservationStatus,
    Operation,
    OperationDescriptor,
    OperationId,
    OperationNormalizationKind,
    OperationRegistration,
    OperationRegistry,
    OperationRequest,
    PreparedSubmissionReceipt,
    RegisteredJobObserved,
    RequestId,
    ResourceId,
    RevisionId,
    RevisionRef,
    RoleId,
    RunId,
    SchemaRef,
    Scope,
    ScopedAdmissionReopen,
    ScopedAdmissionReopenOutcome,
    ScopeInputTarget,
    ScopeReopenNormalization,
    SessionId,
    SessionInput,
    SessionPhase,
    SessionSpec,
    SessionsState,
    SessionView,
    Slot,
    SteerReceived,
    SubmissionBudget,
    TimedOut,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
    project,
    session_view,
)


def _artifact() -> ArtifactRef:
    return ArtifactRef(artifact_id=ArtifactId(root="input"), digest="digest")


def _revision(name: str = "revision") -> RevisionRef:
    return RevisionRef(revision_id=RevisionId(root=name), digest="digest")


def _invocation(name: str = "invocation") -> InvocationRef:
    return InvocationRef(
        session_id=SessionId(root="session"), invocation_id=InvocationId(root=name), generation=0
    )


def _observation(*, sequence: int = 0, at: float = 1.0, accepted: bool = True) -> Observation:
    return Observation(
        event_id=EventId(root="observation"),
        request_id=RequestId(root="request"),
        scope=Scope(owner=RunId(root="run"), generation=0),
        sequence=sequence,
        observed_at=at,
        status=ObservationStatus.PENDING,
        accepted=accepted,
    )


def _identity(stages: tuple[MeasurementStageIdentity, ...] = ()) -> MeasurementIdentity:
    return MeasurementIdentity(
        purpose="baseline",
        candidate=_revision(),
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        recipe_digest="recipe",
        stages=stages,
    )


@given(
    deadline=st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False),
    delay=st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False),
    count=st.integers(min_value=1, max_value=10),
)
def test_wait_all_timeout_preserves_missing_progress(
    deadline: float, delay: float, count: int
) -> None:
    jobs = tuple(JobTimeout(resource_id=ResourceId(root=f"job-{i}")) for i in range(count))
    timeout = TimedOut(deadline_at=deadline, reached_at=deadline + delay, unfinished=jobs)
    restored = TimedOut.model_validate_json(timeout.model_dump_json())
    assert restored == timeout
    assert all(job.progress is None for job in restored.unfinished)
    assert tuple(job.resource_id for job in restored.unfinished) == tuple(
        job.resource_id for job in jobs
    )


def test_timeout_rejects_premature_or_duplicate_dependencies() -> None:
    job = JobTimeout(resource_id=ResourceId(root="job"))
    with pytest.raises(ValidationError, match="reached_at"):
        TimedOut(deadline_at=2.0, reached_at=1.0, unfinished=(job,))
    with pytest.raises(ValidationError, match="unfinished"):
        TimedOut(deadline_at=1.0, reached_at=2.0, unfinished=(job, job))


@given(
    charged=st.integers(min_value=0, max_value=100), refund=st.integers(min_value=0, max_value=100)
)
def test_charge_refunds_never_exceed_charge(charged: int, refund: int) -> None:
    fields = {
        "charge_id": ChargeId(root="charge"),
        "kind": ChargeKind.ATTEMPT,
        "charged": charged,
        "refunded": refund,
    }
    if refund > charged:
        with pytest.raises(ValidationError, match="refunded"):
            ChargeReceipt.model_validate(fields)
    else:
        receipt = ChargeReceipt.model_validate(fields)
        assert ChargeReceipt.model_validate_json(receipt.model_dump_json()) == receipt


@given(refund=st.integers(min_value=1, max_value=100))
def test_turn_currency_never_refunds(refund: int) -> None:
    with pytest.raises(ValidationError, match="TURN"):
        ChargeReceipt(
            charge_id=ChargeId(root="turn"), kind=ChargeKind.TURN, charged=refund, refunded=refund
        )


def test_refund_authority_cannot_repeat() -> None:
    authority = RequestId(root="refund")
    with pytest.raises(ValidationError, match="refund_sources"):
        ChargeReceipt(
            charge_id=ChargeId(root="charge"),
            kind=ChargeKind.ATTEMPT,
            charged=2,
            refunded=2,
            refund_sources=(authority, authority),
        )


@given(count=st.integers(min_value=1, max_value=10))
def test_submission_ordinals_include_historical_consumption(count: int) -> None:
    receipts = tuple(
        HistoricalSubmissionReceipt(ordinal=i, proof=_artifact())
        if i % 2
        else PreparedSubmissionReceipt(ordinal=i, request_id=RequestId(root=f"request-{i}"))
        for i in range(1, count + 1)
    )
    budget = SubmissionBudget(
        scope=Scope(owner=RunId(root="run"), generation=0),
        identity=_identity(),
        limit=count,
        receipts=receipts,
    )
    assert SubmissionBudget.model_validate_json(budget.model_dump_json()) == budget
    with pytest.raises(ValidationError, match="ordinals"):
        budget.model_validate(
            {
                **budget.model_dump(),
                "receipts": (receipts[0].model_copy(update={"ordinal": count + 1}),),
            }
        )


def test_measurement_identity_canonicalizes_stage_and_dependency_order() -> None:
    a = MeasurementStageIdentity(stage_id="a")
    b = MeasurementStageIdentity(stage_id="b")
    c = MeasurementStageIdentity(stage_id="c", depends_on=("b", "a"))
    identity = _identity((c, b, a))
    equivalent = _identity((a, b, c.model_copy(update={"depends_on": ("a", "b")})))
    assert identity == equivalent
    assert MeasurementIdentity.model_validate_json(identity.model_dump_json()) == identity


@pytest.mark.parametrize(
    "stages",
    [
        (MeasurementStageIdentity(stage_id="a", depends_on=("missing",)),),
        (MeasurementStageIdentity(stage_id="a", depends_on=("a",)),),
        (MeasurementStageIdentity(stage_id="a"), MeasurementStageIdentity(stage_id="a")),
    ],
)
def test_measurement_identity_rejects_invalid_dependency_graph(
    stages: tuple[MeasurementStageIdentity, ...],
) -> None:
    with pytest.raises(ValidationError):
        _identity(stages)


def test_identical_artifacts_are_distinct_input_occurrences() -> None:
    target = ScopeInputTarget(scope=Scope(owner=RunId(root="run"), generation=0))
    records = tuple(
        InputRecord(
            input=SessionInput(
                input_id=InputId(root=f"note-{i}"),
                target=target,
                artifact=_artifact(),
                received_at=1.0,
                sequence=i,
            )
        )
        for i in range(2)
    )
    state = SessionsState(inputs=records)
    assert SessionsState.model_validate_json(state.model_dump_json()) == state
    with pytest.raises(ValidationError, match="duplicate input ID"):
        SessionsState(inputs=(records[0], records[0]))


@pytest.mark.parametrize("accepted", [False, True])
def test_delivery_requires_confirmed_matching_reservation(*, accepted: bool) -> None:
    invocation = _invocation()
    item = SessionInput(
        input_id=InputId(root="note"),
        target=InvocationInputTarget(invocation=invocation),
        artifact=_artifact(),
        received_at=1.0,
        sequence=0,
    )
    if not accepted:
        with pytest.raises(ValidationError, match="accepted"):
            InputDelivered(
                input_id=item.input_id,
                invocation=invocation,
                observation=_observation(accepted=False),
            )
        return
    delivery = InputDelivered(
        input_id=item.input_id, invocation=invocation, observation=_observation()
    )
    record = InputRecord(input=item, reserved_to=invocation, receipt=delivery)
    assert InputRecord.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError, match="reserved_to"):
        InputRecord(input=item, reserved_to=_invocation("different"), receipt=delivery)


def test_steer_cannot_retarget_invocation_specific_input() -> None:
    item = SessionInput(
        input_id=InputId(root="note"),
        target=InvocationInputTarget(invocation=_invocation()),
        artifact=_artifact(),
        received_at=1.0,
        sequence=0,
    )
    with pytest.raises(ValidationError, match=r"inputs\.target\.invocation"):
        SteerReceived(invocation=_invocation("different"), inputs=(item,))


def test_dropped_receipt_preserves_occurrence_and_target() -> None:
    target = ScopeInputTarget(scope=Scope(owner=RunId(root="run"), generation=0))
    item = SessionInput(
        input_id=InputId(root="note"),
        target=target,
        artifact=_artifact(),
        received_at=1.0,
        sequence=0,
    )
    receipt = InputDropped(
        input_id=item.input_id, target=target, reason=InputDropReason.RUN_TERMINAL, at=2.0
    )
    record = InputRecord(input=item, receipt=receipt)
    assert InputRecord.model_validate_json(record.model_dump_json()) == record


def test_checkpoint_history_projects_latest_without_losing_attribution() -> None:
    checkpoints = tuple(
        AttemptCheckpoint(
            invocation=_invocation(f"invocation-{i}"),
            request_id=RequestId(root=f"checkpoint-{i}"),
            revision=_revision(f"r{i}"),
            retention="wip",
        )
        for i in range(2)
    )
    view = AttemptView(
        attempt_id=AttemptId(root="attempt"),
        item_id=ItemId(root="item"),
        generation=0,
        phase=AttemptPhase.ACTIVE,
        workspace=WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=_revision()),
        budget=AttemptBudget(),
        checkpoints=checkpoints,
    )
    assert view.checkpoint == checkpoints[-1].revision
    assert view.checkpoints[0].invocation == _invocation("invocation-0")


@given(at=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False))
def test_slot_rejects_negative_occupancy_interval(at: float) -> None:
    with pytest.raises(ValidationError, match="charge_ended_at"):
        Slot(
            attempt=AttemptRef(attempt_id=AttemptId(root="attempt"), generation=0),
            admission_id=DecisionId(root="episode"),
            admitted_at=at + 1.0,
            charge_ended_at=at,
        )


def test_progress_is_strict_and_unknown_fields_name_the_key() -> None:
    progress = JobProgress(observation_sequence=0, observed_at=1.0, state="unknown")
    assert (
        TypeAdapter(JobProgress).validate_json(TypeAdapter(JobProgress).dump_json(progress))
        == progress
    )
    with pytest.raises(ValidationError, match="misspelled"):
        JobProgress.model_validate({**progress.model_dump(), "misspelled": 1})


@pytest.mark.parametrize("registered", [False, True])
@given(sequence=st.integers(min_value=0, max_value=100))
def test_progress_must_match_carrying_observation(*, registered: bool, sequence: int) -> None:
    observation = _observation(sequence=sequence)
    progress = JobProgress(
        observation_sequence=sequence, observed_at=observation.observed_at, state="running"
    )
    if registered:
        event = RegisteredJobObserved(
            operation_id=OperationId(root="job"), observation=observation, progress=progress
        )
    else:
        event = JobObserved(
            resource_id=ResourceId(root="job"), observation=observation, progress=progress
        )
    assert type(event).model_validate_json(event.model_dump_json()) == event
    with pytest.raises(ValidationError, match="progress"):
        type(event).model_validate(
            {
                **event.model_dump(),
                "progress": progress.model_copy(update={"observation_sequence": sequence + 1}),
            }
        )


@given(order=st.permutations((0, 1, 2)))
def test_session_reservations_project_from_occurrences_in_stable_order(order: list[int]) -> None:
    invocation = _invocation()
    scope = Scope(owner=RunId(root="run"), generation=0)
    target = ScopeInputTarget(scope=scope)
    records = tuple(
        InputRecord(
            input=SessionInput(
                input_id=InputId(root=f"note-{i}"),
                target=target,
                artifact=ArtifactRef(artifact_id=ArtifactId(root=f"artifact-{i}"), digest="digest"),
                received_at=1.0,
                sequence=i % 2,
            ),
            reserved_to=invocation,
        )
        for i in order
    )
    delivered = SessionInput(
        input_id=InputId(root="delivered"),
        target=target,
        artifact=_artifact(),
        received_at=1.0,
        sequence=0,
    )
    delivered_record = InputRecord(
        input=delivered,
        reserved_to=invocation,
        receipt=InputDelivered(
            input_id=delivered.input_id, invocation=invocation, observation=_observation()
        ),
    )
    view = SessionView(
        spec=SessionSpec(
            session_id=invocation.session_id,
            role_id=RoleId(root="role"),
            policy="reuse",
            lifetime="owner",
            access=Access.WRITE_ARTIFACTS,
        ),
        scope=scope,
        generation=0,
        phase=SessionPhase.EXECUTING,
        invocation=invocation.invocation_id,
    )
    sessions = SessionsState(sessions=(view,), inputs=(*records, delivered_record))
    projections = session_view(sessions)
    expected = tuple(
        record.input.artifact
        for record in sorted(
            records, key=lambda record: (record.input.sequence, record.input.input_id.root)
        )
    )
    assert projections[0].reserved_inputs == expected
    state = initial_state().model_copy(update={"sessions": sessions})
    assert project(state).sessions == projections
    assert "reserved_inputs" not in sessions.model_dump()["sessions"][0]
    assert project(state).inputs == sessions.inputs


def _normalize_scoped_reopen(request: OperationRequest) -> ScopeReopenNormalization:
    assert isinstance(request, ScopedAdmissionReopen)
    return ScopeReopenNormalization(
        attempt=request.attempt,
        continuation_id=request.continuation_id,
        park_authority=request.park_authority,
        resolved_cancelled_jobs=request.resolved_cancelled_jobs,
    )


@given(jobs=st.integers(min_value=0, max_value=10))
def test_authoritative_reopen_operation_codec_and_normalization(jobs: int) -> None:
    descriptor = OperationDescriptor(
        kind="evaluation.scope.reopen",
        request_schema=SchemaRef(name="scope-reopen", version=1),
        outcome_schema=SchemaRef(name="scope-reopened", version=1),
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        inspect=True,
        normalization=OperationNormalizationKind.SCOPE_REOPEN,
    )
    registry = OperationRegistry(
        (
            OperationRegistration(
                descriptor=descriptor,
                request_model=ScopedAdmissionReopen,
                outcome_model=ScopedAdmissionReopenOutcome,
                normalize_scope_reopen=_normalize_scoped_reopen,
            ),
        )
    )
    request = ScopedAdmissionReopen(
        attempt=AttemptRef(attempt_id=AttemptId(root="attempt"), generation=0),
        continuation_id=ContinuationId(root="wait"),
        park_authority=RequestId(root="park"),
        resolved_cancelled_jobs=tuple(ResourceId(root=f"job-{i}") for i in range(jobs)),
    )
    assert registry.decode(registry.encode(request)) == request
    decision = registry.validate_decision(
        Operation(
            decision_id=DecisionId(root="reopen"),
            scope=Scope(owner=RunId(root="run"), generation=0),
            request=request,
            deadline_at=10.0,
        )
    )
    assert decision.registered_scope_reopen == _normalize_scoped_reopen(request)
    assert decision.registered_wire == registry.encode(request)


@pytest.mark.parametrize("admission", ["reopened", "closed", "unknown"])
def test_reopen_outcome_keeps_positive_and_ambiguous_admission_distinct(admission: str) -> None:
    outcome = ScopedAdmissionReopenOutcome.model_validate(
        {"scope": Scope(owner=RunId(root="run"), generation=0), "admission": admission}
    )
    assert ScopedAdmissionReopenOutcome.model_validate_json(outcome.model_dump_json()) == outcome
