"""Settlement finality, release fencing and replay through the public kernel."""

from typing import ClassVar, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

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


def reload(state: core.CoreState, registry: core.OperationRegistry | None = None) -> core.CoreState:
    envelope = core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=1),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )
    codec = registry or core.OperationRegistry()
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
    owner = state.attempts.attempts[0].model_copy(
        update={
            "charges": (
                core.ChargeReceipt(
                    charge_id=core.ChargeId(root="admission-charge"),
                    kind=core.ChargeKind.ADMISSION,
                    charged=1,
                    refunded=1,
                ),
                core.ChargeReceipt(
                    charge_id=core.ChargeId(root="attempt-charge"),
                    kind=core.ChargeKind.ATTEMPT,
                    charged=1,
                    refunded=1,
                ),
            )
        }
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    accounting = core.project(state).scheduling
    assert accounting.charged <= state.run.limits.max_attempts
    assert accounting.refunded <= accounting.charged
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
        assert core.project(transition.state).scheduling == accounting
        assert accounting.charged <= transition.state.run.limits.max_attempts
        assert accounting.refunded <= accounting.charged
        assert transition.state.attempts == state.attempts
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
    intent = state.intents.intents[0]
    assert isinstance(intent.request, core.DispatchTurn)
    intent = intent.model_copy(
        update={"request": intent.request.model_copy(update={"turn": invocation.turn})}
    )
    state = state.model_copy(
        update={
            "sessions": core.SessionsState(invocations=(invocation,)),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    )
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


class RegisteredJudgeOutcome(core.Value):
    verdict: Literal["satisfied"] = "satisfied"


class RegisteredJudgeRequest(core.OperationRequest):
    kind: Literal["test.settlement.judge"] = "test.settlement.judge"
    lifecycle: Literal[core.LifecycleClass.SESSION_TURN] = core.LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[BaseModel]] = RegisteredJudgeOutcome
    turn: core.TurnSpec


def normalize_judge(request: core.OperationRequest) -> core.TurnSpec:
    assert isinstance(request, RegisteredJudgeRequest)
    return request.turn


def registered_judge_state() -> tuple[core.CoreState, core.Settlement, core.OperationRegistry]:
    state, value = judge_state()
    invocation = state.sessions.invocations[0]
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=core.OperationDescriptor(
                    kind="test.settlement.judge",
                    lifecycle=core.LifecycleClass.SESSION_TURN,
                    request_schema=core.SchemaRef(name="registered-judge", version=1),
                    outcome_schema=core.SchemaRef(name="registered-judge-result", version=1),
                    inspect=True,
                    cancel=True,
                    watch=True,
                ),
                request_model=RegisteredJudgeRequest,
                outcome_model=RegisteredJudgeOutcome,
                normalize_turn=normalize_judge,
            ),
        )
    )
    decision_id = core.DecisionId(root="judge-operation")
    request_id = core.RequestId(root="judge")
    operation_id = core.OperationId(root="judge-operation")
    decision = codec.validate_decision(
        core.Operation(
            decision_id=decision_id,
            scope=invocation.scope,
            deadline_at=10.0,
            request=RegisteredJudgeRequest(turn=invocation.turn),
        )
    )
    assert decision.registered_wire is not None
    request = core.ExecuteRegisteredOperation(
        request_id=request_id,
        scope=invocation.scope,
        deadline_at=10.0,
        decision_id=decision_id,
        operation_id=operation_id,
        operation=decision.registered_wire,
        retry_limit=0,
    )
    receipt = core.DecisionReceipt(
        decision_id=decision_id,
        decision=decision,
        payload_digest="registered-judge",
        feedback=core.Accepted(decision_id=decision_id, request_ids=(request_id,)),
        request_ids=(request_id,),
        completion=core.CompletionStatus.SUCCEEDED,
    )
    intent = core.Intent(
        request_id=request_id,
        request=request,
        payload_digest="registered-judge",
        lifecycle=core.LifecycleClass.SESSION_TURN,
        phase=core.IntentPhase.COMPLETED,
        reconcile_deadline_at=10.0,
    )
    return (
        state.model_copy(
            update={
                "registry": codec.descriptors,
                "run": state.run.model_copy(update={"receipts": (receipt,)}),
                "sessions": core.SessionsState(
                    invocations=(
                        invocation.model_copy(update={"registered_operation": operation_id}),
                    )
                ),
                "intents": state.intents.model_copy(update={"intents": (intent,)}),
            }
        ),
        value,
        codec,
    )


