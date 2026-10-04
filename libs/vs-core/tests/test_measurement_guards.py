"""Source, snapshot, reuse and historical ownership guard properties."""

from dataclasses import dataclass

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .proof_digest import value_digest
from .test_measurements import (
    committed,
    evidence,
    observation,
    plan,
    requested,
    roundtrip,
    submitted,
    transition,
)
from .test_registered_v3_contracts import (
    RegisteredMeasurement,
    expected_identity,
    measurement_codec,
)


def attempt_state() -> tuple[core.CoreState, core.Scope]:
    state = core.initial_state()
    scope = core.Scope(owner=core.AttemptId(root="attempt"), generation=0)
    owner = core.AttemptView(
        attempt_id=scope.owner,
        item_id=core.ItemId(root="item"),
        generation=0,
        phase=core.AttemptPhase.ACTIVE,
        admission_id=core.DecisionId(root="admitted"),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )
    return state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))}), scope


def measure_scope(
    state: core.CoreState, scope: core.Scope, measurement: core.MeasurementPlan
) -> core.Transition:
    decision = core.Measure(
        decision_id=core.DecisionId(root="scoped-measure"), scope=scope, plan=measurement
    )
    return transition(
        state, core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
    )


@given(
    admission=st.booleans(),
    generation=st.integers(min_value=0, max_value=3),
    phase=st.sampled_from(list(core.AttemptPhase)),
    terminal_reason=st.one_of(st.none(), st.sampled_from(list(core.AttemptTerminalReason))),
)
def test_new_submissions_need_current_active_episode(
    *,
    admission: bool,
    generation: int,
    phase: core.AttemptPhase,
    terminal_reason: core.AttemptTerminalReason | None,
) -> None:
    state, scope = attempt_state()
    owner = state.attempts.attempts[0].model_copy(
        update={
            "admission_id": core.DecisionId(root="admitted") if admission else None,
            "phase": phase,
            "generation": generation,
            "terminal_reason": terminal_reason,
        }
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    result = measure_scope(state, scope, plan())
    assert bool(result.requests) == (
        admission
        and generation == 0
        and phase == core.AttemptPhase.ACTIVE
        and terminal_reason is None
    )
    assert result.state.attempts == state.attempts


@given(
    accepted=st.booleans(),
    terminal=st.booleans(),
    status=st.sampled_from(list(core.ObservationStatus)),
    checkpoint=st.booleans(),
    episodes=st.tuples(st.booleans(), st.booleans()),
)
def test_snapshot_result_requires_exact_committed_checkpoint(
    *,
    accepted: bool,
    terminal: bool,
    status: core.ObservationStatus,
    checkpoint: bool,
    episodes: tuple[bool, bool],
) -> None:
    request_episode, observed_episode = episodes
    state, scope = attempt_state()
    owner = state.attempts.attempts[0]
    ref = core.AttemptRef(attempt_id=owner.attempt_id, generation=0)
    request_id = core.RequestId(root="snapshot")
    revision = core.RevisionRef(
        revision_id=core.RevisionId(root="captured"), digest="captured-exactly"
    )
    request = core.SnapshotAndRetain(
        request_id=request_id,
        scope=scope,
        admission_id=owner.admission_id if request_episode else None,
        attempt=ref,
        retention="candidate",
        deadline_at=100.0,
    )
    observed = core.Observation(
        event_id=core.EventId(root="snapshot-done"),
        request_id=request_id,
        scope=scope,
        admission_id=owner.admission_id if observed_episode else None,
        sequence=1,
        observed_at=1.0,
        accepted=accepted,
        terminal=terminal,
        status=status,
    )
    source = core.Intent(
        request_id=request_id,
        request=request,
        observation=observed,
        payload_digest=value_digest(request),
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.COMPLETED,
        reconcile_deadline_at=100.0,
    )
    owner = owner.model_copy(
        update={
            "checkpoints": (
                core.AttemptCheckpoint(
                    request_id=request_id, invocation=None, revision=revision, retention="candidate"
                ),
            )
            if checkpoint
            else ()
        }
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(update={"intents": (source,)}),
        }
    )
    measurement = plan(candidate=core.SnapshotResultRef(request_id=request_id))
    result = measure_scope(state, scope, measurement)
    allowed = (
        accepted
        and terminal
        and status == core.ObservationStatus.SUCCEEDED
        and checkpoint
        and request_episode
        and observed_episode
    )
    assert bool(result.requests) == allowed
    if allowed:
        assert isinstance(result.requests[0], core.SubmitMeasurement)
        assert result.requests[0].plan.candidate == revision
        assert result.state.evaluation.submission_budgets[0].identity.candidate == revision


