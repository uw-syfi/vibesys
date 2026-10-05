"""Measurement budgets and scientific ingress through the public kernel."""

import json
from hashlib import sha256

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


def plan(**updates: object) -> core.MeasurementPlan:
    data = {
        "purpose": "baseline",
        "candidate": core.initial_state().run.facts.baseline,
        "evaluator_digest": "evaluator",
        "workload_digest": "workload",
        "environment_digest": "environment",
        "stages": (
            core.MeasurementStage(stage_id="accuracy", execution_budget=20.0),
            core.MeasurementStage(
                stage_id="benchmark", depends_on=("accuracy",), execution_budget=70.0
            ),
        ),
        "policy": "ordered",
        "recipe": core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="recipe"),
        "submitted_at": 0.0,
        "queue_allowance": 10.0,
        "deadline_at": 100.0,
        "submission_limit": 3,
        "accuracy_stage": "accuracy",
    }
    merged = {**data, **updates}
    stages = updates.get("stages", data["stages"])
    assert isinstance(stages, tuple)
    if "accuracy_stage" not in updates and not any(
        stage.stage_id == "accuracy" for stage in stages
    ):
        merged["accuracy_stage"] = None
    return core.MeasurementPlan.model_validate(merged)


def roundtrip(
    state: core.CoreState, *, codec: core.OperationRegistry | None = None
) -> core.CoreState:
    envelope = core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=1),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )
    codec = codec or core.OperationRegistry()
    return codec.decode_envelope(
        core.RunEnvelope[core.StrategyState], codec.encode_envelope(envelope)
    ).core


def transition(
    state: core.CoreState,
    event: core.CoreEvent,
    *,
    codec: core.OperationRegistry | None = None,
    reducers: core.CoreReducers | None = None,
) -> core.Transition:
    before = state.model_dump_json()
    result = core.step(state, event, reducers=reducers)
    assert result == core.step(roundtrip(state, codec=codec), event, reducers=reducers)
    assert roundtrip(result.state, codec=codec) == result.state
    assert state.model_dump_json() == before
    assert result.state.scheduling == state.scheduling
    assert result.state.sessions == state.sessions
    assert result.state.settlement == state.settlement
    return result


def requested(
    state: core.CoreState | None = None,
    *,
    identity: str = "measure",
    measurement: core.MeasurementPlan | None = None,
) -> core.Transition:
    state = state or core.initial_state()
    measurement = measurement or plan()
    if measurement.purpose == "profile":
        lifecycle: set[core.LifecycleCapability] = set(state.run.capabilities.lifecycle)
        lifecycle.add("profile-capture")
        offered = frozenset(lifecycle)
        state = state.model_copy(
            update={
                "run": state.run.model_copy(
                    update={"capabilities": core.Capabilities(lifecycle=offered)}
                )
            }
        )
    decision = core.Measure(
        decision_id=core.DecisionId(root=identity),
        scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
        plan=measurement,
    )
    return transition(
        state, core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
    )


def observation(
    request: core.SubmitMeasurement, sequence: int = 1, **updates: object
) -> core.Observation:
    data = {
        "event_id": core.EventId(root=f"event-{sequence}"),
        "request_id": request.request_id,
        "scope": request.scope,
        "admission_id": request.admission_id,
        "sequence": sequence,
        "observed_at": float(sequence),
        "status": core.ObservationStatus.PENDING,
        "resource_id": core.ResourceId(root="job"),
        "accepted": True,
    }
    return core.Observation.model_validate({**data, **updates})


def committed(
    state: core.CoreState,
    observed: core.Observation,
    facts: core.EvaluationTerminalFacts | None = None,
) -> core.CoreState:
    # Sibling Intents A owns this atomic ingress, which is still a stub on main.
    # Supply its immutable public output, never replace running code.
    rows = tuple(
        row.model_copy(
            update={
                "observation": observed,
                "sequence": observed.sequence,
                "evaluation_result": facts,
            }
        )
        if row.request_id == observed.request_id
        else row
        for row in state.intents.intents
    )
    return state.model_copy(update={"intents": state.intents.model_copy(update={"intents": rows})})