@given(
    corrupt=st.sampled_from(
        [
            "none",
            "operation",
            "request",
            "decision",
            "receipt_requests",
            "receipt_scope",
            "rejected",
            "deferred",
            "source_session",
            "source_invocation",
            "source_generation",
            "normalized_turn",
        ]
    )
)
def test_registered_judge_authority_requires_exact_canonical_correspondence(corrupt: str) -> None:
    state, value, codec = registered_judge_state()
    invocation = state.sessions.invocations[0]
    intent = state.intents.intents[0]
    request = intent.request
    assert isinstance(request, core.ExecuteRegisteredOperation)
    receipt = state.run.receipts[0]
    if corrupt == "operation":
        invocation = invocation.model_copy(
            update={"registered_operation": core.OperationId(root="wrong")}
        )
    if corrupt == "request":
        assert invocation.observation is not None
        invocation = invocation.model_copy(
            update={
                "observation": invocation.observation.model_copy(
                    update={"request_id": core.RequestId(root="wrong")}
                )
            }
        )
    if corrupt == "decision":
        request = request.model_copy(update={"decision_id": core.DecisionId(root="wrong")})
    if corrupt == "receipt_requests":
        receipt = receipt.model_copy(update={"request_ids": ()})
    if corrupt in {"rejected", "deferred"}:
        feedback = core.Rejected(
            decision_id=receipt.decision_id,
            code=core.RejectionCode.DEPENDENCY,
            path=("dependency",),
            detail="proof absent",
            retry_after=core.DependencyRef(request_id=request.request_id)
            if corrupt == "deferred"
            else None,
        )
        receipt = receipt.model_copy(update={"feedback": feedback})
    if corrupt in {"receipt_scope", "normalized_turn"}:
        assert isinstance(receipt.decision, core.Operation)
        assert isinstance(receipt.decision.request, RegisteredJudgeRequest)
        raw = receipt.decision
        assert isinstance(raw.request, RegisteredJudgeRequest)
        changed_turn = raw.request.turn.model_copy(
            update={"invocation_id": core.InvocationId(root="wrong")}
        )
        changed = codec.validate_decision(
            raw.model_copy(
                update={
                    "scope": raw.scope.model_copy(update={"generation": 1})
                    if corrupt == "receipt_scope"
                    else raw.scope,
                    "request": RegisteredJudgeRequest(turn=changed_turn)
                    if corrupt == "normalized_turn"
                    else raw.request,
                }
            )
        )
        receipt = receipt.model_copy(update={"decision": changed})
        if corrupt == "normalized_turn":
            assert changed.registered_wire is not None
            request = request.model_copy(update={"operation": changed.registered_wire})
    if corrupt.startswith("source_"):
        source = value.assessments[0].sources[0]
        assert isinstance(source, core.InvocationRef)
        replacements = {
            "source_session": {"session_id": core.SessionId(root="wrong")},
            "source_invocation": {"invocation_id": core.InvocationId(root="wrong")},
            "source_generation": {"generation": 1},
        }
        assessment = value.assessments[0].model_copy(
            update={"sources": (source.model_copy(update=replacements[corrupt]),)}
        )
        value = value.model_copy(update={"assessments": (assessment,)})
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": (receipt,)}),
            "sessions": core.SessionsState(invocations=(invocation,)),
            "intents": state.intents.model_copy(
                update={"intents": (intent.model_copy(update={"request": request}),)}
            ),
            "settlement": state.settlement.model_copy(update={"pending": (value,)}),
        }
    )
    result = core.step(state, released(value))
    assert result == core.step(reload(state, codec), released(value))
    assert result.state.settlement.settlements[0].eligible == (corrupt == "none")