def profile_completed() -> tuple[core.CoreState, core.SubmitMeasurement, core.EvidenceRef]:
    measurement = plan(
        purpose="profile",
        stages=(core.MeasurementStage(stage_id="capture", execution_budget=90.0),),
    )
    state, request = submitted(measurement=measurement)
    observed = observation(
        request,
        2,
        status=core.ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
        children_complete=True,
    )
    facts = core.EvaluationTerminalFacts(
        stages=(
            core.EvaluationStageResult(
                stage_id="capture", outcome=core.EvaluationStageOutcome.PASSED
            ),
        )
    )
    capture = evidence(
        request,
        observed,
        identity="capture",
        kind=core.EvidenceKind.PROFILING,
        status=core.ObservationStatus.SUCCEEDED,
    )
    result = transition(
        committed(state, observed, facts),
        core.JobObserved(
            resource_id=core.ResourceId(root="job"),
            observation=observed,
            evidence=(capture,),
            evaluation_result=facts,
        ),
    )
    return result.state, request, result.state.evaluation.evidence[0]


@given(
    field=st.sampled_from(
        (
            None,
            "candidate",
            "workload_digest",
            "evaluator_digest",
            "environment_digest",
            "recipe",
            "stages",
        )
    ),
    hints=st.booleans(),
)
def test_r23_late_profile_reuses_only_exact_capture_identity(
    *, field: str | None, hints: bool
) -> None:
    state, source, capture = profile_completed()
    replacements = {
        "candidate": core.RevisionRef(
            revision_id=core.RevisionId(root="another"), digest="another"
        ),
        "workload_digest": "benchmark-shaped-other",
        "evaluator_digest": "other",
        "environment_digest": "other",
        "recipe": core.ArtifactRef(artifact_id=core.ArtifactId(root="other"), digest="other"),
        "stages": (core.MeasurementStage(stage_id="other", execution_budget=90.0),),
    }
    updates = {
        "submitted_at": 3.0,
        "deadline_at": 103.0,
        "reusable_evidence": (capture.evidence_id,) if hints else (),
    }
    if field is not None:
        updates[field] = replacements[field]
    result = requested(
        state, identity="interpret-completed", measurement=source.plan.model_copy(update=updates)
    )
    assert core.project(result.state).measurements == (capture,)
    if field is None:
        assert result.requests == ()
        feedback = next(e for e in result.events if isinstance(e, core.MeasurementResult))
        assert feedback.evidence == (capture,)
        assert result.state.evaluation.submission_budgets == state.evaluation.submission_budgets
    else:
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], core.SubmitMeasurement)
        assert capture not in next(
            (e.evidence for e in result.events if isinstance(e, core.MeasurementResult)), ()
        )


