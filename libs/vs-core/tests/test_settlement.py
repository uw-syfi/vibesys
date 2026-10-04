"""Settlement finality, release fencing and replay through the public kernel."""

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


def settlement(
    outcome: Literal["succeeded", "failed", "cancelled", "blocked"] = "failed",
    retention: Literal["discard", "wip", "candidate"] = "discard",
) -> core.Settlement:
    return core.Settlement(
        settlement_id=core.SettlementId(root="settlement"),
        attempt=core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=0),
        candidate=(
            core.RevisionRef(revision_id=core.RevisionId(root="candidate"), digest="candidate")
            if retention != "discard"
            else None
        ),
        assessments=(),
        eligible=False,
        retention=retention,
        outcome=outcome,
    )


def pending_state(value: core.Settlement) -> core.CoreState:
    state = core.initial_state()
    admission = core.DecisionId(root="admission")
    checkpoints = (
        (
            core.AttemptCheckpoint(
                invocation=None,
                request_id=core.RequestId(root="retain"),
                revision=value.candidate,
                retention=value.retention,
            ),
        )
        if value.candidate is not None and value.retention != "discard"
        else ()
    )
    owner = core.AttemptView(
        attempt_id=value.attempt.attempt_id,
        item_id=core.ItemId(root="item"),
        generation=value.attempt.generation,
        phase=core.AttemptPhase.TERMINAL,
        admission_id=admission,
        closure=core.AttemptClosure(
            disposition="cancel" if value.outcome == "cancelled" else "settle",
            requested_at=1.0,
            authority=core.RequestId(root="close"),
            admission_id=admission,
        ),
        checkpoints=checkpoints,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )
    return state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "settlement": core.SettlementState(
                pending=(value,),
                adoption=core.Adoption(
                    selection=core.TrustedBaseline(revision=state.run.facts.baseline)
                ),
            ),
        }
    )


def reload(state: core.CoreState) -> core.CoreState:
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


def released(value: core.Settlement) -> core.OwnershipSettled:
    return core.OwnershipSettled(attempt=value.attempt, released=True)


@given(
    outcome=st.sampled_from(["succeeded", "failed", "cancelled", "blocked"]),
    retention=st.sampled_from(["discard", "wip", "candidate"]),
)
def test_retention_and_outcome_survive_one_terminal_publication(
    outcome: Literal["succeeded", "failed", "cancelled", "blocked"],
    retention: Literal["discard", "wip", "candidate"],
) -> None:
    value = settlement(outcome, "discard" if outcome == "cancelled" else retention)
    state = pending_state(value)
    before = reload(state)
    transition = core.step(state, released(value))
    assert transition == core.step(before, released(value))
    assert state == before
    assert transition.state.settlement.pending == ()
    assert transition.state.settlement.settlements == (value,)
    assert core.project(transition.state).settlements == (value,)
    assert transition.state.settlement.adoption == state.settlement.adoption
    assert transition.requests == ()
    assert transition.events == (core.AttemptSettled(settlement=value),)
    assert transition.state.attempts == state.attempts
    assert transition.state.sessions == state.sessions
    assert transition.state.evaluation == state.evaluation
    assert transition.state.intents == state.intents
    replay = core.step(reload(transition.state), released(value))
    assert replay.events == replay.requests == ()
    assert replay.state.settlement == transition.state.settlement


