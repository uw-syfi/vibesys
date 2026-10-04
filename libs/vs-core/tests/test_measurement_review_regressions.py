"""Review regressions for the measurements leaf, driven through the public kernel."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_measurements import (
    committed,
    evidence,
    observation,
    plan,
    requested,
    submitted,
    transition,
)

JOB = core.ResourceId(root="job")
S = core.ObservationStatus
CONCLUSIVE = (S.SUCCEEDED, S.FAILED, S.CANCELLED, S.REJECTED)


def observe_job(
    state: core.CoreState,
    observed: core.Observation,
    *,
    facts: core.EvaluationTerminalFacts | None = None,
    evidence: tuple[core.EvidenceRef, ...] = (),
) -> core.Transition:
    return transition(
        committed(state, observed, facts),
        core.JobObserved(
            resource_id=observed.resource_id or JOB,
            observation=observed,
            evidence=evidence,
            evaluation_result=facts,
        ),
    )


def results(result: core.Transition) -> list[core.MeasurementResult]:
    return [e for e in result.events if isinstance(e, core.MeasurementResult)]


# F1: inert observations emit nothing.


@given(
    sequence=st.integers(min_value=1, max_value=6),
    fault=st.sampled_from(("unknown-request", "uncommitted", "foreign-resource", "early")),
)
def test_inert_job_observations_emit_no_events(sequence: int, fault: str) -> None:
    if fault == "early":
        fresh = requested()
        request = fresh.requests[0]
        assert isinstance(request, core.SubmitMeasurement)
        state = fresh.state
    else:
        state, request = submitted()
    observed = observation(request, sequence + 1)
    if fault == "unknown-request":
        observed = observed.model_copy(update={"request_id": core.RequestId(root="unknown")})
    elif fault == "foreign-resource":
        observed = observed.model_copy(update={"resource_id": core.ResourceId(root="foreign")})
    if fault != "uncommitted":
        state = committed(state, observed)
    result = transition(state, core.JobObserved(resource_id=JOB, observation=observed))
    assert result.events == ()
    assert result.requests == ()
    assert result.state.evaluation == state.evaluation


# F2: retry authority comes from committed facts, never from the failure claim.


@given(
    claim=st.sampled_from([None, *core.MeasurementFailure]),
    status=st.sampled_from(list(S)),
    terminal=st.booleans(),
)
def test_failure_claim_cannot_grant_retry_after_accepted_execution(
    *, claim: core.MeasurementFailure | None, status: core.ObservationStatus, terminal: bool
) -> None:
    state, request = submitted()
    fields = {"terminal": terminal, "released": True, "children_complete": True, "status": status}
    state = observe_job(state, observation(request, 2, **fields)).state
    classified = observation(request, 3, **fields)
    state = transition(
        committed(state, classified),
        core.MeasurementSubmissionObserved(observation=classified, failure=claim),
    ).state
    retried = requested(
        state, identity="again", measurement=plan(submitted_at=5.0, deadline_at=105.0)
    )
    conclusive = terminal and status in CONCLUSIVE
    assert bool(retried.requests) == (
        conclusive and status != S.SUCCEEDED and claim == core.MeasurementFailure.INFRASTRUCTURE
    )


def test_success_then_infrastructure_claim_never_charges_a_retry() -> None:
    state, request = submitted()
    fields = {"terminal": True, "released": True, "children_complete": True, "status": S.SUCCEEDED}
    state = observe_job(state, observation(request, 2, **fields)).state
    late = observation(request, 3, **fields)
    state = transition(
        committed(state, late),
        core.MeasurementSubmissionObserved(
            observation=late, failure=core.MeasurementFailure.INFRASTRUCTURE
        ),
    ).state
    again = requested(state, identity="again")
    assert again.requests == ()
    assert len(again.state.evaluation.submission_budgets[0].receipts) == 1


def test_workload_claim_on_running_job_does_not_freeze_the_receipt() -> None:
    state, request = submitted()
    running = observation(request, 2)
    state = transition(
        committed(state, running),
        core.MeasurementSubmissionObserved(
            observation=running, failure=core.MeasurementFailure.WORKLOAD
        ),
    ).state
    fields = {"terminal": True, "released": True, "children_complete": True, "status": S.FAILED}
    state = observe_job(state, observation(request, 3, **fields)).state
    failed = observation(request, 4, **fields)
    state = transition(
        committed(state, failed),
        core.MeasurementSubmissionObserved(
            observation=failed, failure=core.MeasurementFailure.INFRASTRUCTURE
        ),
    ).state
    assert requested(state, identity="again").requests != ()


# F3: late evidence is published exactly once; UNKNOWN terminal never suppresses.


@given(
    split=st.lists(st.integers(min_value=0, max_value=2), min_size=1, max_size=4),
    replay=st.booleans(),
)
def test_every_evidence_item_is_published_exactly_once(*, split: list[int], replay: bool) -> None:
    state, request = submitted()
    published: list[core.EvidenceRef] = []
    serial = 0
    for index, count in enumerate(split):
        observed = observation(request, 2 + index, terminal=True, released=True, status=S.SUCCEEDED)
        batch = tuple(
            evidence(
                request,
                observed,
                identity=f"ev-{serial + n}",
                kind=core.EvidenceKind.BENCHMARK,
                status=S.FAILED,
            )
            for n in range(count)
        )
        serial += count
        result = observe_job(state, observed, evidence=batch)
        state = result.state
        emitted = results(result)
        assert len(emitted) == (1 if index == 0 or count else 0)
        for event in emitted:
            published.extend(event.evidence)
        if replay:
            again = observe_job(state, observed, evidence=batch)
            assert again.events == ()
    ledger = state.evaluation.evidence
    assert [e.evidence_id for e in published] == [e.evidence_id for e in ledger]


def test_unknown_terminal_never_suppresses_the_conclusive_result() -> None:
    state, request = submitted()
    unknown = observation(request, 2, terminal=True, status=S.UNKNOWN)
    state = observe_job(state, unknown).state
    final = observation(request, 3, terminal=True, released=True, status=S.SUCCEEDED)
    emitted = results(observe_job(state, final))
    assert [e.status for e in emitted] == [S.SUCCEEDED]


# F4 and F5: success requires every required stage, accuracy, and agreeing status.

ACCURACY = "accuracy"
BENCHMARK = "benchmark"


def facts_for(
    accuracy: core.EvaluationStageOutcome | None,
    benchmark: core.EvaluationStageOutcome | None,
    *,
    accuracy_passed: bool,
) -> core.EvaluationTerminalFacts:
    stages = tuple(
        core.EvaluationStageResult(stage_id=stage_id, outcome=outcome)
        for stage_id, outcome in ((ACCURACY, accuracy), (BENCHMARK, benchmark))
        if outcome is not None
    )
    return core.EvaluationTerminalFacts(stages=stages, accuracy_passed=accuracy_passed)


def optional_outcome() -> st.SearchStrategy[core.EvaluationStageOutcome | None]:
    return st.sampled_from([None, *core.EvaluationStageOutcome])


@given(
    kind=st.sampled_from((core.EvidenceKind.BENCHMARK, core.EvidenceKind.CORRECTNESS)),
    accuracy=optional_outcome(),
    benchmark=optional_outcome(),
    accuracy_passed=st.booleans(),
    marked=st.booleans(),
)
def test_successful_evidence_requires_accuracy_and_required_stages(
    kind: core.EvidenceKind,
    accuracy: core.EvaluationStageOutcome | None,
    benchmark: core.EvaluationStageOutcome | None,
    *,
    accuracy_passed: bool,
    marked: bool,
) -> None:
    state, request = submitted(measurement=plan(accuracy_stage=ACCURACY if marked else None))
    observed = observation(request, 2, terminal=True, released=True, status=S.SUCCEEDED)
    claim = evidence(request, observed, identity="proof", kind=kind, status=S.SUCCEEDED)
    facts = facts_for(accuracy, benchmark, accuracy_passed=accuracy_passed)
    result = observe_job(state, observed, facts=facts, evidence=(claim,))
    passed = core.EvaluationStageOutcome.PASSED
    required = (
        accuracy == passed
        if kind == core.EvidenceKind.CORRECTNESS and marked
        else accuracy == passed and benchmark == passed
    )
    retained = core.project(result.state).measurements
    assert bool(retained) == (accuracy_passed and required)


def test_accuracy_gate_is_the_declared_stage_not_a_dependency_inference() -> None:
    """Independent stages: only the marked one gates correctness evidence."""
    independent = (
        core.MeasurementStage(stage_id=ACCURACY, execution_budget=20.0),
        core.MeasurementStage(stage_id=BENCHMARK, execution_budget=70.0),
    )
    passed, failed = core.EvaluationStageOutcome.PASSED, core.EvaluationStageOutcome.FAILED
    for marker, retained in ((BENCHMARK, False), (ACCURACY, True)):
        state, request = submitted(measurement=plan(stages=independent, accuracy_stage=marker))
        observed = observation(request, 2, terminal=True, released=True, status=S.SUCCEEDED)
        claim = evidence(
            request, observed, identity="c", kind=core.EvidenceKind.CORRECTNESS, status=S.SUCCEEDED
        )
        facts = facts_for(passed, failed, accuracy_passed=True)
        result = observe_job(state, observed, facts=facts, evidence=(claim,))
        assert bool(core.project(result.state).measurements) == retained


def test_accuracy_stage_must_name_a_plan_stage_and_is_part_of_the_identity() -> None:
    with pytest.raises(ValueError, match="accuracy_stage"):
        plan(accuracy_stage="missing")
    revision = core.initial_state().run.facts.baseline
    marked = core.MeasurementIdentity.from_plan(plan(), revision)
    unmarked = core.MeasurementIdentity.from_plan(plan(accuracy_stage=None), revision)
    assert marked.accuracy_stage == ACCURACY
    assert marked != unmarked
    for identity in (marked, unmarked):
        assert core.MeasurementIdentity.model_validate_json(identity.model_dump_json()) == identity
    for value in (plan(), plan(accuracy_stage=None)):
        assert core.MeasurementPlan.model_validate_json(value.model_dump_json()) == value


@given(
    observed_status=st.sampled_from([s for s in CONCLUSIVE if s != S.SUCCEEDED]),
    kind=st.sampled_from((core.EvidenceKind.BENCHMARK, core.EvidenceKind.CORRECTNESS)),
)
def test_successful_evidence_cannot_ride_a_failed_execution(
    observed_status: core.ObservationStatus, kind: core.EvidenceKind
) -> None:
    state, request = submitted()
    observed = observation(request, 2, terminal=True, released=True, status=observed_status)
    claim = evidence(request, observed, identity="proof", kind=kind, status=S.SUCCEEDED)
    facts = facts_for(
        core.EvaluationStageOutcome.PASSED,
        core.EvaluationStageOutcome.PASSED,
        accuracy_passed=True,
    )
    result = observe_job(state, observed, facts=facts, evidence=(claim,))
    assert core.project(result.state).measurements == ()
    assert [e.evidence for e in results(result)] == [()]


# F6: a resource id owned by one submission is never adopted by another.


@given(sequence=st.integers(min_value=2, max_value=5))
def test_colliding_resource_ids_cannot_orphan_owned_jobs(sequence: int) -> None:
    state, first = submitted()
    other = requested(state, identity="other", measurement=plan(workload_digest="other-workload"))
    second = other.requests[0]
    assert isinstance(second, core.SubmitMeasurement)
    observed = observation(second, 1)
    state = committed(other.state, observed)
    state = transition(state, core.MeasurementSubmissionObserved(observation=observed)).state
    assert [j.submission_id for j in state.evaluation.jobs] == [first.request_id]
    receipt = state.evaluation.submission_budgets[1].receipts[0]
    assert isinstance(receipt, core.PreparedSubmissionReceipt)
    assert receipt.observation is None
    later = observation(first, sequence)
    state = observe_job(state, later).state
    assert state.evaluation.jobs[0].observation == later
    cancel = transition(state, core.JobTerminationRequested(resource_id=JOB, cause="retirement"))
    assert [type(r) for r in cancel.requests] == [core.CancelOwnedJob]
    stray = observe_job(state, observation(second, sequence + 1))
    assert stray.events == ()
    assert stray.state.evaluation == state.evaluation


# F7: an evidence id held by one source can never be taken by another.


@given(
    order=st.permutations((0, 1)),
    colliding=st.booleans(),
)
def test_evidence_ids_are_never_shared_across_jobs(*, order: list[int], colliding: bool) -> None:
    state, first = submitted()
    other = requested(state, identity="other", measurement=plan(workload_digest="other-workload"))
    second = other.requests[0]
    assert isinstance(second, core.SubmitMeasurement)
    job2 = core.ResourceId(root="job2")
    opened = observation(second, 1, resource_id=job2)
    state = transition(
        committed(other.state, opened), core.MeasurementSubmissionObserved(observation=opened)
    ).state
    requests = (first, second)
    emitted: dict[int, list[core.MeasurementResult]] = {}
    for index in order:
        request = requests[index]
        observed = observation(
            request,
            2,
            terminal=True,
            released=True,
            status=S.SUCCEEDED,
            resource_id=JOB if index == 0 else job2,
        )
        name = "accuracy" if colliding else f"accuracy-{index}"
        claim = evidence(
            request, observed, identity=name, kind=core.EvidenceKind.BENCHMARK, status=S.FAILED
        )
        result = observe_job(state, observed, evidence=(claim,))
        state = result.state
        emitted[index] = results(result)
    ids = [e.evidence_id for e in core.project(state).measurements]
    assert len(ids) == len(set(ids))
    winner, loser = order
    assert len(emitted[winner][0].evidence) == 1
    assert emitted[winner][0].failure is None
    if colliding:
        assert emitted[loser][0].evidence == ()
        assert emitted[loser][0].failure == core.MeasurementFailure.UNKNOWN
        assert len(ids) == 1
    else:
        assert len(emitted[loser][0].evidence) == 1
        assert len(ids) == 2