@given(
    receipt=st.booleans(),
    provenance=st.sampled_from(("trusted", "self-report")),
    status=st.sampled_from(list(core.ObservationStatus)),
    terminal=st.booleans(),
)
def test_capture_without_final_trusted_source_never_grants_reuse(
    *, receipt: bool, provenance: str, status: core.ObservationStatus, terminal: bool
) -> None:
    state, source, capture = profile_completed()
    acceptance = capture.acceptance_receipt
    assert acceptance is not None
    # A historical reference has no receipt; a same-status receipt remains valid
    # even when it does not certify terminal scientific completion.
    if status == core.ObservationStatus.UNKNOWN:
        receipt = False
    acceptance = (
        core.EvidenceAcceptanceReceipt(
            observation=acceptance.observation.model_copy(
                update={"status": status, "terminal": terminal}
            )
        )
        if receipt
        else None
    )
    capture = capture.model_copy(
        update={"acceptance_receipt": acceptance, "provenance": provenance, "status": status}
    )
    job = state.evaluation.jobs[0].model_copy(update={"evidence": (capture,)})
    state = state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(
                update={"evidence": (capture,), "jobs": (job,)}
            )
        }
    )
    result = requested(
        state,
        identity="reuse",
        measurement=source.plan.model_copy(update={"submitted_at": 3.0, "deadline_at": 103.0}),
    )
    reused = (
        receipt
        and provenance == "trusted"
        and status == core.ObservationStatus.SUCCEEDED
        and terminal
    )
    feedback = next(e for e in result.events if isinstance(e, core.MeasurementResult))
    assert (feedback.evidence == (capture,)) == reused
    assert result.requests == ()


@given(
    failed_partial=st.booleans(),
    accuracy_passed=st.booleans(),
    kind=st.sampled_from(list(core.EvidenceKind)),
)
def test_scientific_failure_cannot_be_reported_as_passing_execution(
    *, failed_partial: bool, accuracy_passed: bool, kind: core.EvidenceKind
) -> None:
    state, request = submitted()
    observed = observation(request, 2, terminal=True, status=core.ObservationStatus.SUCCEEDED)
    facts = core.EvaluationTerminalFacts(
        stages=(
            core.EvaluationStageResult(
                stage_id="benchmark", outcome=core.EvaluationStageOutcome.PASSED
            ),
        ),
        accuracy_passed=accuracy_passed,
        failed_benchmark=core.BenchmarkFailure(
            partial_rate=79.835, rate_lower=64.0, rate_upper=128.0
        )
        if failed_partial
        else None,
    )
    claimed = evidence(
        request,
        observed,
        identity="claimed-pass",
        kind=kind,
        status=core.ObservationStatus.SUCCEEDED,
    )
    result = transition(
        committed(state, observed, facts),
        core.JobObserved(
            resource_id=core.ResourceId(root="job"),
            observation=observed,
            evidence=(claimed,),
            evaluation_result=facts,
        ),
    )
    allowed = (
        accuracy_passed
        if kind == core.EvidenceKind.CORRECTNESS
        else not failed_partial
        if kind == core.EvidenceKind.BENCHMARK
        else True
    )
    assert bool(core.project(result.state).measurements) == allowed


@given(
    fault=st.sampled_from(("receipt", "membership", "payload", "budget", "sequence", "resource"))
)
def test_ingress_requires_independent_canonical_submission_proof(fault: str) -> None:
    state, request = submitted()
    observed = observation(request, 2)
    state = committed(state, observed)
    if fault == "receipt":
        state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": ()})})
    elif fault == "membership":
        receipt = state.run.receipts[0].model_copy(update={"request_ids": ()})
        state = state.model_copy(
            update={"run": state.run.model_copy(update={"receipts": (receipt,)})}
        )
    elif fault == "payload":
        row = state.intents.intents[0].model_copy(update={"payload_digest": "wrong"})
        state = state.model_copy(
            update={
                "intents": state.intents.model_copy(
                    update={"intents": (row, *state.intents.intents[1:])}
                )
            }
        )
    elif fault == "budget":
        state = state.model_copy(
            update={"evaluation": state.evaluation.model_copy(update={"submission_budgets": ()})}
        )
    elif fault == "sequence":
        observed = observation(request, 0)
        state = committed(state, observed)
    else:
        observed = observed.model_copy(update={"resource_id": core.ResourceId(root="foreign")})
        state = committed(state, observed)
    result = transition(
        state, core.JobObserved(resource_id=core.ResourceId(root="job"), observation=observed)
    )
    assert result.state.evaluation == state.evaluation
    assert result.requests == ()