@given(events=st.lists(st.integers(min_value=0, max_value=5), min_size=1, max_size=50))
def test_duplicate_reordered_and_stale_events_publish_at_most_once(events: list[int]) -> None:
    value = settlement()
    state = pending_state(value)
    stale = value.attempt.model_copy(update={"generation": 1})
    choices = (
        released(value),
        core.OwnershipSettled(attempt=value.attempt, released=False),
        core.OwnershipSettled(attempt=stale, released=True),
        core.AssessmentSubmitted(settlement=value),
        core.AttemptSettled(settlement=value),
        core.AttemptSettled(
            settlement=value.model_copy(update={"settlement_id": core.SettlementId(root="foreign")})
        ),
    )
    callbacks = 0
    for index in events:
        event = choices[index]
        transition = core.step(state, event)
        assert transition == core.step(reload(state), event)
        callbacks += sum(isinstance(output, core.AttemptSettled) for output in transition.events)
        assert callbacks <= 1
        assert len(transition.state.settlement.settlements) <= 1
        assert len(transition.state.settlement.pending) <= 1
        assert transition.requests == ()
        assert core.project(transition.state).scheduling.charged == 0
        assert core.project(transition.state).scheduling.refunded == 0
        assert transition.state.settlement.adoption == state.settlement.adoption
        state = reload(transition.state)


@given(
    kind=st.sampled_from(["session", "job", "workspace", "operation", "request"]),
    blocked=st.booleans(),
)
def test_cleanup_dependency_fences_settlement_and_slot_ordering(
    kind: Literal["session", "job", "workspace", "operation", "request"], *, blocked: bool
) -> None:
    value = settlement()
    state = pending_state(value)
    identities = {
        "session": core.SessionId(root="session"),
        "job": core.ResourceId(root="job"),
        "workspace": core.RequestId(root="discard"),
        "operation": core.OperationId(root="operation"),
        "request": core.RequestId(root="request"),
    }
    owner = state.attempts.attempts[0]
    owner = owner.model_copy(
        update={
            "phase": core.AttemptPhase.BLOCKED if blocked else core.AttemptPhase.CLOSING,
            "release_dependencies": (core.ReleaseDependency(kind=kind, identity=identities[kind]),),
        }
    )
    waiting = core.AttemptRequest(
        decision_id=core.DecisionId(root="next"),
        attempt_id=core.AttemptId(root="next"),
        item_id=core.ItemId(root="next"),
        generation=0,
        admission_charge=1,
    )
    scheduling = core.SchedulingState(
        queue=(waiting,),
        slots=(
            core.Slot(
                attempt=value.attempt,
                admission_id=core.DecisionId(root="admission"),
                admitted_at=0.0,
            ),
        ),
    )
    state = state.model_copy(
        update={"attempts": core.AttemptsState(attempts=(owner,)), "scheduling": scheduling}
    )
    for event in (
        core.OwnershipSettled(attempt=value.attempt, released=False, blocked=blocked),
        released(value),
        core.AttemptSettled(settlement=value),
    ):
        result = core.step(reload(state), event)
        assert result.state.settlement == state.settlement
        assert result.state.scheduling == scheduling
        assert result.events == result.requests == ()


@pytest.mark.parametrize("missing", ["closure", "episode", "checkpoint", "phase", "intent"])
def test_missing_owner_proof_cannot_publish_retained_candidate(missing: str) -> None:
    value = settlement("succeeded", "candidate")
    state = pending_state(value)
    owner = state.attempts.attempts[0]
    replacements = {
        "closure": {"closure": None},
        "episode": {"admission_id": core.DecisionId(root="new-episode")},
        "checkpoint": {"checkpoints": ()},
        "phase": {"phase": core.AttemptPhase.ACTIVE},
        "intent": {"pending_intents": (core.RequestId(root="unfinished"),)},
    }
    owner = owner.model_copy(update=replacements[missing])
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    result = core.step(state, released(value))
    assert result.state.settlement == state.settlement
    assert result.events == result.requests == ()


