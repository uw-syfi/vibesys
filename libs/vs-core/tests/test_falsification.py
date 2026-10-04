"""Section 6 executable falsification scenarios, gated by typed wave-1 failures."""

from __future__ import annotations

from typing import Literal

import pytest

from vs_core.api import (
    Accepted,
    Access,
    Area,
    ArtifactId,
    ArtifactRef,
    AssessmentKind,
    AssessmentProposal,
    AssessmentSubmitted,
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptRef,
    AttemptsState,
    AttemptView,
    BlockIntent,
    Cancel,
    Capabilities,
    CloseAttemptScope,
    CloseSession,
    CoreEvent,
    CoreState,
    DecisionId,
    DecisionReceipt,
    DecisionSubmitted,
    DiscardWorkspace,
    EnsureWorkspace,
    EvaluationState,
    EventId,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    EvidenceRequirement,
    EvidenceRequirements,
    ExecuteRegisteredOperation,
    InspectRequest,
    IntentPhase,
    IntentsChange,
    Invocation,
    InvocationId,
    InvocationRef,
    ItemId,
    KernelNotImplementedError,
    LifecycleClass,
    MeasurementPlan,
    MeasurementStage,
    Observation,
    ObservationStatus,
    OperationDescriptor,
    OperationId,
    OperationSchemaRef,
    OperationWire,
    OwnedJob,
    ReconciliationDeadline,
    RecoveryStarted,
    ReducerTrace,
    Request,
    RequestId,
    RequestObserved,
    RequestPrepared,
    ResourceId,
    RestoreRevision,
    RetainRevision,
    RevisionId,
    RevisionRef,
    RoleId,
    SchemaRef,
    Scope,
    SessionId,
    SessionObserved,
    SessionPhase,
    SessionSpec,
    SessionsState,
    Settlement,
    SettlementId,
    Slot,
    SnapshotAndRetain,
    StartAttempt,
    TraceFrame,
    Transition,
    TurnSpec,
    Withdraw,
    WorkspaceMode,
    WorkspaceObserved,
    WorkspacePlan,
    initial_state,
    step,
    trace_step,
)

from .proof_digest import value_digest


def lane_step(state: CoreState, event: CoreEvent, area: Area) -> Transition:
    """Only the declared owning-lane rejection may satisfy a strict xfail."""
    try:
        return step(state, event)
    except KernelNotImplementedError as error:
        if error.area != area:
            raise WrongLaneError(area, error.area) from error
        raise


def candidate(number: int) -> RevisionRef:
    return RevisionRef(
        revision_id=RevisionId(root=f"candidate:{number}"), digest=f"digest:{number}"
    )


def cleanup_result(
    request: Request, observation: Observation, retained: RevisionRef | None
) -> CoreEvent:
    """A workspace acknowledgement carries the actual immutable result identity."""
    if isinstance(request, SnapshotAndRetain):
        assert retained is not None
        return WorkspaceObserved(
            attempt=request.attempt, observation=observation, revision=retained
        )
    if isinstance(request, RetainRevision | RestoreRevision):
        return WorkspaceObserved(
            attempt=request.attempt, observation=observation, revision=request.revision
        )
    if isinstance(request, EnsureWorkspace):
        return WorkspaceObserved(
            attempt=request.attempt, observation=observation, revision=request.plan.base
        )
    if isinstance(request, DiscardWorkspace | CloseAttemptScope):
        return WorkspaceObserved(attempt=request.attempt, observation=observation)
    if isinstance(request, CloseSession):
        return SessionObserved(session_id=request.session_id, observation=observation)
    raise AssertionError(type(request))


def acknowledge_cleanup(result: Transition, retained: RevisionRef | None) -> CoreState:
    """Drain requested retention and release acknowledgements before finality."""
    state = result.state
    pending = result.requests
    for batch in range(20):
        if not pending:
            return state
        following = []
        for index, request in enumerate(pending):
            assert request.request_id is not None
            observed = RequestObserved(
                observation=Observation(
                    event_id=EventId(root=f"ack:{batch}:{index}:{request.request_id.root}"),
                    request_id=request.request_id,
                    scope=request.scope,
                    sequence=1,
                    observed_at=20.0,
                    status=ObservationStatus.SUCCEEDED,
                    accepted=True,
                    terminal=True,
                    # An acknowledgement belongs to its request's episode.
                    admission_id=request.admission_id,
                    released=isinstance(
                        request, DiscardWorkspace | CloseAttemptScope | CloseSession
                    ),
                )
            )
            acknowledged = step(state, observed)
            state = acknowledged.state
            following.extend(acknowledged.requests)
            semantic = step(state, cleanup_result(request, observed.observation, retained))
            state = semantic.state
            following.extend(semantic.requests)
        pending = tuple(following)
    assert not pending, "cleanup request graph failed to terminate"
    return state