@given(
    status=st.sampled_from((core.ObservationStatus.UNKNOWN, core.ObservationStatus.PENDING)),
    terminal=st.booleans(),
    released=st.booleans(),
)
def test_later_observations_cannot_regress_terminal_or_release_facts(
    *, status: core.ObservationStatus, terminal: bool, released: bool
) -> None:
    state, request, _ = profile_completed()
    observed = observation(request, 3, status=status, terminal=terminal, released=released)
    result = transition(
        committed(state, observed),
        core.JobObserved(resource_id=core.ResourceId(root="job"), observation=observed),
    )
    assert result.state.evaluation.jobs == state.evaluation.jobs
    assert core.project(result.state).measurements == core.project(state).measurements


@given(generation=st.integers(min_value=1, max_value=8))
def test_late_old_generation_retains_science_without_submitting_or_mutating_current_owner(
    generation: int,
) -> None:
    state, scope = attempt_state()
    submitted_result = measure_scope(state, scope, plan())
    request = submitted_result.requests[0]
    assert isinstance(request, core.SubmitMeasurement)
    observed = observation(request)
    state = transition(
        committed(submitted_result.state, observed),
        core.MeasurementSubmissionObserved(observation=observed),
    ).state
    owner = state.attempts.attempts[0].model_copy(
        update={"generation": generation, "admission_id": core.DecisionId(root="new-episode")}
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    terminal = observation(
        request, 2, terminal=True, released=True, status=core.ObservationStatus.FAILED
    )
    facts = core.EvaluationTerminalFacts(
        failed_benchmark=core.BenchmarkFailure(
            partial_rate=79.835, rate_lower=64.0, rate_upper=128.0
        ),
        accuracy_passed=True,
    )
    partial = evidence(
        request,
        terminal,
        identity="late-partial",
        kind=core.EvidenceKind.BENCHMARK,
        status=core.ObservationStatus.FAILED,
    )
    result = transition(
        committed(state, terminal, facts),
        core.JobObserved(
            resource_id=core.ResourceId(root="job"),
            observation=terminal,
            evidence=(partial,),
            evaluation_result=facts,
        ),
    )
    assert core.project(result.state).measurements[0].artifacts == partial.artifacts
    assert result.state.attempts == state.attempts
    assert result.requests == ()
    assert not any(isinstance(e, core.ResumeAuthorized) for e in result.events)


def registered_state(
    *, normalized: bool
) -> tuple[core.CoreState, core.RegisteredJobRequested, core.OperationRegistry]:
    codec = measurement_codec(normalized=normalized)
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    identity = expected_identity("evaluator")
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="registered"),
            scope=scope,
            request=RegisteredMeasurement(identity=identity),
            deadline_at=100.0,
        )
    )
    wire = decision.registered_wire
    assert wire is not None
    request_id = core.RequestId(root="operation:registered")
    request = core.ExecuteRegisteredOperation(
        request_id=request_id,
        decision_id=decision.decision_id,
        operation_id=core.OperationId(root="operation:registered"),
        scope=scope,
        deadline_at=100.0,
        operation=wire,
        retry_limit=0,
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=value_digest(decision),
        feedback=core.Accepted(decision_id=decision.decision_id),
        request_ids=(request_id,),
    )
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                    "receipts": (receipt,),
                }
            ),
        }
    )
    return (
        state,
        core.RegisteredJobRequested(
            request=request,
            resource_pool=core.PoolId(root="jobs"),
            expected_measurement=identity if normalized else None,
        ),
        codec,
    )