def submitted(
    *, measurement: core.MeasurementPlan | None = None
) -> tuple[core.CoreState, core.SubmitMeasurement]:
    result = requested(measurement=measurement)
    request = result.requests[0]
    assert isinstance(request, core.SubmitMeasurement)
    observed = observation(request)
    state = committed(result.state, observed)
    accepted = transition(state, core.MeasurementSubmissionObserved(observation=observed))
    observed_job = transition(
        accepted.state,
        core.JobObserved(resource_id=core.ResourceId(root="job"), observation=observed),
    )
    return observed_job.state, request


def evidence(
    request: core.SubmitMeasurement,
    observed: core.Observation,
    *,
    identity: str,
    kind: core.EvidenceKind,
    status: core.ObservationStatus,
) -> core.EvidenceRef:
    assert isinstance(request.plan.candidate, core.RevisionRef)
    return core.EvidenceRef(
        evidence_id=core.EvidenceId(root=identity),
        kind=kind,
        purpose=request.plan.purpose,
        scope=request.scope,
        source_request=observed.request_id,
        candidate=request.plan.candidate,
        observation_sequence=observed.sequence,
        evaluator_digest=request.plan.evaluator_digest,
        workload_digest=request.plan.workload_digest,
        environment_digest=request.plan.environment_digest,
        provenance="trusted",
        status=status,
        artifacts=(
            core.ArtifactRef(
                artifact_id=core.ArtifactId(root=f"{identity}-artifact"),
                digest="original-provenance",
                schema_ref=core.SchemaRef(name="scientific-partials", version=1),
            ),
        ),
    )


def test_submission_receipt_and_request_are_allocated_atomically() -> None:
    result = requested()
    assert len(result.requests) == 1
    request = result.requests[0]
    assert isinstance(request, core.SubmitMeasurement)
    assert request.request_id is not None
    budget = result.state.evaluation.submission_budgets[0]
    assert budget.limit == 3
    assert budget.receipts == (
        core.PreparedSubmissionReceipt(request_id=request.request_id, ordinal=1),
    )
    assert result.state.intents.intents[0].request == request
    replay = requested(result.state)
    assert replay.requests == ()
    assert replay.state.evaluation == result.state.evaluation


@given(order=st.lists(st.integers(min_value=1, max_value=8), min_size=1, max_size=20))
def test_duplicate_reordered_observations_and_reload_never_regress(order: list[int]) -> None:
    state, request = submitted()
    latest = state.evaluation.jobs[0].observation
    assert latest is not None
    for sequence in order:
        observed = observation(request, sequence)
        state = committed(state, observed)
        result = transition(
            state, core.JobObserved(resource_id=core.ResourceId(root="job"), observation=observed)
        )
        # Only the direct successor of the held observation is one the executor can have
        # issued next; a gap, a replay and an older one change nothing.
        if sequence == latest.sequence + 1:
            latest = observed
        assert result.state.evaluation.jobs[0].observation == latest
        assert len(result.state.evaluation.submission_budgets[0].receipts) == 1
        state = roundtrip(result.state)