def test_first_pending_completion_survives_cancellation_and_stop() -> None:
    # Legacy T:_settle:944 and R:record:332-351 preserve the first recorded result.
    # Legacy population/test_population_search.py failed_snapshotted_cold_start.
    value = settlement("failed", "wip")
    state = pending_state(value)
    late = value.model_copy(
        update={"outcome": "cancelled", "retention": "discard", "candidate": None}
    )
    for status in (core.RunStatus.RUNNING, core.RunStatus.CLOSING):
        boundary = state.model_copy(update={"run": state.run.model_copy(update={"status": status})})
        result = core.step(boundary, core.AssessmentSubmitted(settlement=late))
        assert result.state.settlement.pending == (value,)
        assert result.events == result.requests == ()
        final = core.step(reload(result.state), released(value))
        assert final.state.settlement.settlements == (value,)
        assert final.events == (core.AttemptSettled(settlement=value),)
        replay = core.step(final.state, core.AssessmentSubmitted(settlement=late))
        assert replay.state.settlement.settlements == (value,)
        assert replay.events == replay.requests == ()


def test_final_completion_cannot_be_replaced_by_another_settlement_id() -> None:
    value = settlement()
    result = core.step(pending_state(value), released(value))
    late = value.model_copy(update={"settlement_id": core.SettlementId(root="replacement")})
    duplicate = core.step(reload(result.state), core.AssessmentSubmitted(settlement=late))
    assert duplicate.state.settlement.settlements == (value,)
    assert duplicate.events == duplicate.requests == ()


def test_unknown_attempt_and_stale_generation_grant_no_cleanup_authority() -> None:
    value = settlement()
    state = core.initial_state()
    for event in (
        core.AssessmentSubmitted(settlement=value),
        released(value),
        core.AttemptSettled(settlement=value),
    ):
        result = core.step(state, event)
        assert result.state.settlement == state.settlement
        assert result.events == result.requests == ()


def measured_state() -> tuple[core.CoreState, core.Settlement]:
    value = settlement("succeeded", "candidate")
    assert value.candidate is not None
    state = pending_state(value)
    scope = core.Scope(owner=value.attempt.attempt_id, generation=value.attempt.generation)
    request_id = core.RequestId(root="measurement")
    evidence = core.EvidenceRef(
        evidence_id=core.EvidenceId(root="correctness"),
        kind=core.EvidenceKind.CORRECTNESS,
        purpose="official",
        scope=scope,
        source_request=request_id,
        candidate=value.candidate,
        observation_sequence=2,
        evaluator_digest=state.run.facts.evaluator_digest,
        workload_digest=state.run.facts.workload_digest,
        environment_digest=state.run.facts.environment_digest,
        provenance="trusted",
        status=core.ObservationStatus.SUCCEEDED,
    )
    plan = core.MeasurementPlan(
        purpose="official",
        candidate=value.candidate,
        evaluator_digest=evidence.evaluator_digest,
        workload_digest=evidence.workload_digest,
        environment_digest=evidence.environment_digest,
        stages=(core.MeasurementStage(stage_id="correctness", execution_budget=10.0),),
        policy="ordered",
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="recipe"),
        submitted_at=0.0,
        queue_allowance=0.0,
        deadline_at=10.0,
    )
    job = core.OwnedJob(
        resource_id=core.ResourceId(root="measurement"),
        submission_id=request_id,
        scope=scope,
        plan=plan,
        status=core.ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
        observation=core.Observation(
            event_id=core.EventId(root="measurement-result"),
            request_id=request_id,
            scope=scope,
            sequence=2,
            observed_at=2.0,
            status=core.ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=True,
            resource_id=core.ResourceId(root="measurement"),
        ),
        evidence=(evidence,),
    )
    value = value.model_copy(
        update={
            "eligible": True,
            "assessments": (
                core.AssessmentProposal(
                    kind=core.AssessmentKind.CORRECTNESS,
                    verdict="satisfied",
                    sources=(evidence.evidence_id,),
                    candidate=value.candidate,
                    schema_version=1,
                ),
            ),
        }
    )
    requirements = core.EvidenceRequirements(
        required_assessments=(core.AssessmentKind.CORRECTNESS,),
        required_evidence=(
            core.EvidenceRequirement(
                kind=core.EvidenceKind.CORRECTNESS, provenance="trusted", purpose="official"
            ),
        ),
    )
    return state.model_copy(
        update={
            "run": state.run.model_copy(update={"requirements": requirements}),
            "settlement": state.settlement.model_copy(update={"pending": (value,)}),
            "evaluation": core.EvaluationState(jobs=(job,), evidence=(evidence,)),
        }
    ), value


