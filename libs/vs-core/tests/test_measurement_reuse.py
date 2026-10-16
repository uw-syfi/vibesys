"""An official measurement is answered by the scope's one run of the same input (#2949).

An agent's own evaluation runs the task's evaluation on its candidate. When the strategy then
asks for the official measurement of the same input, core answers it with that run instead of
a second one: the submission names the run it reuses, and the records the runtime reports for
it must be that run's own. These tests drive the kernel through its public events only.
"""

from typing import Literal

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_measurements import committed, plan, requested, transition

type Outcome = Literal["passed", "failed", "machinery"]

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


def measured(
    purpose: Literal["local-validation", "official"],
    candidate: str,
    measured_input: str | None,
) -> core.MeasurementPlan:
    return plan(purpose=purpose, candidate=revision(candidate), measured_input=measured_input)


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
    if request.plan.purpose == "local-validation":
        kind = core.EvidenceKind.LOCAL_VALIDATION
    elif stage == "accuracy":
        kind = core.EvidenceKind.CORRECTNESS
    else:
        kind = core.EvidenceKind.BENCHMARK
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
        plan(purpose="local-validation", candidate=revision("0027142")),
        "passed",
    )
    assert agent is not None
    asked = official(state, plan(purpose="official", candidate=revision("0027142")))
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
            st.sampled_from(("passed", "failed", "machinery")),
        ),
        max_size=5,
    ),
    target=st.tuples(st.integers(min_value=0, max_value=2), st.integers(min_value=0, max_value=2)),
    input_known=st.booleans(),
    reload_at=st.integers(min_value=0, max_value=5),
)
def test_an_official_measurement_reuses_exactly_the_scopes_one_latest_conclusive_run(
    *,
    runs: list[tuple[int, int, Outcome]],
    target: tuple[int, int],
    input_known: bool,
    reload_at: int,
) -> None:
    """Any sequence of agent runs of inputs (two commits each), with a reload anywhere.

    The official measurement of input ``target[0]`` (at commit ``target[1]``) reuses a run iff
    the scope ran that input exactly once, that run is the scope's latest, and it ended
    conclusively; a commit the agent never ran matches by measured input only when it is known.
    """
    state = core.initial_state()
    jobs: list[tuple[int, int, Outcome, core.RequestId]] = []
    for number, (source_input, commit, outcome) in enumerate(runs):
        if number == reload_at:
            state = reloaded(state)
        name = f"i{source_input}c{commit}"
        state, request = agent_run(
            state,
            f"call-{number}",
            measured("local-validation", name, f"sha256:{source_input}"),
            outcome,
        )
        if request is not None:
            assert request.request_id is not None
            jobs.append((source_input, commit, outcome, request.request_id))
    if reload_at >= len(runs):
        state = reloaded(state)
    wanted, commit = target
    measured_input = f"sha256:{wanted}" if input_known else None
    asked = official(state, measured("official", f"i{wanted}c{commit}", measured_input))
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]

    same = [job for job in jobs if job[0] == wanted and (input_known or job[1] == commit)]
    expected = (
        same[0][3] if len(same) == 1 and same[0] == jobs[-1] and same[0][2] != "machinery" else None
    )
    assert submission.reuses == expected
    again = transition(
        asked.state,
        core.DecisionSubmitted(
            decision=core.Wait(
                request=core.Measure(
                    decision_id=core.DecisionId(root="official"),
                    scope=scope_of(state),
                    plan=measured("official", f"i{wanted}c{commit}", measured_input),
                ),
                until=core.Within(limit=100.0),
            ),
            expected_revision=asked.state.revision,
        ),
    )
    assert submissions(again.state) == submissions(asked.state), "a replay allocates nothing"
    assert reloaded(asked.state) == asked.state


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
        plan=plan(purpose="official", candidate=revision(candidate)),
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