@given(
    failure=st.sampled_from(list(core.MeasurementFailure)),
    accepted=st.booleans(),
    terminal=st.booleans(),
    released=st.booleans(),
    status=st.sampled_from(list(core.ObservationStatus)),
)
def test_submission_retry_requires_conclusive_infrastructure_failure(
    *,
    failure: core.MeasurementFailure,
    accepted: bool,
    terminal: bool,
    released: bool,
    status: core.ObservationStatus,
) -> None:
    result = requested()
    request = result.requests[0]
    assert isinstance(request, core.SubmitMeasurement)
    observed = observation(
        request,
        accepted=accepted,
        terminal=terminal,
        released=released,
        status=status,
        resource_id=None,
    )
    state = committed(result.state, observed)
    result = transition(
        state, core.MeasurementSubmissionObserved(observation=observed, failure=failure)
    )
    retried = requested(
        result.state,
        identity="retry",
        measurement=plan(submitted_at=2.0, deadline_at=102.0, submission_limit=99),
    )
    allowed = (
        terminal
        and status not in (core.ObservationStatus.PENDING, core.ObservationStatus.UNKNOWN)
        and failure == core.MeasurementFailure.INFRASTRUCTURE
        and not accepted
    )
    assert bool(retried.requests) == allowed
    budget = retried.state.evaluation.submission_budgets[0]
    assert budget.limit == 3
    assert len(budget.receipts) == 1 + int(allowed)


@given(count=st.integers(min_value=4, max_value=10))
def test_preacceptance_failures_are_bounded_and_timing_never_resets_identity(count: int) -> None:
    state = core.initial_state()
    for index in range(count):
        result = requested(
            state,
            identity=f"measure-{index}",
            measurement=plan(submitted_at=float(index), deadline_at=100.0 + index),
        )
        if index >= 3:
            assert result.requests == ()
            assert len(result.state.evaluation.submission_budgets[0].receipts) == 3
            state = result.state
            continue
        request = result.requests[0]
        assert isinstance(request, core.SubmitMeasurement)
        observed = observation(
            request,
            index + 1,
            accepted=False,
            terminal=True,
            status=core.ObservationStatus.FAILED,
            resource_id=None,
        )
        state = committed(result.state, observed)
        result = transition(
            state,
            core.MeasurementSubmissionObserved(
                observation=observed, failure=core.MeasurementFailure.INFRASTRUCTURE
            ),
        )
        state = result.state
        budget = state.evaluation.submission_budgets[0]
        assert [r.ordinal for r in budget.receipts] == list(range(1, index + 2))


def test_r23_accuracy_and_exact_failed_partial_keep_original_artifacts() -> None:
    state, request = submitted()
    observed = observation(
        request, 2, terminal=True, released=True, status=core.ObservationStatus.SUCCEEDED
    )
    facts = core.EvaluationTerminalFacts(
        stages=(
            core.EvaluationStageResult(
                stage_id="accuracy", outcome=core.EvaluationStageOutcome.PASSED
            ),
            core.EvaluationStageResult(
                stage_id="benchmark", outcome=core.EvaluationStageOutcome.FAILED
            ),
        ),
        accuracy_passed=True,
        failed_benchmark=core.BenchmarkFailure(
            partial_rate=79.835, rate_lower=64.0, rate_upper=128.0
        ),
    )
    accuracy = evidence(
        request,
        observed,
        identity="accuracy",
        kind=core.EvidenceKind.CORRECTNESS,
        status=core.ObservationStatus.SUCCEEDED,
    )
    partial = evidence(
        request,
        observed,
        identity="79.835-at-71-of-72",
        kind=core.EvidenceKind.BENCHMARK,
        status=core.ObservationStatus.FAILED,
    )
    state = committed(state, observed, facts)
    result = transition(
        state,
        core.JobObserved(
            resource_id=core.ResourceId(root="job"),
            observation=observed,
            evidence=(accuracy, partial),
            evaluation_result=facts,
        ),
    )
    retained = core.project(result.state).measurements
    assert len(retained) == 2
    assert retained[1].artifacts == partial.artifacts
    assert retained[1].status == core.ObservationStatus.FAILED
    assert retained[1].acceptance_receipt is not None
    assert retained[1].source_request == request.request_id
    assert retained[1].observation_sequence == 2
    assert result.state.intents.intents[0].evaluation_result == facts
    late = observation(
        request, 3, terminal=True, released=True, status=core.ObservationStatus.SUCCEEDED
    )
    state = committed(result.state, late, facts)
    final = transition(
        state,
        core.JobObserved(
            resource_id=core.ResourceId(root="job"), observation=late, evaluation_result=facts
        ),
    )
    assert core.project(final.state).measurements == retained