@given(
    missing=st.sampled_from(
        ["evidence", "assessment", "contradiction", "eligible", "wip", "generic"]
    )
)
def test_mandatory_proofs_and_explicit_candidate_choice_cannot_be_inferred(missing: str) -> None:
    state, value = measured_state()
    if missing == "evidence":
        state = state.model_copy(
            update={"evaluation": state.evaluation.model_copy(update={"evidence": ()})}
        )
    if missing == "assessment":
        value = value.model_copy(update={"assessments": ()})
    if missing == "contradiction":
        value = value.model_copy(
            update={
                "assessments": (
                    *value.assessments,
                    value.assessments[0].model_copy(update={"verdict": "rejected"}),
                )
            }
        )
    if missing == "eligible":
        value = value.model_copy(update={"eligible": False})
    if missing == "wip":
        value = value.model_copy(update={"retention": "wip"})
        owner = state.attempts.attempts[0]
        owner = owner.model_copy(
            update={
                "checkpoints": tuple(
                    checkpoint.model_copy(update={"retention": "wip"})
                    for checkpoint in owner.checkpoints
                )
            }
        )
        state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    if missing == "generic":
        job = state.evaluation.jobs[0]
        generic = core.RegisteredOwnedJob(
            operation_id=core.OperationId(root="generic"),
            request_id=job.submission_id,
            scope=job.scope,
            resource_pool=core.PoolId(root="jobs"),
            resource_id=job.resource_id,
            status=job.status,
            terminal=True,
            released=True,
            observation=job.observation,
            evidence=job.evidence,
        )
        state = state.model_copy(
            update={
                "evaluation": state.evaluation.model_copy(
                    update={"jobs": (), "registered_jobs": (generic,)}
                )
            }
        )
    state = state.model_copy(
        update={"settlement": state.settlement.model_copy(update={"pending": (value,)})}
    )
    result = core.step(reload(state), released(value))
    assert not result.state.settlement.settlements[0].eligible
    assert result.state.evaluation == state.evaluation


@given(
    disposition=st.sampled_from(["settle", "cancel"]),
    retention=st.sampled_from(["discard", "wip", "candidate"]),
    explicit_candidate=st.booleans(),
)
def test_cancelled_result_discards_before_revision_retention_validation(
    disposition: Literal["settle", "cancel"],
    retention: Literal["discard", "wip", "candidate"],
    *,
    explicit_candidate: bool,
) -> None:
    state, value = measured_state()
    owner = state.attempts.attempts[0]
    assert owner.closure is not None
    owner = owner.model_copy(
        update={"closure": owner.closure.model_copy(update={"disposition": disposition})}
    )
    value = value.model_copy(
        update={
            "outcome": "cancelled" if disposition == "settle" else "succeeded",
            "retention": retention,
            "candidate": value.candidate if explicit_candidate else None,
        }
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "settlement": state.settlement.model_copy(update={"pending": ()}),
        }
    )
    result = core.step(reload(state), core.AssessmentSubmitted(settlement=value))
    final = result.state.settlement.settlements[0]
    assert final.outcome == "cancelled"
    assert final.retention == "discard"
    assert not final.eligible
    assert result.events == (core.AttemptSettled(settlement=final),)
    assert result.requests == ()


