"""An official measurement is answered by the scope's runs of the same input (#2949, #3047).

An agent's own evaluation runs the task's evaluation on its candidate. A framework run is
trusted whoever asked for it, so every conclusive run of one input is a sample of one
measurement. When the strategy then asks for the official measurement of the same input, core
answers it from those samples instead of a new run: the submission names the run that carries
the answer, and the records the runtime reports for it must be that run's own. These tests
drive the kernel through its public events only.
"""

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_measurements import committed, plan, requested, transition

type Outcome = Literal["passed", "failed", "machinery", "ambiguous", "unknown"]
type Variant = Literal[
    "same", "environment", "evaluator", "workload", "stages", "budget", "queue", "shared-host"
]
_UNLIKE: tuple[Variant, ...] = (
    "environment",
    "evaluator",
    "workload",
    "stages",
    "budget",
    "queue",
)
"""How a run may differ from the official one and still be another measurement.

A shared host is not among them: the host is recorded, and it widens the noise, but a run
timed beside other work is as trusted as any framework run."""
_CONCLUSIVE: tuple[Outcome, ...] = ("passed", "failed")
"""Outcomes a reuse may stand on: the candidate's own pass or failure."""

_PASSED = core.EvaluationTerminalFacts(
    stages=(
        core.EvaluationStageResult(stage_id="accuracy", outcome=core.EvaluationStageOutcome.PASSED),
        core.EvaluationStageResult(
            stage_id="benchmark", outcome=core.EvaluationStageOutcome.PASSED
        ),
    ),
    accuracy_passed=True,
)
_FAILED = core.EvaluationTerminalFacts(
    stages=(
        core.EvaluationStageResult(stage_id="accuracy", outcome=core.EvaluationStageOutcome.FAILED),
        core.EvaluationStageResult(
            stage_id="benchmark", outcome=core.EvaluationStageOutcome.UNKNOWN
        ),
    ),
    accuracy_passed=False,
)


def revision(name: str) -> core.RevisionRef:
    return core.RevisionRef.of_git_commit(name)


def scope_of(state: core.CoreState) -> core.Scope:
    return core.Scope(owner=state.run.run_id, generation=state.run.generation)


_BENCHMARK = core.MeasurementStage(
    stage_id="benchmark", depends_on=("accuracy",), execution_budget=70.0
)
_ACCURACY = core.MeasurementStage(stage_id="accuracy", execution_budget=20.0)
_VARIANTS: dict[Variant, dict[str, object]] = {
    "same": {},
    "environment": {"environment_digest": "another-environment"},
    "evaluator": {"evaluator_digest": "another-evaluator"},
    "workload": {"workload_digest": "another-workload"},
    "stages": {"stages": (_ACCURACY,)},
    "budget": {"stages": (_ACCURACY, _BENCHMARK.model_copy(update={"execution_budget": 69.0}))},
    "queue": {"queue_allowance": 9.0},
    "shared-host": {"dedicated_measurement_resources": False},
}
"""How a run's plan may differ from the official one; only the unlike ones are not reused."""


def measured(
    purpose: Literal["local-validation", "official"],
    candidate: str,
    measured_input: str | None,
    variant: Variant = "same",
) -> core.MeasurementPlan:
    """A plan of ``candidate`` on an executor that holds its hosts alone, unless ``variant``."""
    return plan(
        purpose=purpose,
        candidate=revision(candidate),
        measured_input=measured_input,
        **({"dedicated_measurement_resources": True} | _VARIANTS[variant]),
    )


def reloaded(state: core.CoreState) -> core.CoreState:
    """The state as a restarted host reads it back from its stored envelope."""
    envelope = core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=1),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )
    codec = core.OperationRegistry()
    return codec.decode_envelope(
        core.RunEnvelope[core.StrategyState], codec.encode_envelope(envelope)
    ).core


def submissions(state: core.CoreState) -> list[core.SubmitMeasurement]:
    return [
        row.request
        for row in state.records.records
        if isinstance(row.request, core.SubmitMeasurement)
    ]