def test_exact_measured_candidate_remains_eligible_after_reload() -> None:
    state, value = measured_state()
    result = core.step(reload(state), released(value))
    assert result.state.settlement.settlements == (value,)
    assert result.events == (core.AttemptSettled(settlement=value),)


@given(
    corrupt=st.sampled_from(
        [
            "scope",
            "digest",
            "revision",
            "evaluator_digest",
            "workload_digest",
            "environment_digest",
            "provenance",
            "purpose",
            "kind",
            "sequence",
            "source",
            "status",
            "job_evidence",
            "job_terminal",
            "verdict",
            "assessment_source",
            "assessment_candidate",
            "duplicate_source",
        ]
    )
)
def test_evidence_attribution_corruption_cannot_establish_eligibility(corrupt: str) -> None:
    state, value = measured_state()
    evidence = state.evaluation.evidence[0]
    job = state.evaluation.jobs[0]
    updates = {
        "scope": {"scope": evidence.scope.model_copy(update={"generation": 1})},
        "digest": {"candidate": evidence.candidate.model_copy(update={"digest": "wrong"})},
        "revision": {
            "candidate": evidence.candidate.model_copy(
                update={"revision_id": core.RevisionId(root="wrong")}
            )
        },
        "evaluator_digest": {"evaluator_digest": "wrong"},
        "workload_digest": {"workload_digest": "wrong"},
        "environment_digest": {"environment_digest": "wrong"},
        "provenance": {"provenance": "self-report"},
        "purpose": {"purpose": "profile"},
        "kind": {"kind": core.EvidenceKind.BENCHMARK},
        "sequence": {"observation_sequence": 1},
        "source": {"source_request": core.RequestId(root="wrong")},
        "status": {"status": core.ObservationStatus.FAILED},
    }
    evidence = evidence.model_copy(update=updates.get(corrupt, {}))
    job = job.model_copy(
        update={"evidence": (evidence,) if corrupt != "sequence" else job.evidence}
    )
    if corrupt == "job_evidence":
        job = job.model_copy(update={"evidence": ()})
    if corrupt == "job_terminal":
        job = job.model_copy(update={"terminal": False})
    assessment = value.assessments[0]
    if corrupt == "verdict":
        assessment = assessment.model_copy(update={"verdict": "rejected"})
    if corrupt == "assessment_source":
        assessment = assessment.model_copy(update={"sources": (core.EvidenceId(root="missing"),)})
    if corrupt == "assessment_candidate":
        assessment = assessment.model_copy(update={"candidate": state.run.facts.baseline})
    if corrupt == "duplicate_source":
        assessment = assessment.model_copy(
            update={"sources": (*assessment.sources, *assessment.sources)}
        )
    value = value.model_copy(update={"assessments": (assessment,)})
    state = state.model_copy(
        update={
            "settlement": state.settlement.model_copy(update={"pending": (value,)}),
            "evaluation": core.EvaluationState(jobs=(job,), evidence=(evidence,)),
        }
    )
    result = core.step(state, released(value))
    assert result == core.step(reload(state), released(value))
    final = result.state.settlement.settlements[0]
    assert not final.eligible
    assert final.assessments == value.assessments
    assert result.state.evaluation == state.evaluation
    assert final.outcome == "succeeded"