@given(
    count=st.integers(min_value=2, max_value=4),
    events=st.lists(
        st.tuples(st.integers(min_value=0, max_value=3), st.integers(min_value=0, max_value=4)),
        min_size=1,
        max_size=60,
    ),
)
def test_multiple_attempt_replays_keep_terminal_callbacks_and_accounting_separate(
    count: int,
    events: list[tuple[int, int]],
) -> None:
    original = settlement()
    state = pending_state(original)
    owner = state.attempts.attempts[0]
    assert owner.closure is not None
    values = tuple(
        original.model_copy(
            update={
                "settlement_id": core.SettlementId(root=f"settlement-{index}"),
                "attempt": original.attempt.model_copy(
                    update={"attempt_id": core.AttemptId(root=f"attempt-{index}")}
                ),
            }
        )
        for index in range(count)
    )
    owners = tuple(
        owner.model_copy(
            update={
                "attempt_id": value.attempt.attempt_id,
                "item_id": core.ItemId(root=f"item-{index}"),
                "admission_id": core.DecisionId(root=f"admission-{index}"),
                "closure": owner.closure.model_copy(
                    update={
                        "authority": core.RequestId(root=f"close-{index}"),
                        "admission_id": core.DecisionId(root=f"admission-{index}"),
                    }
                ),
                "charges": (
                    core.ChargeReceipt(
                        charge_id=core.ChargeId(root=f"charge-{index}"),
                        kind=core.ChargeKind.ADMISSION,
                        charged=1,
                    ),
                ),
            }
        )
        for index, value in enumerate(values)
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=owners),
            "run": state.run.model_copy(
                update={"limits": state.run.limits.model_copy(update={"max_attempts": count})}
            ),
            "settlement": state.settlement.model_copy(update={"pending": values}),
        }
    )
    accounting = core.project(state).scheduling
    assert accounting.charged <= state.run.limits.max_attempts
    assert accounting.refunded <= accounting.charged
    immutable_siblings = (
        state.attempts,
        state.sessions,
        state.evaluation,
        state.intents,
        state.scheduling,
    )
    callbacks: dict[core.AttemptRef, int] = {}
    for index, choice in events:
        value = values[index % count]
        choices = (
            released(value),
            core.OwnershipSettled(attempt=value.attempt, released=True, blocked=True),
            core.OwnershipSettled(attempt=value.attempt, released=False),
            core.OwnershipSettled(
                attempt=value.attempt.model_copy(update={"generation": 1}), released=True
            ),
            core.AssessmentSubmitted(settlement=value),
        )
        event = choices[choice]
        result = core.step(state, event)
        assert result == core.step(reload(state), event)
        if choice != 0:
            assert result.events == ()
            assert result.state.settlement == state.settlement
        for output in result.events:
            assert isinstance(output, core.AttemptSettled)
            attempt = output.settlement.attempt
            callbacks[attempt] = callbacks.get(attempt, 0) + 1
            assert callbacks[attempt] == 1
        terminal = result.state.settlement.settlements
        assert len({value.attempt for value in terminal}) == len(terminal)
        assert len({value.settlement_id for value in terminal}) == len(terminal)
        assert core.project(result.state).scheduling == accounting
        assert accounting.charged <= result.state.run.limits.max_attempts
        assert accounting.refunded <= accounting.charged
        assert (
            result.state.attempts,
            result.state.sessions,
            result.state.evaluation,
            result.state.intents,
            result.state.scheduling,
        ) == immutable_siblings
        assert result.state.settlement.adoption == state.settlement.adoption
        assert result.requests == ()
        state = reload(result.state)


@pytest.mark.parametrize("finalized", [False, True])
def test_settlement_identity_cannot_be_reused_by_another_attempt(*, finalized: bool) -> None:
    value = settlement()
    state = pending_state(value)
    if finalized:
        state = core.step(state, released(value)).state
    owner = state.attempts.attempts[0]
    other = value.model_copy(
        update={
            "attempt": value.attempt.model_copy(update={"attempt_id": core.AttemptId(root="other")})
        }
    )
    assert owner.closure is not None
    other_owner = owner.model_copy(
        update={
            "attempt_id": other.attempt.attempt_id,
            "item_id": core.ItemId(root="other"),
            "admission_id": core.DecisionId(root="other-admission"),
            "closure": owner.closure.model_copy(
                update={
                    "authority": core.RequestId(root="other-close"),
                    "admission_id": core.DecisionId(root="other-admission"),
                }
            ),
        }
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner, other_owner))})
    with pytest.raises(core.ContractValidationError, match="settlement_id"):
        core.step(reload(state), core.AssessmentSubmitted(settlement=other))


def settlement_decision(
    state: core.CoreState, value: core.Settlement, identity: str = "settle"
) -> core.Withdraw:
    return core.Withdraw(
        decision_id=core.DecisionId(root=identity),
        scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
        target=value.attempt,
        disposition=core.Settle(
            assessments=value.assessments,
            eligible=value.eligible,
            retention=value.retention,
            outcome=value.outcome,
            candidate=value.candidate,
        ),
    )