def seen(request: core.SubmitMeasurement, sequence: int, **updates: object) -> core.Observation:
    assert request.request_id is not None
    data = {
        "event_id": core.EventId(root=f"{request.request_id.root}-{sequence}"),
        "request_id": request.request_id,
        "scope": request.scope,
        "admission_id": request.admission_id,
        "sequence": sequence,
        "observed_at": float(sequence),
        "status": core.ObservationStatus.PENDING,
        "resource_id": core.ResourceId(root=f"job-{request.request_id.root}"),
        "accepted": True,
    }
    return core.Observation.model_validate({**data, **updates})


def record(
    request: core.SubmitMeasurement,
    observed: core.Observation,
    stage: str,
    status: core.ObservationStatus,
    *,
    reused: core.RequestId | None = None,
) -> core.EvidenceRef:
    """One stage's record as the runtime reports it for ``request``, reusing run ``reused``."""
    assert isinstance(request.plan.candidate, core.RevisionRef)
    kind = core.EvidenceKind.CORRECTNESS if stage == "accuracy" else core.EvidenceKind.BENCHMARK
    source = request.request_id if reused is None else reused
    assert source is not None
    made = core.EvidenceRef(
        evidence_id=core.EvidenceId(root=f"{source.root}-{stage}"),
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
                artifact_id=core.ArtifactId(root=f"{source.root}-{stage}.json"),
                digest="sha256:result",
            ),
        ),
    )
    # Named only on a reuse, so this helper also builds records on code without reuse.
    return made if reused is None else made.model_copy(update={"reused_from": reused})


def ended(
    state: core.CoreState,
    request: core.SubmitMeasurement,
    outcome: Outcome,
    *,
    reused: core.RequestId | None = None,
) -> core.Transition:
    """Accept ``request``'s job and end it as the runtime reports ``outcome``."""
    started = seen(request, 1)
    state = committed(state, started)
    state = transition(state, core.MeasurementSubmissionObserved(observation=started)).state
    resource = started.resource_id
    assert resource is not None
    state = transition(state, core.JobObserved(resource_id=resource, observation=started)).state
    passed = outcome == "passed"
    status = core.ObservationStatus.SUCCEEDED if passed else core.ObservationStatus.FAILED
    finished = seen(request, 2, status=status, terminal=True, released=True)
    facts = _PASSED if passed else _FAILED
    evidence = tuple(
        record(request, finished, stage, status, reused=reused)
        for stage in ("accuracy", "benchmark")
    )
    state = committed(state, finished, facts)
    failure = {
        "passed": None,
        "failed": core.MeasurementFailure.WORKLOAD,
        "machinery": core.MeasurementFailure.INFRASTRUCTURE,
        "ambiguous": core.MeasurementFailure.AMBIGUOUS,
        "unknown": core.MeasurementFailure.UNKNOWN,
    }[outcome]
    if failure is not None:
        state = transition(
            state, core.MeasurementSubmissionObserved(observation=finished, failure=failure)
        ).state
    return transition(
        state,
        core.JobObserved(
            resource_id=resource,
            observation=finished,
            # An unclassified or ambiguous end may still report records; only its kind differs.
            evidence=evidence if outcome != "machinery" else (),
            evaluation_result=facts if outcome != "machinery" else None,
            failure=failure,
        ),
    )


def agent_run(
    state: core.CoreState, call_id: str, measurement: core.MeasurementPlan, outcome: Outcome
) -> tuple[core.CoreState, core.SubmitMeasurement | None]:
    """An agent's evaluation call and, when it allocated a submission, that run's end."""
    before = len(submissions(state))
    called = transition(
        state,
        core.AgentMeasurementRequested(
            scope=scope_of(state),
            plan=measurement,
            within=core.Within(limit=100.0),
            call_id=call_id,
        ),
    )
    if len(submissions(called.state)) == before:
        return called.state, None
    request = submissions(called.state)[-1]
    return ended(called.state, request, outcome).state, request


def official(state: core.CoreState, measurement: core.MeasurementPlan) -> core.Transition:
    return requested(state, identity="official", measurement=measurement)


def results(change: core.Transition) -> list[core.MeasurementResult]:
    return [event for event in change.events if isinstance(event, core.MeasurementResult)]