def judge_state() -> tuple[core.CoreState, core.Settlement]:
    state, value = measured_state()
    scope = core.Scope(owner=value.attempt.attempt_id, generation=0)
    spec = core.SessionSpec(
        session_id=core.SessionId(root="judge"),
        role_id=core.RoleId(root="judge"),
        policy="reuse",
        lifetime="owner",
        access=core.Access.READ_ONLY,
    )
    source = core.InvocationRef(
        session_id=spec.session_id,
        invocation_id=core.InvocationId(root="judge"),
        generation=0,
    )
    schema = core.SchemaRef(name="judge", version=1)
    assert value.candidate is not None
    turn = core.TurnSpec(
        session=spec,
        invocation_id=source.invocation_id,
        workspace=core.WorkspaceRef(
            scope=scope, revision=value.candidate, mode=core.WorkspaceMode.READ_ONLY_REVISION
        ),
        prompts=(),
        output_schema=schema,
        deadline_at=10.0,
        charge_class="free",
    )
    invocation = core.Invocation(
        invocation=source,
        scope=scope,
        turn=turn,
        phase=core.SessionPhase.TERMINAL,
        observation=core.Observation(
            event_id=core.EventId(root="judge"),
            request_id=core.RequestId(root="judge"),
            scope=scope,
            sequence=1,
            observed_at=1.0,
            status=core.ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
        ),
        output_schema=schema,
        output_json='{"verdict":"satisfied"}',
    )
    value = value.model_copy(
        update={"assessments": (value.assessments[0].model_copy(update={"sources": (source,)}),)}
    )
    requirements = state.run.requirements.model_copy(
        update={
            "required_evidence": (),
            "assessment_authorities": (
                core.AssessmentAuthority(
                    kind=core.AssessmentKind.CORRECTNESS, role_id=spec.role_id, output_schema=schema
                ),
            ),
        }
    )
    request = core.DispatchTurn(
        request_id=core.RequestId(root="judge"), scope=scope, deadline_at=10.0, turn=turn
    )
    intent = core.Intent(
        request_id=core.RequestId(root="judge"),
        request=request,
        payload_digest="judge",
        lifecycle=core.LifecycleClass.SESSION_TURN,
        phase=core.IntentPhase.COMPLETED,
        reconcile_deadline_at=10.0,
    )
    return state.model_copy(
        update={
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
            "run": state.run.model_copy(update={"requirements": requirements}),
            "sessions": core.SessionsState(invocations=(invocation,)),
            "evaluation": core.EvaluationState(),
            "settlement": state.settlement.model_copy(update={"pending": (value,)}),
        }
    ), value


@given(
    corrupt=st.sampled_from(
        [
            "none",
            "authority",
            "role",
            "schema",
            "accepted",
            "terminal",
            "generation",
            "workspace",
            "json",
            "missing_output",
        ]
    )
)
def test_unmeasured_judge_requires_declared_exact_final_authority(corrupt: str) -> None:
    state, value = judge_state()
    invocation = state.sessions.invocations[0]
    assert invocation.observation is not None
    if corrupt == "authority":
        state = state.model_copy(
            update={
                "run": state.run.model_copy(
                    update={
                        "requirements": state.run.requirements.model_copy(
                            update={"assessment_authorities": ()}
                        )
                    }
                )
            }
        )
    replacements = {
        "role": {
            "turn": invocation.turn.model_copy(
                update={
                    "session": invocation.turn.session.model_copy(
                        update={"role_id": core.RoleId(root="wrong")}
                    )
                }
            )
        },
        "schema": {"output_schema": core.SchemaRef(name="wrong", version=1)},
        "accepted": {"observation": invocation.observation.model_copy(update={"accepted": False})},
        "terminal": {"observation": invocation.observation.model_copy(update={"terminal": False})},
        "generation": {"scope": invocation.scope.model_copy(update={"generation": 1})},
        "workspace": {"turn": invocation.turn.model_copy(update={"workspace": invocation.scope})},
        "json": {"output_json": "malformed"},
        "missing_output": {"output_json": None},
    }
    invocation = invocation.model_copy(update=replacements.get(corrupt, {}))
    state = state.model_copy(update={"sessions": core.SessionsState(invocations=(invocation,))})
    result = core.step(reload(state), released(value))
    assert result.state.settlement.settlements[0].eligible == (corrupt == "none")
    assert result.state.evaluation.evidence == ()