@given(
    outcome=st.sampled_from(["succeeded", "failed", "cancelled", "blocked"]),
    retention=st.sampled_from(["discard", "wip", "candidate"]),
)
def test_public_settle_decision_completes_cleanup_once_independent_of_scientific_outcome(
    outcome: Literal["succeeded", "failed", "cancelled", "blocked"],
    retention: Literal["discard", "wip", "candidate"],
) -> None:
    value = settlement(outcome, retention)
    state = pending_state(value)
    state = state.model_copy(
        update={"settlement": state.settlement.model_copy(update={"pending": ()})}
    )
    decision = settlement_decision(state, value)
    submitted = core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
    result = core.step(state, submitted)
    assert result == core.step(reload(state), submitted)
    receipt = next(
        row for row in result.state.run.receipts if row.decision_id == decision.decision_id
    )
    assert isinstance(receipt.feedback, core.Accepted)
    assert receipt.completion == core.CompletionStatus.SUCCEEDED
    assert len(result.state.settlement.settlements) == 1
    final = result.state.settlement.settlements[0]
    assert final.outcome == outcome
    assert final.retention == ("discard" if outcome == "cancelled" else retention)
    assert final.settlement_id == core.SettlementId(root=f"settlement:{decision.decision_id.root}")
    assert sum(isinstance(event, core.AttemptSettled) for event in result.events) == 1
    assert result.requests == ()
    replay = core.step(
        reload(result.state),
        core.DecisionSubmitted(decision=decision, expected_revision=result.state.revision),
    )
    assert replay.events == replay.requests == ()
    assert replay.state.run.receipts == result.state.run.receipts
    assert replay.state.settlement == result.state.settlement


@given(blocked=st.booleans())
def test_pending_settle_receipt_waits_for_positive_complete_cleanup(*, blocked: bool) -> None:
    value = settlement("failed", "wip").model_copy(
        update={"settlement_id": core.SettlementId(root="settlement:settle")}
    )
    state = pending_state(value)
    decision = settlement_decision(state, value)
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest="settle",
        feedback=core.Accepted(decision_id=decision.decision_id),
    )
    state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": (receipt,)})})
    owner = state.attempts.attempts[0]
    waiting = owner.model_copy(
        update={
            "phase": core.AttemptPhase.CLOSING,
            "release_dependencies": (
                core.ReleaseDependency(kind="job", identity=core.ResourceId(root="live-job")),
            ),
        }
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(waiting,))})
    for event in (
        core.OwnershipSettled(attempt=value.attempt, released=False, blocked=blocked),
        released(value),
        core.AttemptSettled(settlement=value),
    ):
        result = core.step(reload(state), event)
        assert result.state.run.receipts[0].completion is None
        assert result.state.settlement.pending == (value,)
        assert result.events == result.requests == ()
        state = result.state
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    result = core.step(reload(state), released(value))
    assert result.state.run.receipts[0].completion == core.CompletionStatus.SUCCEEDED
    assert result.state.settlement.settlements == (value,)
    assert result.events == (core.AttemptSettled(settlement=value),)
    replay = core.step(reload(result.state), released(value))
    assert replay.events == replay.requests == ()
    assert replay.state.run.receipts == result.state.run.receipts


@pytest.mark.parametrize("finalized", [False, True])
def test_distinct_settle_decision_cannot_override_a_committed_result(*, finalized: bool) -> None:
    value = settlement()
    state = pending_state(value)
    if finalized:
        state = core.step(state, released(value)).state
    decision = settlement_decision(state, value, "competing")
    result = core.step(
        reload(state), core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
    )
    feedback = next(
        row.feedback for row in result.state.run.receipts if row.decision_id == decision.decision_id
    )
    assert isinstance(feedback, core.Rejected)
    assert feedback.code == core.RejectionCode.ALREADY_SETTLED
    assert result.state.settlement == state.settlement
    assert result.events == (feedback,)
    assert result.requests == ()