def test_an_official_measurement_of_the_agents_evaluated_commit_submits_no_second_job() -> None:
    """The agent measured the very commit the strategy then measures officially.

    Written against the API that predates reuse, so it fails on the merge base by
    observing the second, fresh submission rather than by a missing field.
    """
    state, agent = agent_run(
        core.initial_state(),
        "c1",
        measured("local-validation", "0027142", None),
        "passed",
    )
    assert agent is not None
    asked = official(state, measured("official", "0027142", None))
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    assert agent.request_id is not None
    reused = submission.model_dump(mode="json").get("reuses")
    assert reused == agent.request_id.model_dump(mode="json"), "a second measurement ran"


def test_an_official_measurement_of_the_agents_evaluated_input_runs_no_second_job() -> None:
    """The reported case: a notes-only commit after the agent's evaluation (0027142 -> 509dd57).

    On main the Measure submitted a second job; it now reuses the agent's run, and the
    strategy's result carries the agent run's records under the official purpose.
    """
    state, agent = agent_run(
        core.initial_state(),
        "c1",
        measured("local-validation", "0027142", "sha256:input"),
        "passed",
    )
    assert agent is not None
    asked = official(state, measured("official", "509dd57", "sha256:input"))
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    assert submission.reuses == agent.request_id
    done = ended(asked.state, submission, "passed", reused=submission.reuses)
    (result,) = results(done)
    assert result.status is core.ObservationStatus.SUCCEEDED
    assert [item.reused_from for item in result.evidence] == [agent.request_id] * 2
    assert {item.kind for item in result.evidence} == {
        core.EvidenceKind.CORRECTNESS,
        core.EvidenceKind.BENCHMARK,
    }
    assert core.trusted_keys(
        result, scope=submission.scope, candidate=revision("509dd57"), purpose="official"
    ) == tuple(item.key for item in result.evidence)


def test_a_reusing_submission_takes_only_its_runs_own_records() -> None:
    state, agent = agent_run(
        core.initial_state(), "c1", measured("local-validation", "a", "sha256:input"), "passed"
    )
    asked = official(state, measured("official", "b", "sha256:input"))
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    assert agent is not None
    assert submission.reuses == agent.request_id
    started = seen(submission, 1)
    after = committed(asked.state, started)
    after = transition(after, core.MeasurementSubmissionObserved(observation=started)).state
    finished = seen(
        submission, 2, status=core.ObservationStatus.SUCCEEDED, terminal=True, released=True
    )
    forged = (
        record(
            submission,
            finished,
            "benchmark",
            core.ObservationStatus.SUCCEEDED,
            reused=submission.reuses,
        ).model_copy(update={"evidence_id": core.EvidenceId(root="never-measured")}),
        record(submission, finished, "accuracy", core.ObservationStatus.SUCCEEDED),
    )
    genuine = record(
        submission,
        finished,
        "benchmark",
        core.ObservationStatus.SUCCEEDED,
        reused=submission.reuses,
    )
    assert started.resource_id is not None
    done = transition(
        committed(after, finished, _PASSED),
        core.JobObserved(
            resource_id=started.resource_id,
            observation=finished,
            evidence=(*forged, genuine),
            evaluation_result=_PASSED,
        ),
    )
    keys = {item.key for item in core.project(done.state).measurements}
    assert not keys & {item.key for item in forged}
    assert genuine.key in keys