@given(
    field=st.sampled_from(
        (
            "candidate",
            "workload_digest",
            "evaluator_digest",
            "environment_digest",
            "scope",
            "source_request",
            "observation_sequence",
        )
    )
)
def test_evidence_mismatch_never_enters_projection(field: str) -> None:
    state, request = submitted()
    observed = observation(request, 2, terminal=True, status=core.ObservationStatus.SUCCEEDED)
    valid = evidence(
        request,
        observed,
        identity="proof",
        kind=core.EvidenceKind.BENCHMARK,
        status=core.ObservationStatus.SUCCEEDED,
    )
    replacements = {
        "candidate": core.RevisionRef(revision_id=core.RevisionId(root="wrong"), digest="wrong"),
        "workload_digest": "wrong",
        "evaluator_digest": "wrong",
        "environment_digest": "wrong",
        "scope": core.Scope(owner=core.RunId(root="foreign"), generation=0),
        "source_request": core.RequestId(root="foreign"),
        "observation_sequence": 99,
    }
    incoming = valid.model_copy(update={field: replacements[field]})
    state = committed(state, observed)
    result = transition(
        state,
        core.JobObserved(
            resource_id=core.ResourceId(root="job"), observation=observed, evidence=(incoming,)
        ),
    )
    assert core.project(result.state).measurements == ()


@given(
    progress_present=st.booleans(),
    stage=st.sampled_from((None, "accuracy", "benchmark", "unknown")),
    queued=st.one_of(st.none(), st.floats(min_value=0, max_value=10, allow_nan=False)),
    ran=st.one_of(st.none(), st.floats(min_value=0, max_value=10, allow_nan=False)),
)
def test_progress_optional_fields_preserve_missing_values_and_stage_registry(
    *, progress_present: bool, stage: str | None, queued: float | None, ran: float | None
) -> None:
    state, request = submitted()
    observed = observation(request, 2)
    progress = (
        core.JobProgress(
            observation_sequence=2,
            observed_at=2.0,
            state="running",
            stage_id=stage,
            queued_s=queued,
            ran_s=ran,
        )
        if progress_present
        else None
    )
    state = committed(state, observed)
    result = transition(
        state,
        core.JobObserved(
            resource_id=core.ResourceId(root="job"), observation=observed, progress=progress
        ),
    )
    if progress_present and stage == "unknown":
        assert result.state.evaluation == state.evaluation
    else:
        assert result.state.evaluation.jobs[0].progress == progress