@pytest.mark.parametrize("owner_status", ["unknown", "parked"])
def test_settle_decision_without_current_owner_authority_has_a_typed_rejection(
    owner_status: str,
) -> None:
    value = settlement()
    state = pending_state(value)
    owner = state.attempts.attempts[0]
    assert owner.closure is not None
    owners = (
        ()
        if owner_status == "unknown"
        else (
            owner.model_copy(
                update={
                    "phase": core.AttemptPhase.PARKED,
                    "closure": owner.closure.model_copy(update={"disposition": "park"}),
                }
            ),
        )
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=owners),
            "settlement": state.settlement.model_copy(update={"pending": ()}),
        }
    )
    decision = settlement_decision(state, value)
    result = core.step(
        reload(state), core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
    )
    feedback = result.state.run.receipts[0].feedback
    assert isinstance(feedback, core.Rejected)
    expected = (
        core.RejectionCode.OWNERSHIP
        if owner_status == "unknown"
        else core.RejectionCode.CLOSED_SCOPE
    )
    assert feedback.code == expected
    assert result.events == (feedback,)
    assert result.requests == ()
    assert result.state.settlement == state.settlement


def prerequisite_receipt(completion: core.CompletionStatus | None = None) -> core.DecisionReceipt:
    identity = core.DecisionId(root="prerequisite")
    return core.DecisionReceipt(
        decision_id=identity,
        payload_digest="prerequisite",
        feedback=core.Accepted(decision_id=identity),
        completion=completion,
    )


@pytest.mark.parametrize("completed", [False, True])
def test_settle_dependency_requires_semantic_completion_before_acknowledgement(
    *, completed: bool
) -> None:
    value = settlement()
    state = pending_state(value)
    prerequisite = prerequisite_receipt(core.CompletionStatus.SUCCEEDED if completed else None)
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": (prerequisite,)}),
            "settlement": state.settlement.model_copy(update={"pending": ()}),
        }
    )
    decision = settlement_decision(state, value).model_copy(
        update={"depends_on": (prerequisite.decision_id,)}
    )
    result = core.step(
        reload(state), core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
    )
    receipt = next(
        row for row in result.state.run.receipts if row.decision_id == decision.decision_id
    )
    if completed:
        assert isinstance(receipt.feedback, core.Accepted)
        assert receipt.completion == core.CompletionStatus.SUCCEEDED
        assert len(result.state.settlement.settlements) == 1
    else:
        assert isinstance(receipt.feedback, core.Rejected)
        assert receipt.feedback.code == core.RejectionCode.DEPENDENCY
        assert receipt.feedback.path == ("depends_on",)
        assert receipt.feedback.retry_after == core.DependencyRef(
            decision_id=prerequisite.decision_id
        )
        assert result.state.settlement == state.settlement
        assert result.events == (receipt.feedback,)
    assert result.requests == ()


def test_existing_pending_settlement_cannot_complete_an_unresolved_decision_dependency() -> None:
    value = settlement().model_copy(
        update={"settlement_id": core.SettlementId(root="settlement:settle")}
    )
    state = pending_state(value)
    prerequisite = prerequisite_receipt()
    decision = settlement_decision(state, value).model_copy(
        update={"depends_on": (prerequisite.decision_id,)}
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest="settle",
        feedback=core.Accepted(decision_id=decision.decision_id),
    )
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"receipts": (prerequisite, receipt)})}
    )
    result = core.step(reload(state), released(value))
    assert result.state.settlement.pending == (value,)
    assert result.state.settlement.settlements == ()
    assert result.state.run.receipts[1].completion is None
    assert result.events == result.requests == ()
    state = result.state.model_copy(
        update={
            "run": result.state.run.model_copy(
                update={
                    "receipts": (
                        prerequisite.model_copy(
                            update={"completion": core.CompletionStatus.SUCCEEDED}
                        ),
                        receipt,
                    ),
                }
            )
        }
    )
    final = core.step(reload(state), released(value))
    assert final.state.settlement.settlements == (value,)
    assert final.state.run.receipts[1].completion == core.CompletionStatus.SUCCEEDED
    assert final.events == (core.AttemptSettled(settlement=value),)