@given(normalized=st.booleans(), incoming_identity=st.booleans())
def test_registered_jobs_cannot_infer_expected_identity_from_evidence(
    *, normalized: bool, incoming_identity: bool
) -> None:
    state, event, codec = registered_state(normalized=normalized)
    result = transition(state, event, codec=codec)
    assert len(result.state.evaluation.registered_jobs) == 1
    request = event.request
    assert request.request_id is not None
    observed = core.Observation(
        event_id=core.EventId(root="generic-terminal"),
        request_id=request.request_id,
        scope=request.scope,
        sequence=1,
        observed_at=2.0,
        status=core.ObservationStatus.SUCCEEDED,
        resource_id=core.ResourceId(root="generic"),
        accepted=True,
        terminal=True,
        released=True,
    )
    source = next(r for r in result.state.intents.intents if r.request_id == request.request_id)
    facts = (
        core.EvaluationTerminalFacts(
            stages=(
                core.EvaluationStageResult(
                    stage_id="benchmark", outcome=core.EvaluationStageOutcome.PASSED
                ),
            )
        )
        if normalized
        else None
    )
    state = result.state.model_copy(
        update={
            "intents": result.state.intents.model_copy(
                update={
                    "intents": (
                        source.model_copy(
                            update={"observation": observed, "evaluation_result": facts}
                        ),
                    )
                }
            )
        }
    )
    expected = expected_identity("evaluator")
    submitted_evidence = core.EvidenceRef(
        evidence_id=core.EvidenceId(root="generic-science"),
        kind=core.EvidenceKind.BENCHMARK,
        purpose=expected.purpose,
        scope=request.scope,
        source_request=request.request_id,
        candidate=expected.candidate,
        observation_sequence=1,
        evaluator_digest="evaluator" if incoming_identity else "forged",
        workload_digest=expected.workload_digest,
        environment_digest=expected.environment_digest,
        provenance="trusted",
        status=core.ObservationStatus.SUCCEEDED,
    )
    result = transition(
        state,
        core.RegisteredJobObserved(
            operation_id=request.operation_id,
            observation=observed,
            evidence=(submitted_evidence,),
            evaluation_result=facts,
        ),
        codec=codec,
    )
    assert bool(core.project(result.state).measurements) == (normalized and incoming_identity)
    assert result.state.evaluation.registered_jobs[0].resource_id == observed.resource_id
    assert (
        result.state.evaluation.registered_jobs[0].expected_measurement
        == event.expected_measurement
    )
    assert len(result.state.evaluation.submission_budgets) == int(normalized)


@dataclass(frozen=True)
class PreUpdateReceiver:
    """Strict pure receiving seam for A's public cross-area fact contract.

    It validates exact prior facts and publishes a diagnostic acknowledgement.
    It grants no continuation, execution, history or release authority.
    """

    expected: core.JobFactsBeforeObservation

    def __call__(
        self,
        state: core.EvaluationState,
        context: core.EvaluationContext,
        event: core.EvaluationEvent,
    ) -> core.AreaChange[core.EvaluationState]:
        if isinstance(event, core.ContinuationJobsChanged):
            if event.previous != self.expected:
                raise core.ContractValidationError(
                    "previous", "not the exact pre-update owner fact"
                )
            return core.AreaChange(
                state=state,
                events=(
                    core.MeasurementResult(
                        scope=event.observation.scope,
                        evidence=event.previous.evidence
                        if isinstance(event.previous, core.ObservedJobFacts)
                        else (),
                        status=core.ObservationStatus.UNKNOWN,
                    ),
                ),
            )
        return core.advance_evaluation(state, context, event)