@pytest.mark.xfail(
    strict=True,
    raises=KernelNotImplementedError,
    reason="Intents A stub: request_observed (Attempts B retention is implemented)",
)
def test_settle_preserves_normal_finality_wip_and_evidence_eligibility() -> None:
    state = initial_state()
    scope = Scope(owner=state.run.run_id, generation=0)
    judge = SessionSpec(
        session_id=SessionId(root="judge"),
        role_id=RoleId(root="judge"),
        policy="reuse",
        lifetime="owner",
        access=Access.READ_ONLY,
    )
    judge_ref = InvocationRef(
        session_id=judge.session_id, invocation_id=InvocationId(root="issue-success"), generation=0
    )
    judge_turn = TurnSpec(
        session=judge,
        invocation_id=judge_ref.invocation_id,
        workspace=scope,
        prompts=(),
        output_schema=SchemaRef(name="judge", version=1),
        deadline_at=100.0,
        charge_class="free",
    )
    evidence_scope = Scope(owner=AttemptId(root="attempt:3"), generation=0)
    evidence = EvidenceRef(
        kind=EvidenceKind.CORRECTNESS,
        purpose="official",
        scope=evidence_scope,
        source_request=RequestId(root="measurement"),
        evidence_id=EvidenceId(root="trusted"),
        candidate=candidate(3),
        observation_sequence=1,
        evaluator_digest=state.run.facts.evaluator_digest,
        workload_digest=state.run.facts.workload_digest,
        environment_digest=state.run.facts.environment_digest,
        provenance="trusted",
        status=ObservationStatus.SUCCEEDED,
    )
    job = OwnedJob(
        resource_id=ResourceId(root="measurement"),
        submission_id=evidence.source_request,
        scope=evidence_scope,
        plan=MeasurementPlan(
            purpose=evidence.purpose,
            candidate=evidence.candidate,
            evaluator_digest=evidence.evaluator_digest,
            workload_digest=evidence.workload_digest,
            environment_digest=evidence.environment_digest,
            stages=(MeasurementStage(stage_id="correctness", execution_budget=10.0),),
            policy="ordered",
            recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
            submitted_at=0.0,
            queue_allowance=0.0,
            deadline_at=10.0,
        ),
        status=ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
        observation=Observation(
            event_id=EventId(root="measurement-result"),
            request_id=evidence.source_request,
            scope=evidence_scope,
            sequence=evidence.observation_sequence,
            observed_at=2.0,
            status=ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=True,
            resource_id=ResourceId(root="measurement"),
        ),
        evidence=(evidence,),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "requirements": EvidenceRequirements(
                        required_assessments=(AssessmentKind.CORRECTNESS,),
                        required_evidence=(
                            EvidenceRequirement(
                                kind=evidence.kind,
                                provenance=evidence.provenance,
                                purpose=evidence.purpose,
                            ),
                        ),
                    )
                }
            ),
            "sessions": SessionsState(
                invocations=(
                    Invocation(
                        invocation=judge_ref,
                        scope=scope,
                        turn=judge_turn,
                        phase=SessionPhase.TERMINAL,
                        output_schema=judge_turn.output_schema,
                        output_json='{"verdict":"satisfied"}',
                    ),
                )
            ),
            "evaluation": EvaluationState(jobs=(job,), evidence=(evidence,)),
        }
    )
    cases: tuple[
        tuple[
            Literal["succeeded", "failed", "cancelled", "blocked"],
            Literal["discard", "wip", "candidate"],
            bool,
            tuple[AssessmentProposal, ...],
        ],
        ...,
    ] = (
        ("failed", "wip", False, ()),
        (
            "succeeded",
            "discard",
            False,
            (
                AssessmentProposal(
                    kind=AssessmentKind.CORRECTNESS,
                    verdict="satisfied",
                    sources=(
                        InvocationRef(
                            session_id=SessionId(root="judge"),
                            invocation_id=InvocationId(root="issue-success"),
                            generation=0,
                        ),
                    ),
                    candidate=None,
                    schema_version=1,
                ),
            ),
        ),
        (
            "failed",
            "discard",
            False,
            (
                AssessmentProposal(
                    kind=AssessmentKind.CORRECTNESS,
                    verdict="rejected",
                    sources=(),
                    candidate=None,
                    schema_version=1,
                ),
            ),
        ),
        (
            "succeeded",
            "candidate",
            True,
            (
                AssessmentProposal(
                    kind=AssessmentKind.CORRECTNESS,
                    verdict="satisfied",
                    sources=(EvidenceId(root="trusted"),),
                    candidate=candidate(3),
                    schema_version=1,
                ),
            ),
        ),
    )
    for index, (outcome, retention, eligible, assessments) in enumerate(cases):
        proposal = Settlement(
            settlement_id=SettlementId(root=f"settlement:{index}"),
            attempt=AttemptRef(attempt_id=AttemptId(root=f"attempt:{index}"), generation=0),
            candidate=candidate(index) if retention != "discard" else None,
            assessments=assessments,
            eligible=eligible,
            retention=retention,
            outcome=outcome,
        )
        owned = AttemptView(
            attempt_id=proposal.attempt.attempt_id,
            item_id=ItemId(root=f"item:{index}"),
            generation=0,
            phase=AttemptPhase.ACTIVE,
            admission_id=DecisionId(root=f"admit:{index}"),
            workspace=WorkspacePlan(
                mode=WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
            ),
            budget=AttemptBudget(),
        )
        start = StartAttempt(
            decision_id=DecisionId(root=f"admit:{index}"),
            scope=Scope(owner=state.run.run_id, generation=0),
            attempt_id=owned.attempt_id,
            item_id=owned.item_id,
            workspace=owned.workspace,
            budget=owned.budget,
        )
        start_receipt = DecisionReceipt(
            decision_id=start.decision_id,
            decision=start,
            payload_digest=value_digest(start),
            feedback=Accepted(decision_id=start.decision_id),
        )
        state = state.model_copy(
            update={
                # Retirement closes an episode only on its accepted start.
                "run": state.run.model_copy(
                    update={"receipts": (*state.run.receipts, start_receipt)}
                ),
                "attempts": AttemptsState(attempts=(*state.attempts.attempts, owned)),
                "scheduling": state.scheduling.model_copy(
                    update={
                        "slots": (
                            Slot(
                                attempt=proposal.attempt,
                                admission_id=DecisionId(root=f"admit:{index}"),
                                admitted_at=0.0,
                            ),
                        )
                    }
                ),
            }
        )
        initial_owned = state
        event = AssessmentSubmitted(settlement=proposal)
        result = lane_step(state, event, Area.ATTEMPTS)
        assert (
            not result.state.settlement.settlements
            or proposal not in result.state.settlement.settlements
        )
        # Cancellation arriving after assessment but before cleanup cannot erase finality.
        cancellation = Withdraw(
            decision_id=DecisionId(root=f"closing-cancel:{index}"),
            scope=Scope(owner=state.run.run_id, generation=0),
            target=proposal.attempt,
            disposition=Cancel(),
        )
        cancelled = step(
            result.state,
            DecisionSubmitted(decision=cancellation, expected_revision=result.state.revision),
        )
        state = acknowledge_cleanup(
            Transition(state=cancelled.state, requests=(*result.requests, *cancelled.requests)),
            proposal.candidate,
        )
        replay = step(state, event)
        assert (
            len(
                [
                    value
                    for value in state.settlement.settlements
                    if value.settlement_id == proposal.settlement_id
                ]
            )
            == 1
        )
        assert replay.events == replay.requests == ()
        assert not state.scheduling.slots
        retained = state.settlement.settlements[-1]
        assert retained == proposal
        final_owner = next(
            owner for owner in state.attempts.attempts if owner.attempt_id == owned.attempt_id
        )
        assert final_owner.phase == AttemptPhase.TERMINAL
        assert not final_owner.pending_intents
        assert not final_owner.release_dependencies
        if retention != "discard":
            assert any(
                checkpoint.revision == proposal.candidate and checkpoint.retention == retention
                for checkpoint in final_owner.checkpoints
            )
        assert state.evaluation.evidence == (evidence,)
        cancel = Withdraw(
            decision_id=DecisionId(root=f"late-cancel:{index}"),
            scope=Scope(owner=state.run.run_id, generation=0),
            target=proposal.attempt,
            disposition=Cancel(),
        )
        cancelled = step(
            state, DecisionSubmitted(decision=cancel, expected_revision=state.revision)
        )
        assert cancelled.state.settlement.settlements == state.settlement.settlements
        early_cancel = cancellation.model_copy(
            update={"decision_id": DecisionId(root=f"early-cancel:{index}")}
        )
        early = step(
            initial_owned,
            DecisionSubmitted(decision=early_cancel, expected_revision=initial_owned.revision),
        )
        retired = acknowledge_cleanup(early, proposal.candidate)
        cancelled_settlements = tuple(
            value for value in retired.settlement.settlements if value.attempt == proposal.attempt
        )
        assert len(cancelled_settlements) == 1
        assert cancelled_settlements[0].outcome == "cancelled"
        assert not cancelled_settlements[0].eligible
        late_settle = step(retired, event)
        assert late_settle.state.settlement.settlements == retired.settlement.settlements
    assert sum(value.eligible for value in state.settlement.settlements) == 1