@given(
    runs=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=2),
            st.integers(min_value=0, max_value=1),
            st.sampled_from(("passed", "failed", "machinery", "ambiguous", "unknown")),
            st.sampled_from(tuple(_VARIANTS)),
        ),
        max_size=4,
    ),
    target=st.tuples(st.integers(min_value=0, max_value=2), st.integers(min_value=0, max_value=2)),
    input_known=st.booleans(),
    exclusive=st.booleans(),
    reload_at=st.integers(min_value=0, max_value=4),
)
def test_an_official_measurement_is_answered_by_the_scopes_conclusive_runs_of_its_input(
    *,
    runs: list[tuple[int, int, Outcome, Variant]],
    target: tuple[int, int],
    input_known: bool,
    exclusive: bool,
    reload_at: int,
) -> None:
    """Any sequence of agent runs of inputs (two commits each), with a reload anywhere.

    Each run may differ from the official measurement in its environment, evaluator or
    workload digest, its stage set, a stage's budget or its queue allowance, or run on a shared
    host, and may end passed, failed, or in a machinery, ambiguous or unknown failure. The
    official measurement of input ``target[0]`` (at commit ``target[1]``, on any host) is
    answered by the scope's conclusive runs of that input that are alike in every respect but
    the purpose: by a failed one if any failed, else by the first. It never refuses because
    there are several, other inputs' runs never matter, and a commit the agent never ran
    matches by measured input only when it is known.
    """
    state = core.initial_state()
    jobs: list[tuple[int, int, Outcome, Variant, core.RequestId]] = []
    for number, (source_input, commit, outcome, variant) in enumerate(runs):
        if number == reload_at:
            state = reloaded(state)
        name = f"i{source_input}c{commit}"
        state, request = agent_run(
            state,
            f"call-{number}",
            measured("local-validation", name, f"sha256:{source_input}", variant),
            outcome,
        )
        if request is not None:
            assert request.request_id is not None
            jobs.append((source_input, commit, outcome, variant, request.request_id))
    if reload_at >= len(runs):
        state = reloaded(state)
    wanted, commit = target
    measured_input = f"sha256:{wanted}" if input_known else None
    asked_plan = measured(
        "official", f"i{wanted}c{commit}", measured_input, "same" if exclusive else "shared-host"
    )
    asked = official(state, asked_plan)
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]

    matching = [
        job
        for job in jobs
        if job[0] == wanted
        and (input_known or job[1] == commit)
        and job[2] in _CONCLUSIVE
        and job[3] not in _UNLIKE
    ]
    failed = [job for job in matching if job[2] == "failed"]
    chosen = (failed or matching or [None])[0]
    assert submission.reuses == (None if chosen is None else chosen[4])
    again = transition(
        asked.state,
        core.DecisionSubmitted(
            decision=core.Wait(
                request=core.Measure(
                    decision_id=core.DecisionId(root="official"),
                    scope=scope_of(state),
                    plan=asked_plan,
                ),
                until=core.Within(limit=100.0),
            ),
            expected_revision=asked.state.revision,
        ),
    )
    assert submissions(again.state) == submissions(asked.state), "a replay allocates nothing"
    assert reloaded(asked.state) == asked.state


def test_several_runs_of_other_inputs_never_stop_the_official_measurement_reusing_its_own() -> None:
    """Two inputs measured: nothing is refused for there being several, and nothing is picked."""
    state, first = agent_run(
        core.initial_state(), "c1", measured("local-validation", "a", "sha256:a"), "passed"
    )
    state, other = agent_run(state, "c2", measured("local-validation", "b", "sha256:b"), "passed")
    assert first is not None
    assert other is not None
    asked = official(state, measured("official", "a", "sha256:a"))
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    assert submission.reuses == first.request_id
    asked = official(state, measured("official", "b", "sha256:b"))
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    assert submission.reuses == other.request_id


def test_a_run_not_yet_ended_is_no_sample_and_does_not_hold_the_official_measurement_back() -> None:
    """Only a conclusive run answers; one still running is not one of the samples yet."""
    state, first = agent_run(
        core.initial_state(), "c1", measured("local-validation", "a", "sha256:a"), "passed"
    )
    pending = transition(
        state,
        core.AgentMeasurementRequested(
            scope=scope_of(state),
            plan=measured("local-validation", "b", "sha256:a"),
            within=core.Within(limit=100.0),
            call_id="c2",
        ),
    ).state
    assert len(submissions(pending)) == 2
    asked = official(pending, measured("official", "a2", "sha256:a"))
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    assert first is not None
    assert submission.reuses == first.request_id


def test_a_failed_run_is_the_one_that_answers_when_any_run_of_the_input_failed() -> None:
    """A candidate that failed once did not pass: the passing run never hides it."""
    state, _ = agent_run(
        core.initial_state(), "c1", measured("local-validation", "a", "sha256:a"), "passed"
    )
    state, failing = agent_run(state, "c2", measured("local-validation", "b", "sha256:a"), "failed")
    state, _ = agent_run(state, "c3", measured("local-validation", "c", "sha256:a"), "passed")
    assert failing is not None
    asked = official(state, measured("official", "d", "sha256:a"))
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    assert submission.reuses == failing.request_id