@given(field=st.sampled_from(["candidate", "assessments"]))
def test_canonical_settlement_event_cannot_rewrite_the_accepted_proposal(field: str) -> None:
    value = settlement("failed", "wip").model_copy(
        update={"settlement_id": core.SettlementId(root="settlement:settle")}
    )
    state = pending_state(value)
    decision = settlement_decision(state, value)
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest="settle",
        feedback=core.Accepted(decision_id=decision.decision_id),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": (receipt,)}),
            "settlement": state.settlement.model_copy(update={"pending": ()}),
        }
    )
    replacement = (
        {"candidate": state.run.facts.baseline}
        if field == "candidate"
        else {
            "assessments": (
                core.AssessmentProposal(
                    kind=core.AssessmentKind.CORRECTNESS,
                    verdict="rejected",
                    sources=(),
                    candidate=value.candidate,
                    schema_version=1,
                ),
            )
        }
    )
    corrupted = value.model_copy(update=replacement)
    before = reload(state)
    with pytest.raises(core.ContractValidationError, match=field):
        core.step(state, core.AssessmentSubmitted(settlement=corrupted))
    assert state == before


@given(
    mode=st.sampled_from(["failed", "cancelled", "succeeded", "rejected", "failed-prerequisite"])
)
def test_recovered_pending_settlement_cannot_fall_back_from_its_terminal_decision(
    mode: str,
) -> None:
    value = settlement().model_copy(
        update={"settlement_id": core.SettlementId(root="settlement:settle")}
    )
    state = pending_state(value)
    decision = settlement_decision(state, value)
    prerequisite = prerequisite_receipt(core.CompletionStatus.FAILED)
    if mode == "failed-prerequisite":
        decision = decision.model_copy(update={"depends_on": (prerequisite.decision_id,)})
    statuses = {
        "failed": core.CompletionStatus.FAILED,
        "cancelled": core.CompletionStatus.CANCELLED,
        "succeeded": core.CompletionStatus.SUCCEEDED,
    }
    feedback: core.DecisionFeedback = core.Accepted(decision_id=decision.decision_id)
    if mode == "rejected":
        feedback = core.Rejected(
            decision_id=decision.decision_id,
            code=core.RejectionCode.CLOSED_SCOPE,
            path=("scope",),
            detail="decision no longer accepted",
        )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest="settle",
        feedback=feedback,
        completion=statuses.get(mode),
    )
    receipts = (prerequisite, receipt) if mode == "failed-prerequisite" else (receipt,)
    state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": receipts})})
    for event in (released(value), core.AttemptSettled(settlement=value)):
        result = core.step(state, event)
        assert result == core.step(reload(state), event)
        assert result.state.settlement == state.settlement
        assert result.state.run.receipts == receipts
        assert result.events == result.requests == ()


@given(corrupt=st.sampled_from(["none", "invocation", "digest"]))
def test_judge_checkpoint_authority_requires_exact_invocation_and_revision(corrupt: str) -> None:
    state, value = judge_state()
    invocation = state.sessions.invocations[0]
    invocation = invocation.model_copy(
        update={"turn": invocation.turn.model_copy(update={"workspace": invocation.scope})}
    )
    intent = state.intents.intents[0]
    assert isinstance(intent.request, core.DispatchTurn)
    intent = intent.model_copy(
        update={"request": intent.request.model_copy(update={"turn": invocation.turn})}
    )
    assert value.candidate is not None
    source = invocation.invocation
    revision = value.candidate
    if corrupt == "invocation":
        source = source.model_copy(
            update={"invocation_id": core.InvocationId(root="another-judge")}
        )
    if corrupt == "digest":
        revision = revision.model_copy(update={"digest": "another-digest"})
    checkpoint = core.AttemptCheckpoint(
        invocation=source,
        request_id=core.RequestId(root="judge-checkpoint"),
        revision=revision,
        retention="candidate",
    )
    owner = state.attempts.attempts[0]
    # Keep independent retention proof so only invocation attribution varies.
    owner = owner.model_copy(update={"checkpoints": (*owner.checkpoints, checkpoint)})
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "sessions": core.SessionsState(invocations=(invocation,)),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    )
    result = core.step(state, released(value))
    assert result == core.step(reload(state), released(value))
    final = result.state.settlement.settlements[0]
    assert final.eligible == (corrupt == "none")
    assert final.retention == "candidate"
    assert result.events == (core.AttemptSettled(settlement=final),)