@pytest.mark.xfail(
    strict=True,
    raises=KernelNotImplementedError,
    reason="needs Intents A observation composition",
)
def test_lost_write_acceptance_cannot_blindly_redispatch_after_restart() -> None:
    state = initial_state()
    schema = OperationSchemaRef(
        kind="project.artifact.put",
        request_schema=SchemaRef(name="artifact-put", version=1),
        outcome_schema=SchemaRef(name="artifact-put-outcome", version=1),
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
    )
    request = ExecuteRegisteredOperation(
        request_id=RequestId(root="write"),
        scope=Scope(owner=state.run.run_id, generation=0),
        deadline_at=100.0,
        operation_id=OperationId(root="operation"),
        operation=OperationWire(
            schema_ref=schema, payload_json='{"content":"same immutable payload"}'
        ),
        retry_limit=1,
    )
    state = state.model_copy(
        update={
            "registry": (OperationDescriptor(**schema.model_dump(mode="python"), inspect=True),),
            "run": state.run.model_copy(
                update={
                    "capabilities": Capabilities(
                        operations=(
                            OperationDescriptor(**schema.model_dump(mode="python"), inspect=True),
                        )
                    )
                }
            ),
        }
    )
    clock = RequestPrepared(request=request, lifecycle=LifecycleClass.IDEMPOTENT_WRITE)
    prepared = trace_step(
        state,
        clock,
        ReducerTrace(
            frames=(
                TraceFrame(
                    signal=clock,
                    change=IntentsChange(state=state.intents, requests=(request,)),
                ),
            )
        ),
    ).state
    dispatched = prepared.intents.intents[0].model_copy(update={"phase": IntentPhase.DISPATCHED})
    prepared = prepared.model_copy(
        update={"intents": prepared.intents.model_copy(update={"intents": (dispatched,)})}
    )
    assert request.request_id is not None
    restarted = CoreState.model_validate_json(prepared.model_dump_json())
    result = lane_step(restarted, RecoveryStarted(epoch=1, now_at=10.0), Area.INTENTS)
    assert len(result.requests) == 1
    inspection = result.requests[0]
    assert isinstance(inspection, InspectRequest)
    assert inspection.target == request.request_id
    assert all(value.kind != "execute_registered_operation" for value in result.requests)
    assert inspection.request_id is not None
    observation = Observation(
        event_id=EventId(root="lost-acceptance"),
        request_id=inspection.request_id,
        scope=inspection.scope,
        sequence=1,
        observed_at=20.0,
        status=ObservationStatus.UNKNOWN,
    )
    unknown = step(result.state, RequestObserved(observation=observation))
    blocked = step(
        unknown.state, ReconciliationDeadline(request_id=request.request_id, now_at=100.0)
    )
    assert any(isinstance(value, BlockIntent) for value in blocked.requests)
    assert all(value.kind != "execute_registered_operation" for value in blocked.requests)
    assert blocked.state.intents.intents[0].phase == IntentPhase.BLOCKED


class WrongLaneError(AssertionError):
    """A different pending area cannot satisfy another lane's xfail gate."""

    def __init__(self, expected: Area, actual: Area) -> None:
        super().__init__(f"expected {expected}, got {actual}")