@pytest.mark.parametrize("status", [core.RunStatus.RUNNING, core.RunStatus.CLOSING])
def test_withdrawal_first_keeps_discard_and_ineligibility_during_late_completion(
    status: core.RunStatus,
) -> None:
    state, value = measured_state()
    owner = state.attempts.attempts[0]
    assert owner.closure is not None
    owner = owner.model_copy(
        update={"closure": owner.closure.model_copy(update={"disposition": "cancel"})}
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "run": state.run.model_copy(update={"status": status}),
            "settlement": state.settlement.model_copy(update={"pending": ()}),
        }
    )
    result = core.step(reload(state), core.AssessmentSubmitted(settlement=value))
    final = result.state.settlement.settlements[0]
    assert final.outcome == "cancelled"
    assert final.retention == "discard"
    assert not final.eligible
    assert result.events == (core.AttemptSettled(settlement=final),)
    duplicate = core.step(reload(result.state), core.AssessmentSubmitted(settlement=value))
    assert duplicate.events == duplicate.requests == ()
    assert duplicate.state.settlement.settlements == (final,)


def test_released_owner_accepts_measured_assessment_without_repeating_retention() -> None:
    state, value = measured_state()
    state = state.model_copy(
        update={"settlement": state.settlement.model_copy(update={"pending": ()})}
    )
    result = core.step(state, core.AssessmentSubmitted(settlement=value))
    assert result.state.settlement.settlements == (value,)
    assert result.state.settlement.pending == ()
    assert result.events == (core.AttemptSettled(settlement=value),)
    assert result.requests == ()


@given(sequence=st.integers(min_value=3, max_value=1000))
def test_later_release_observation_preserves_original_evidence_eligibility(sequence: int) -> None:
    state, value = measured_state()
    job = state.evaluation.jobs[0]
    assert job.observation is not None
    job = job.model_copy(
        update={"observation": job.observation.model_copy(update={"sequence": sequence})}
    )
    state = state.model_copy(
        update={"evaluation": state.evaluation.model_copy(update={"jobs": (job,)})}
    )
    result = core.step(reload(state), released(value))
    assert result.state.settlement.settlements == (value,)
    assert result.state.evaluation.evidence[0].observation_sequence == 2


def test_future_evidence_sequence_cannot_prove_eligibility() -> None:
    state, value = measured_state()
    evidence = state.evaluation.evidence[0].model_copy(update={"observation_sequence": 3})
    job = state.evaluation.jobs[0].model_copy(update={"evidence": (evidence,)})
    state = state.model_copy(
        update={"evaluation": core.EvaluationState(jobs=(job,), evidence=(evidence,))}
    )
    result = core.step(reload(state), released(value))
    assert not result.state.settlement.settlements[0].eligible


@pytest.mark.parametrize("retention", ["wip", "candidate"])
def test_retention_requires_an_explicit_revision(retention: Literal["wip", "candidate"]) -> None:
    value = settlement("failed", retention)
    state = pending_state(value)
    value = value.model_copy(update={"candidate": None})
    state = state.model_copy(
        update={"settlement": state.settlement.model_copy(update={"pending": ()})}
    )
    with pytest.raises(core.ContractValidationError, match="candidate"):
        core.step(state, core.AssessmentSubmitted(settlement=value))


def test_parked_owner_ignores_late_completion_without_changing_adoption() -> None:
    value = settlement()
    state = pending_state(value)
    owner = state.attempts.attempts[0]
    assert owner.closure is not None
    owner = owner.model_copy(
        update={
            "closure": owner.closure.model_copy(update={"disposition": "park"}),
            "phase": core.AttemptPhase.PARKED,
        }
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "settlement": state.settlement.model_copy(update={"pending": ()}),
        }
    )
    result = core.step(reload(state), core.AssessmentSubmitted(settlement=value))
    assert result.state.settlement == state.settlement
    assert result.events == result.requests == ()