@given(
    prior_observed=st.booleans(),
    progress=st.booleans(),
    late_at=st.sampled_from((9.0, 10.0, 11.0)),
    queued=st.one_of(st.none(), st.floats(min_value=0, max_value=2, allow_nan=False)),
    ran=st.one_of(st.none(), st.floats(min_value=0, max_value=2, allow_nan=False)),
)
def test_exact_and_late_wakes_carry_prior_observation_progress_and_evidence(
    *, prior_observed: bool, progress: bool, late_at: float, queued: float | None, ran: float | None
) -> None:
    state, request = submitted()
    job = state.evaluation.jobs[0]
    retained_progress = (
        core.JobProgress(
            observation_sequence=1,
            observed_at=1.0,
            state="running",
            stage_id="benchmark",
            queued_s=queued,
            ran_s=ran,
        )
        if progress and prior_observed
        else None
    )
    job = job.model_copy(
        update={
            "observation": job.observation if prior_observed else None,
            "progress": retained_progress,
        }
    )
    state = state.model_copy(
        update={"evaluation": state.evaluation.model_copy(update={"jobs": (job,)})}
    )
    assert job.resource_id is not None
    expected = (
        core.ObservedJobFacts(
            resource_id=job.resource_id,
            observation=job.observation,
            progress=job.progress,
            evidence=job.evidence,
        )
        if job.observation is not None
        else core.UnobservedJobFacts(resource_id=job.resource_id)
    )
    observed = observation(
        request, 2, observed_at=late_at, terminal=True, status=core.ObservationStatus.FAILED
    )
    receiver = PreUpdateReceiver(expected=expected)
    result = transition(
        committed(state, observed),
        core.JobObserved(resource_id=job.resource_id, observation=observed),
        reducers=core.CoreReducers(evaluation=receiver),
    )
    assert (
        sum(
            isinstance(e, core.MeasurementResult) and e.status == core.ObservationStatus.UNKNOWN
            for e in result.events
        )
        == 1
    )
    assert result.state.evaluation.jobs[0].observation == observed
    assert result.state.evaluation.jobs[0].progress == retained_progress
    # Duplicate source facts produce no second snapshot acknowledgement.
    replay = core.step(
        roundtrip(result.state),
        core.JobObserved(resource_id=job.resource_id, observation=observed),
        reducers=core.CoreReducers(evaluation=receiver),
    )
    assert replay.events == ()


@pytest.mark.parametrize("fault", ["scope", "normalization", "missing-request-id"])
def test_registered_request_conflicts_are_rejected_without_ownership(fault: str) -> None:
    state, event, codec = registered_state(normalized=True)
    if fault == "scope":
        event = event.model_copy(
            update={
                "request": event.request.model_copy(
                    update={"scope": core.Scope(owner=core.RunId(root="foreign"), generation=0)}
                )
            }
        )
    elif fault == "normalization":
        event = event.model_copy(update={"expected_measurement": None})
    else:
        event = event.model_copy(
            update={"request": event.request.model_copy(update={"request_id": None})}
        )
    result = transition(state, event, codec=codec)
    assert result.state.evaluation == state.evaluation
    assert result.requests == ()
    assert result.events[0].status == core.ObservationStatus.REJECTED


@given(
    root_released=st.booleans(),
    manifest=st.booleans(),
    child_present=st.booleans(),
    child_released=st.booleans(),
    child_manifest=st.booleans(),
)
def test_retry_waits_for_root_and_every_discovered_child_release(
    *,
    root_released: bool,
    manifest: bool,
    child_present: bool,
    child_released: bool,
    child_manifest: bool,
) -> None:
    state, request = submitted()
    child_id = core.ResourceId(root="child")
    terminal = observation(
        request,
        2,
        terminal=True,
        status=core.ObservationStatus.FAILED,
        released=root_released,
        children_complete=manifest,
        children=(child_id,),
    )
    state = transition(
        committed(state, terminal),
        core.MeasurementSubmissionObserved(
            observation=terminal, failure=core.MeasurementFailure.INFRASTRUCTURE
        ),
    ).state
    state = transition(
        state, core.JobObserved(resource_id=core.ResourceId(root="job"), observation=terminal)
    ).state
    child_observation = terminal.model_copy(
        update={
            "event_id": core.EventId(root="child-release"),
            "resource_id": child_id,
            "released": child_released,
            "children": (),
            "children_complete": child_manifest,
        }
    )
    assert request.request_id is not None
    child = core.ChildLease(
        resource_id=child_id,
        scope=request.scope,
        source_requests=(request.request_id,),
        parent_resources=(core.ResourceId(root="job"),),
        observation=child_observation,
        observation_watermarks=(
            core.ChildObservationWatermark(
                source_request=request.request_id, observation=child_observation
            ),
        ),
        watermark_history_complete=True,
    )
    state = state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={"children": (child,) if child_present else ()}
            )
        }
    )
    result = requested(
        state,
        identity="after-release",
        measurement=request.plan.model_copy(update={"submitted_at": 3.0, "deadline_at": 103.0}),
    )
    assert bool(result.requests) == (
        root_released and manifest and child_present and child_released and child_manifest
    )
    assert len(result.state.evaluation.submission_budgets[0].receipts) <= 3