@given(
    sequence=st.integers(min_value=1, max_value=10),
    mismatch=st.sampled_from(
        (
            "unknown",
            "foreign-resource",
            "wrong-kind",
            "uncommitted",
            "never-issued",
            "conflicts-with-ledger",
        )
    ),
)
def test_unknown_foreign_wrong_kind_and_uncommitted_sources_are_inert(
    sequence: int, mismatch: str
) -> None:
    state, request = submitted()
    observed = observation(request, sequence)
    if mismatch == "unknown":
        observed = observed.model_copy(update={"request_id": core.RequestId(root="unknown")})
    elif mismatch == "foreign-resource":
        observed = observed.model_copy(update={"resource_id": core.ResourceId(root="foreign")})
        state = committed(state, observed)
    elif mismatch == "never-issued":
        # The job holds sequence 1, so sequence 2 is the next one the executor can issue.
        observed = observation(request, sequence + 2)
        state = committed(state, observed)
    elif mismatch == "uncommitted":
        # The ledger holds no observation of the submission, so nothing vouches for the job.
        rows = tuple(
            row.model_copy(update={"observation": None, "sequence": 0})
            for row in state.intents.intents
        )
        state = state.model_copy(
            update={"intents": state.intents.model_copy(update={"intents": rows})}
        )
    elif mismatch == "conflicts-with-ledger":
        held = state.intents.intents[0].observation
        assert held is not None
        observed = observation(request, held.sequence, status=core.ObservationStatus.SUCCEEDED)
    elif mismatch == "wrong-kind":
        source = state.intents.intents[0]
        query = core.ObserveOwnedJob(
            request_id=source.request_id,
            scope=request.scope,
            deadline_at=100.0,
            resource_id=core.ResourceId(root="job"),
        )
        codec_digest = sha256(
            json.dumps(
                query.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        source = source.model_copy(
            update={
                "request": query,
                "lifecycle": core.LifecycleClass.QUERY,
                "payload_digest": codec_digest,
                "observation": observed,
            }
        )
        state = state.model_copy(
            update={"intents": state.intents.model_copy(update={"intents": (source,)})}
        )
    before = state.evaluation
    result = transition(
        state, core.JobObserved(resource_id=core.ResourceId(root="job"), observation=observed)
    )
    assert result.state.evaluation == before
    assert result.requests == ()


def _tick_to_next_poll(state: core.CoreState, resource: core.ResourceId) -> core.CoreState:
    """Advance core time to the job's due time and expect exactly one poll of it."""
    due = core.project(state).next_observe_at
    assert due is not None
    early = core.step(state, core.ClockAdvanced(now_at=due - 0.001))
    assert not [r for r in early.requests if isinstance(r, core.ObserveOwnedJob)]
    tick = core.step(early.state, core.ClockAdvanced(now_at=due))
    polls = [r for r in tick.requests if isinstance(r, core.ObserveOwnedJob)]
    assert [p.resource_id for p in polls] == [resource]
    return tick.state


def _step_job_polls(statuses: list[core.ObservationStatus]) -> core.CoreState:
    """Submit through the real ledger, then deliver one executor poll per status."""
    result = requested()
    submit = result.requests[0]
    assert isinstance(submit, core.SubmitMeasurement)
    assert submit.request_id is not None
    state = core.step(result.state, core.DispatchAuthorized(request_id=submit.request_id)).state
    first = observation(submit, 1)
    state = core.step(state, core.RequestObserved(observation=first)).state
    resource = core.ResourceId(root="job")
    # The executor delivers the submission's own view as the job's first observation.
    after = core.step(state, core.JobObserved(resource_id=resource, observation=first))
    # An observation never answers itself with a poll: the clock does, when one is due.
    assert after.requests == ()
    state = _tick_to_next_poll(after.state, resource)
    for sequence, status in enumerate(statuses, start=2):
        terminal = status is not core.ObservationStatus.PENDING
        # Job observations carry the submission's request id and never reach the ledger.
        polled = observation(submit, sequence, status=status, terminal=terminal)
        step = core.step(state, core.JobObserved(resource_id=resource, observation=polled))
        state = step.state
        assert state.evaluation.jobs[0].observation == polled
        assert not [r for r in step.requests if isinstance(r, core.ObserveOwnedJob)]
        # Each poll of a live job comes due once, and an ended job is never polled again.
        assert core.project(state).next_observe_at == (
            None if terminal else state.evaluation.jobs[0].pacing.next_at
        )
        if not terminal:
            state = _tick_to_next_poll(state, resource)
        stale = core.step(
            state, core.JobObserved(resource_id=resource, observation=observation(submit, 1))
        )
        assert stale.state.evaluation == state.evaluation
    return state


@given(polls=st.integers(min_value=1, max_value=5))
def test_a_submitted_job_is_polled_until_it_ends(polls: int) -> None:
    pending = [core.ObservationStatus.PENDING] * (polls - 1)
    state = _step_job_polls([*pending, core.ObservationStatus.SUCCEEDED])
    assert state.evaluation.jobs[0].terminal