@pytest.mark.parametrize("variant", _UNLIKE)
def test_a_run_unlike_the_official_one_in_any_respect_is_not_reused(variant: Variant) -> None:
    """Budgets and the queue allowance are outside the identity, yet still another run."""
    state, agent = agent_run(
        core.initial_state(),
        "c1",
        measured("local-validation", "a", "sha256:a", variant),
        "passed",
    )
    assert agent is not None
    asked = official(state, measured("official", "a", "sha256:a"))
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    assert submission.reuses is None


@pytest.mark.parametrize("outcome", ["passed", "failed", "machinery", "ambiguous", "unknown"])
def test_only_a_run_the_candidate_itself_passed_or_failed_is_reused(outcome: Outcome) -> None:
    """A machinery, ambiguous or unclassified end says nothing about the candidate."""
    state, agent = agent_run(
        core.initial_state(), "c1", measured("local-validation", "a", "sha256:a"), outcome
    )
    assert agent is not None
    asked = official(state, measured("official", "a", "sha256:a"))
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    assert submission.reuses == (agent.request_id if outcome in _CONCLUSIVE else None)


def _reusing_job(
    source_purpose: Literal["local-validation", "official"],
) -> tuple[core.CoreState, core.SubmitMeasurement, core.Observation, core.Observation]:
    """A submission reusing the scope's one earlier run (of ``source_purpose``), up to its end."""
    state = core.initial_state()
    if source_purpose == "local-validation":
        state, source = agent_run(
            state, "c1", measured("local-validation", "a", "sha256:a"), "passed"
        )
    else:
        asked = official(state, measured("official", "a", "sha256:a"))
        (source,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
        state = ended(asked.state, source, "passed").state
    assert source is not None
    asked = requested(state, identity="second", measurement=measured("official", "b", "sha256:a"))
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    assert submission.reuses == source.request_id
    started = seen(submission, 1)
    after = committed(asked.state, started)
    after = transition(after, core.MeasurementSubmissionObserved(observation=started)).state
    finished = seen(
        submission,
        2,
        status=core.ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
        children_complete=True,
    )
    return after, submission, started, finished


def _report(
    state: core.CoreState,
    started: core.Observation,
    finished: core.Observation,
    evidence: tuple[core.EvidenceRef, ...],
) -> core.Transition:
    assert started.resource_id is not None
    return transition(
        committed(state, finished, _PASSED),
        core.JobObserved(
            resource_id=started.resource_id,
            observation=finished,
            evidence=evidence,
            evaluation_result=_PASSED,
        ),
    )


_STAGES = ("accuracy", "benchmark")
_KINDS = tuple(core.EvidenceKind)


_HONEST = {"accuracy": core.EvidenceKind.CORRECTNESS, "benchmark": core.EvidenceKind.BENCHMARK}


@pytest.mark.parametrize("source_purpose", ["local-validation", "official"])
@given(kinds=st.tuples(st.sampled_from(_KINDS), st.sampled_from(_KINDS)))
def test_a_reused_record_takes_exactly_the_kind_its_source_record_maps_to(
    source_purpose: Literal["local-validation", "official"],
    kinds: tuple[core.EvidenceKind, core.EvidenceKind],
) -> None:
    """Each reused record keeps its own stage's kind: no kind is invented, none is swapped."""
    state, submission, started, finished = _reusing_job(source_purpose)
    evidence = tuple(
        record(
            submission, finished, stage, core.ObservationStatus.SUCCEEDED, reused=submission.reuses
        ).model_copy(update={"kind": kind})
        for stage, kind in zip(_STAGES, kinds, strict=True)
    )
    done = _report(state, started, finished, evidence)
    kept = {item.key for item in core.project(done.state).measurements}
    for stage, item in zip(_STAGES, evidence, strict=True):
        assert (item.key in kept) is (item.kind is _HONEST[stage]), (stage, item.kind)


def test_a_reusing_job_that_ends_without_all_its_sources_records_is_refused() -> None:
    """Dropping the record of one stage would turn the source's failure into a pass."""
    state, submission, started, finished = _reusing_job("local-validation")
    both = tuple(
        record(
            submission, finished, stage, core.ObservationStatus.SUCCEEDED, reused=submission.reuses
        )
        for stage in _STAGES
    )
    short = _report(state, started, finished, both[1:])
    (result,) = results(short)
    assert result.failure is core.MeasurementFailure.INFRASTRUCTURE
    full = _report(state, started, finished, both)
    (result,) = results(full)
    assert result.failure is None


def test_a_short_reused_result_is_retried_with_a_fresh_measurement() -> None:
    """An incomplete reused result is the machinery's, so the candidate gets one fresh run."""
    state, submission, started, finished = _reusing_job("local-validation")
    both = tuple(
        record(
            submission, finished, stage, core.ObservationStatus.SUCCEEDED, reused=submission.reuses
        )
        for stage in _STAGES
    )
    short = _report(state, started, finished, both[1:])
    retried = requested(
        short.state,
        identity="third",
        measurement=measured("official", "b", "sha256:a"),
    )
    (fresh,) = [r for r in retried.requests if isinstance(r, core.SubmitMeasurement)]
    assert fresh.reuses is None
    assert fresh.request_id != submission.request_id


def test_a_run_on_a_shared_host_is_as_trusted_as_one_on_a_host_held_alone() -> None:
    """Hosts held alone lower the noise; they are recorded, never required for trust."""
    for source, asked_on in (("shared-host", "same"), ("same", "shared-host")):
        state, agent = agent_run(
            core.initial_state(),
            "c1",
            measured("local-validation", "a", "sha256:a", source),
            "passed",
        )
        assert agent is not None
        asked = official(state, measured("official", "a", "sha256:a", asked_on))
        (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
        assert submission.reuses == agent.request_id, (source, asked_on)


def attempt_with_checkpoints(inputs: dict[str, str]) -> tuple[core.CoreState, core.Scope]:
    """A run whose one attempt retained a checkpoint of each revision, with its measured input."""
    state = core.initial_state()
    owner = core.AttemptView(
        attempt_id=core.AttemptId(root="attempt"),
        item_id=core.ItemId(root="item"),
        generation=0,
        phase=core.AttemptPhase.ACTIVE,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
        admission_id=core.DecisionId(root="admission"),
        checkpoints=tuple(
            core.AttemptCheckpoint(
                invocation=None,
                request_id=core.RequestId(root=f"snapshot-{name}"),
                revision=revision(name),
                retention="wip",
                measured_input=digest,
            )
            for name, digest in inputs.items()
        ),
    )
    attempts = core.AttemptsState(attempts=(owner,))
    return state.model_copy(update={"attempts": attempts}), core.Scope(
        owner=owner.attempt_id, generation=0
    )


def measure_in(
    state: core.CoreState, scope: core.Scope, name: str, candidate: str
) -> tuple[core.CoreState, core.SubmitMeasurement]:
    """A strategy's official measurement of ``candidate``, its input unnamed, and its submission."""
    decision = core.Measure(
        decision_id=core.DecisionId(root=name),
        scope=scope,
        plan=plan(
            purpose="official", candidate=revision(candidate), dedicated_measurement_resources=True
        ),
    )
    asked = transition(
        state,
        core.DecisionSubmitted(
            decision=core.Wait(request=decision, until=core.Within(limit=100.0)),
            expected_revision=state.revision,
        ),
    )
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
    return asked.state, submission


def test_a_measurement_takes_its_input_from_the_attempts_checkpoint_of_the_revision() -> None:
    """The strategy names a revision only; the snapshot that retained it named its input."""
    state, scope = attempt_with_checkpoints(
        {"0027142": "sha256:input", "509dd57": "sha256:input", "a1b2c3d": "sha256:other"}
    )
    asked, first = measure_in(state, scope, "first", "0027142")
    assert first.plan.measured_input == "sha256:input"
    assert first.reuses is None
    after = ended(asked, first, "passed").state
    _, same = measure_in(after, scope, "second", "509dd57")
    assert same.plan.measured_input == "sha256:input"
    assert same.reuses == first.request_id
    _, other = measure_in(after, scope, "third", "a1b2c3d")
    assert other.plan.measured_input == "sha256:other"
    assert other.reuses is None
