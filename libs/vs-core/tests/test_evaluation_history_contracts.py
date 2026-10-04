"""Attempt-wide bound evidence validates at public contracts and codec boundaries."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    ArtifactId,
    ArtifactRef,
    AttemptBudget,
    AttemptEvaluationHistory,
    AttemptEvaluationHistoryUpdated,
    AttemptEvaluationRecord,
    AttemptExhausted,
    AttemptId,
    AttemptPhase,
    AttemptRef,
    AttemptsState,
    AttemptTerminalReason,
    AttemptView,
    BenchmarkFailure,
    Continuation,
    ContinuationId,
    ContinuationJobsChanged,
    ContinuationPhase,
    CoreState,
    EvaluationHistoryAvailability,
    EvaluationHistoryCursor,
    EvaluationTerminalFacts,
    EventCursor,
    EventId,
    HostFence,
    HostId,
    Intent,
    IntentPhase,
    InvocationId,
    InvocationRef,
    ItemId,
    JobObserved,
    JobProgress,
    JobTimeout,
    LifecycleClass,
    MeasurementPlan,
    Observation,
    ObservationStatus,
    ObservedJobFacts,
    OperationId,
    OperationRegistry,
    ProposalSubmitted,
    RegisteredJobObserved,
    RepeatedFailureGuidance,
    RequestId,
    RequestObserved,
    ResourceId,
    ResumeAuthorizationReceipt,
    ResumeAuthorized,
    RunEnvelope,
    Scope,
    SessionId,
    StrategyEvent,
    StrategyState,
    SubmitMeasurement,
    TargetObservation,
    TimedOut,
    UnobservedJobFacts,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
    project,
    step,
)


def record(ordinal: int, generation: int = 0) -> AttemptEvaluationRecord:
    """Build one fully correlated accepted terminal measurement receipt."""
    request = RequestId(root=f"submission-{ordinal}")
    scope = Scope(owner=AttemptId(root="attempt"), generation=generation)
    return AttemptEvaluationRecord(
        ordinal=ordinal,
        submission_id=request,
        scope=scope,
        terminal_observation=Observation(
            event_id=EventId(root=f"terminal-{ordinal}"),
            request_id=request,
            scope=scope,
            sequence=ordinal,
            observed_at=float(ordinal),
            status=ObservationStatus.FAILED,
            accepted=True,
            terminal=True,
        ),
        traceback_signature="same-stack",
        failed_benchmark=BenchmarkFailure(partial_rate=2.0, rate_lower=1.0, rate_upper=3.0),
    )


def history(records: tuple[AttemptEvaluationRecord, ...]) -> AttemptEvaluationHistory:
    """Positively certify exact ordered coverage, including the empty initial set."""
    return AttemptEvaluationHistory(
        availability=EvaluationHistoryAvailability.COMPLETE,
        covered_submissions=tuple(item.submission_id for item in records),
        records=records,
    )


def state_with_history(receipts: AttemptEvaluationHistory) -> CoreState:
    state = initial_state()
    attempt = AttemptView(
        attempt_id=AttemptId(root="attempt"),
        item_id=ItemId(root="item"),
        generation=0,
        phase=AttemptPhase.ACTIVE,
        workspace=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline),
        budget=AttemptBudget(),
        evaluation_history=receipts,
        terminal_reason=AttemptTerminalReason.REPEATED_TRACEBACK,
    )
    return state.model_copy(update={"attempts": AttemptsState(attempts=(attempt,))})


@given(st.integers(min_value=0, max_value=12))
def test_durable_history_cursor_and_terminal_reason_survive_step_and_codec(count: int) -> None:
    receipts = history(tuple(record(index) for index in range(1, count + 1)))
    state = state_with_history(receipts)
    before = state.model_dump_json()
    result = step(state, ProposalSubmitted(decisions=(), expected_revision=state.revision))
    assert result.state.attempts == state.attempts
    assert project(result.state).attempts[0].evaluation_history == receipts
    assert state.model_dump_json() == before
    envelope = RunEnvelope[StrategyState](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        fence=HostFence(host_id=HostId(root="host"), epoch=1),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=result.state,
        strategy=StrategyState(schema_version=1),
        event_cursor=EventCursor(sequence=0),
    )
    codec = OperationRegistry()
    restored = codec.decode_envelope(RunEnvelope[StrategyState], codec.encode_envelope(envelope))
    assert restored == envelope
    assert restored.core.attempts.attempts[0].evaluation_history.cursor.ordinal == count
    assert (
        restored.core.attempts.attempts[0].terminal_reason
        == AttemptTerminalReason.REPEATED_TRACEBACK
    )


@given(st.integers(min_value=1, max_value=12))
def test_complete_coverage_cannot_hide_a_missing_terminal_submission(count: int) -> None:
    records = tuple(record(index) for index in range(1, count + 1))
    manifest = tuple(item.submission_id for item in records)
    with pytest.raises(ValidationError, match="complete history"):
        AttemptEvaluationHistory(
            availability=EvaluationHistoryAvailability.COMPLETE,
            covered_submissions=manifest,
            records=records[:-1],
        )
    unavailable = AttemptEvaluationHistory(covered_submissions=manifest, records=records[:-1])
    assert unavailable.availability == EvaluationHistoryAvailability.UNAVAILABLE
    assert unavailable.cursor.submission_id == manifest[-1]


@given(st.integers(min_value=1, max_value=12))
def test_history_duplicates_and_submission_identity_substitution_are_rejected(count: int) -> None:
    receipts = tuple(record(index) for index in range(1, count + 1))
    with pytest.raises(ValidationError, match="duplicate submission"):
        history((*receipts, receipts[-1]))
    with pytest.raises(ValidationError, match="coverage ordinal"):
        AttemptEvaluationHistory(
            covered_submissions=(RequestId(root="wrong"),), records=(receipts[0],)
        )


@pytest.mark.parametrize("field", ["accepted", "terminal", "request_id", "scope", "status"])
def test_terminal_history_requires_positive_correlated_receipt(field: str) -> None:
    item = record(1)
    changes = {
        "accepted": False,
        "terminal": False,
        "request_id": RequestId(root="other"),
        "scope": Scope(owner=AttemptId(root="other"), generation=0),
        "status": ObservationStatus.UNKNOWN,
    }
    payload = item.model_dump()
    payload["terminal_observation"][field] = changes[field]
    with pytest.raises(ValidationError, match="terminal_observation"):
        AttemptEvaluationRecord.model_validate(payload)


@given(st.integers(min_value=1, max_value=20))
def test_history_receipts_cannot_cross_attempt_generation(generation: int) -> None:
    receipts = history((record(1, generation),))
    with pytest.raises(ValidationError, match="attempt scope"):
        state_with_history(receipts)
    with pytest.raises(ValidationError, match="attempt scope"):
        AttemptEvaluationHistoryUpdated(
            attempt=AttemptRef(attempt_id=AttemptId(root="attempt"), generation=0),
            history=receipts,
        )


@pytest.mark.parametrize(("ordinal", "submission"), [(0, RequestId(root="wrong")), (1, None)])
def test_cursor_absence_cannot_prove_a_preceding_submission(
    ordinal: int, submission: object
) -> None:
    with pytest.raises(ValidationError, match="cursor ordinal"):
        EvaluationHistoryCursor.model_validate({"ordinal": ordinal, "submission_id": submission})
    assert AttemptEvaluationHistory().availability == EvaluationHistoryAvailability.UNAVAILABLE


@given(st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False))
def test_failed_benchmark_retains_a_checkable_comparison_range(rate: float) -> None:
    partial = BenchmarkFailure(partial_rate=rate, rate_lower=rate, rate_upper=rate + 1.0)
    assert BenchmarkFailure.model_validate_json(partial.model_dump_json()) == partial
    with pytest.raises(ValidationError, match="rate range"):
        BenchmarkFailure(partial_rate=rate, rate_lower=rate + 1.0, rate_upper=rate + 2.0)


@given(
    st.sampled_from(
        [ContinuationPhase.PARKED, ContinuationPhase.REOPENING, ContinuationPhase.RESUMED]
    )
)
def test_resume_publication_proof_survives_phase_changes_and_reload(
    phase: ContinuationPhase,
) -> None:
    invocation = InvocationRef(
        session_id=SessionId(root="session"), invocation_id=InvocationId(root="next"), generation=0
    )
    receipt = ResumeAuthorizationReceipt(
        continuation_id=ContinuationId(root="wait"),
        next_invocation=invocation,
        evidence=(),
        history_cursor=history((record(1),)).cursor,
    )
    continuation = Continuation(
        continuation_id=receipt.continuation_id,
        invocation=invocation.model_copy(update={"invocation_id": InvocationId(root="previous")}),
        next_invocation=invocation,
        jobs=(),
        deadline_at=10.0,
        phase=phase,
        authorization_receipt=receipt,
    )
    assert (
        Continuation.model_validate_json(continuation.model_dump_json()).authorization_receipt
        == receipt
    )
    with pytest.raises(ValidationError, match="successor mismatch"):
        Continuation.model_validate(
            {
                **continuation.model_dump(),
                "next_invocation": invocation.model_copy(update={"generation": 1}),
            }
        )


@given(st.integers(min_value=1, max_value=30))
def test_preupdate_job_receipt_keeps_deadline_facts_independent_of_new_terminal_fact(
    sequence: int,
) -> None:
    resource = ResourceId(root="job")
    prior = record(1).terminal_observation.model_copy(
        update={
            "sequence": sequence,
            "resource_id": resource,
            "observed_at": 9.0,
            "terminal": False,
            "status": ObservationStatus.PENDING,
        }
    )
    progress = JobProgress(observation_sequence=sequence, observed_at=9.0, state="running")
    incoming = prior.model_copy(
        update={
            "sequence": sequence + 1,
            "observed_at": 10.0,
            "terminal": True,
            "status": ObservationStatus.SUCCEEDED,
        }
    )
    event = ContinuationJobsChanged(
        resource_id=resource,
        observation=incoming,
        previous=ObservedJobFacts(resource_id=resource, observation=prior, progress=progress),
    )
    restored = ContinuationJobsChanged.model_validate_json(event.model_dump_json())
    assert restored.observation_sequence == sequence + 1
    assert restored.observed_at == 10.0
    assert isinstance(restored.previous, ObservedJobFacts)
    assert not restored.previous.observation.terminal
    assert restored.previous.progress == progress
    assert restored.observation.terminal
    with pytest.raises(ValidationError, match="sequence does not precede"):
        ContinuationJobsChanged(
            resource_id=resource,
            observation=incoming.model_copy(update={"sequence": sequence}),
            previous=event.previous,
        )


@pytest.mark.parametrize("missing", ["observation", "previous"])
def test_job_wake_cannot_omit_the_carrying_or_preupdate_fact(missing: str) -> None:
    resource = ResourceId(root="job")
    event = ContinuationJobsChanged(
        resource_id=resource,
        observation=record(1).terminal_observation.model_copy(update={"resource_id": resource}),
        previous=UnobservedJobFacts(resource_id=resource),
    )
    payload = event.model_dump()
    del payload[missing]
    with pytest.raises(ValidationError, match=missing):
        ContinuationJobsChanged.model_validate(payload)


@given(st.integers(min_value=1, max_value=12))
def test_repeated_failure_guidance_requires_positive_typed_evidence(count: int) -> None:
    cursor = EvaluationHistoryCursor(ordinal=count, submission_id=RequestId(root="last"))
    guidance = RepeatedFailureGuidance(
        reason=AttemptTerminalReason.REPEATED_TRACEBACK,
        consecutive_failures=count,
        limit=3,
        cursor=cursor,
        traceback_signature="same-stack",
    )
    assert RepeatedFailureGuidance.model_validate_json(guidance.model_dump_json()) == guidance
    with pytest.raises(ValidationError, match="traceback_signature"):
        RepeatedFailureGuidance.model_validate(
            {**guidance.model_dump(), "traceback_signature": None}
        )
    with pytest.raises(ValidationError, match="preceding submissions"):
        RepeatedFailureGuidance.model_validate(
            {**guidance.model_dump(), "consecutive_failures": count + 1}
        )


@given(st.integers(max_value=0))
def test_repeated_failure_bound_is_explicit_and_positive(limit: int) -> None:
    with pytest.raises(ValidationError, match="repeated_failure_limit"):
        AttemptBudget(repeated_failure_limit=limit)


@given(st.integers(min_value=1, max_value=20))
def test_preupdate_sequence_is_correlated_per_source_without_cross_source_ordering(
    sequence: int,
) -> None:
    resource = ResourceId(root="job")
    prior = record(1).terminal_observation.model_copy(
        update={
            "request_id": RequestId(root="source-a"),
            "resource_id": resource,
            "sequence": sequence,
        }
    )
    incoming = prior.model_copy(update={"request_id": RequestId(root="source-b"), "sequence": 0})
    event = ContinuationJobsChanged(
        resource_id=resource,
        observation=incoming,
        previous=ObservedJobFacts(resource_id=resource, observation=prior),
    )
    assert ContinuationJobsChanged.model_validate_json(event.model_dump_json()) == event
    with pytest.raises(ValidationError, match="time follows"):
        ContinuationJobsChanged(
            resource_id=resource,
            observation=incoming.model_copy(update={"observed_at": 0.0}),
            previous=event.previous,
        )


@given(st.integers(min_value=1, max_value=12))
def test_positive_preacceptance_failure_keeps_complete_history_without_scientific_proof(
    count: int,
) -> None:
    records = tuple(record(index) for index in range(1, count + 1))
    final = records[-1].model_copy(
        update={
            "terminal_observation": records[-1].terminal_observation.model_copy(
                update={"accepted": False}
            ),
            "traceback_signature": None,
            "failed_benchmark": None,
        }
    )
    validated = AttemptEvaluationRecord.model_validate_json(final.model_dump_json())
    receipts = history((*records[:-1], validated))
    assert receipts.availability == EvaluationHistoryAvailability.COMPLETE
    assert receipts.cursor.submission_id == final.submission_id
    assert AttemptEvaluationHistory.model_validate_json(receipts.model_dump_json()) == receipts
    with pytest.raises(ValidationError, match="scientific execution"):
        AttemptEvaluationRecord.model_validate({**validated.model_dump(), "accuracy_passed": True})


@given(st.floats(min_value=10.0, max_value=100.0, allow_nan=False, allow_infinity=False))
def test_publication_receipt_cannot_rewrite_frozen_timeout(reached: float) -> None:
    invocation = InvocationRef(
        session_id=SessionId(root="session"), invocation_id=InvocationId(root="next"), generation=0
    )
    timeout = TimedOut(
        deadline_at=10.0,
        reached_at=reached,
        unfinished=(JobTimeout(resource_id=ResourceId(root="job")),),
    )
    receipt = ResumeAuthorizationReceipt(
        continuation_id=ContinuationId(root="wait"),
        next_invocation=invocation,
        evidence=(),
        history_cursor=EvaluationHistoryCursor(),
        timeout=timeout,
    )
    continuation = Continuation(
        continuation_id=receipt.continuation_id,
        invocation=invocation,
        next_invocation=invocation,
        jobs=(ResourceId(root="job"),),
        deadline_at=10.0,
        phase=ContinuationPhase.PARKED,
        authorization_receipt=receipt,
        timeout=timeout,
    )
    assert Continuation.model_validate_json(continuation.model_dump_json()) == continuation
    for replacement in (None, timeout.model_copy(update={"reached_at": reached + 1.0})):
        with pytest.raises(ValidationError, match="frozen timeout"):
            Continuation.model_validate({**continuation.model_dump(), "timeout": replacement})


@pytest.mark.parametrize(
    "carrier", [JobObserved, RegisteredJobObserved, RequestObserved, TargetObservation, Intent]
)
@given(
    accepted=st.booleans(), terminal=st.booleans(), status=st.sampled_from(tuple(ObservationStatus))
)
def test_scientific_terminal_ingress_requires_positive_execution_source(
    carrier: type[
        JobObserved | RegisteredJobObserved | RequestObserved | TargetObservation | Intent
    ],
    *,
    accepted: bool,
    terminal: bool,
    status: ObservationStatus,
) -> None:
    observation = record(1).terminal_observation.model_copy(
        update={
            "accepted": accepted,
            "terminal": terminal,
            "status": status,
            "resource_id": ResourceId(root="job"),
        }
    )
    payload: dict[str, object] = {
        "observation": observation,
        "evaluation_result": EvaluationTerminalFacts(traceback_signature="same-stack"),
    }
    if carrier is JobObserved:
        payload["resource_id"] = ResourceId(root="job")
    elif carrier is RegisteredJobObserved:
        payload["operation_id"] = OperationId(root="operation")
    elif carrier is Intent:
        payload.update(
            request_id=observation.request_id,
            request=SubmitMeasurement(
                request_id=observation.request_id,
                scope=observation.scope,
                deadline_at=100.0,
                plan=MeasurementPlan(
                    purpose="official",
                    candidate=initial_state().run.facts.baseline,
                    evaluator_digest="evaluator",
                    workload_digest="workload",
                    environment_digest="environment",
                    stages=(),
                    policy="ordered",
                    recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
                    submitted_at=0.0,
                    queue_allowance=100.0,
                    deadline_at=100.0,
                ),
            ),
            payload_digest="payload",
            lifecycle=LifecycleClass.OWNED_JOB,
            phase=IntentPhase.COMPLETED,
            reconcile_deadline_at=100.0,
        )
        with pytest.raises(ValidationError, match="evaluation_result"):
            Intent.model_validate({**payload, "observation": None})
    if (
        accepted
        and terminal
        and status not in (ObservationStatus.PENDING, ObservationStatus.UNKNOWN)
    ):
        event = carrier.model_validate(payload)
        assert carrier.model_validate_json(event.model_dump_json()) == event
        assert event.evaluation_result is not None
        assert event.evaluation_result.traceback_signature == "same-stack"
    else:
        with pytest.raises(ValidationError, match="evaluation_result"):
            carrier.model_validate(payload)
    # Missing typed science is a representable unavailable result, never inferred execution facts.
    payload["evaluation_result"] = None
    assert carrier.model_validate(payload).evaluation_result is None


@given(st.sampled_from(tuple(AttemptTerminalReason)))
def test_typed_attempt_exhaustion_feedback_roundtrips_through_strategy_event_codec(
    reason: AttemptTerminalReason,
) -> None:
    feedback = AttemptExhausted(
        attempt=AttemptRef(attempt_id=AttemptId(root="attempt"), generation=0), reason=reason
    )
    codec = TypeAdapter(StrategyEvent)
    restored = codec.validate_json(codec.dump_json(feedback))
    assert restored == feedback
    assert isinstance(restored, AttemptExhausted)
    assert restored.reason == reason
    assert isinstance(restored.reason, AttemptTerminalReason)


@pytest.mark.parametrize("publication", [ResumeAuthorizationReceipt, ResumeAuthorized])
@given(st.integers(min_value=1, max_value=20), st.integers(min_value=1, max_value=20))
def test_repeated_failure_guidance_requires_exact_publication_cursor(
    publication: type[ResumeAuthorizationReceipt | ResumeAuthorized],
    ordinal: int,
    difference: int,
) -> None:
    cursor = EvaluationHistoryCursor(ordinal=ordinal, submission_id=RequestId(root="last"))
    guidance = RepeatedFailureGuidance(
        reason=AttemptTerminalReason.REPEATED_TRACEBACK,
        consecutive_failures=ordinal,
        limit=3,
        cursor=cursor,
        traceback_signature="same-stack",
    )
    payload = {
        "continuation_id": ContinuationId(root="wait"),
        "next_invocation": InvocationRef(
            session_id=SessionId(root="session"),
            invocation_id=InvocationId(root="next"),
            generation=0,
        ),
        "evidence": (),
        "history_cursor": cursor,
        "repeated_failure": guidance,
    }
    positive = publication.model_validate(payload)
    assert publication.model_validate_json(positive.model_dump_json()) == positive
    for wrong in (
        EvaluationHistoryCursor(ordinal=ordinal + difference, submission_id=cursor.submission_id),
        EvaluationHistoryCursor(ordinal=ordinal, submission_id=RequestId(root="other")),
    ):
        with pytest.raises(ValidationError, match="publication cursor"):
            publication.model_validate({**payload, "history_cursor": wrong})


@given(st.integers(min_value=1, max_value=100), st.integers(min_value=0, max_value=100))
def test_deadline_snapshot_retains_older_progress_without_comparing_source_sequences(
    progress_sequence: int,
    latest_sequence: int,
) -> None:
    resource = ResourceId(root="job")
    latest = record(1).terminal_observation.model_copy(
        update={
            "request_id": RequestId(root="latest-source"),
            "resource_id": resource,
            "sequence": latest_sequence,
            "observed_at": 9.0,
            "terminal": False,
            "status": ObservationStatus.PENDING,
        }
    )
    retained = JobProgress(observation_sequence=progress_sequence, observed_at=7.0, state="running")
    event = ContinuationJobsChanged(
        resource_id=resource,
        observation=latest.model_copy(
            update={"sequence": latest_sequence + 1, "observed_at": 10.0}
        ),
        previous=ObservedJobFacts(resource_id=resource, observation=latest, progress=retained),
    )
    restored = ContinuationJobsChanged.model_validate_json(event.model_dump_json())
    assert isinstance(restored.previous, ObservedJobFacts)
    assert restored.previous.progress == retained
    assert restored.previous.progress.observation_sequence == progress_sequence
    assert restored.previous.progress.observed_at == 7.0
    with pytest.raises(ValidationError, match="latest prior observation"):
        ObservedJobFacts(
            resource_id=resource,
            observation=latest,
            progress=retained.model_copy(update={"observed_at": 11.0}),
        )
