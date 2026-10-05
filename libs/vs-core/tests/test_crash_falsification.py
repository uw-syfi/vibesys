"""Section 6 executable falsification scenarios, gated by typed wave-1 failures."""

from __future__ import annotations

from enum import StrEnum

import vs_core.api as core
from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    Access,
    Area,
    ArtifactId,
    ArtifactRef,
    Continuation,
    ContinuationId,
    ContinuationPhase,
    CoreEvent,
    CoreState,
    DeadlineReached,
    DecisionId,
    DecisionSubmitted,
    DispatchTurn,
    EvaluationState,
    EventCursor,
    EventId,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    HostFence,
    HostId,
    InvocationId,
    InvocationRef,
    JobObserved,
    KernelNotImplementedError,
    MeasurementPlan,
    MeasurementStage,
    Observation,
    ObservationStatus,
    OwnedJob,
    RequestId,
    RequestTurn,
    ResourceId,
    ResumeSessionTurn,
    RoleId,
    RunEnvelope,
    SchemaRef,
    Scope,
    SessionId,
    SessionPhase,
    SessionSpec,
    SessionsState,
    SessionView,
    StrategyState,
    Transition,
    TurnObserved,
    TurnResult,
    TurnSpec,
    initial_state,
    step,
)

from .test_recovery_proof_regressions import _digest


def lane_step(state: CoreState, event: CoreEvent, area: Area) -> Transition:
    """The strict xfail must identify the expected first owning reducer."""
    try:
        return step(state, event)
    except KernelNotImplementedError as error:
        if error.area != area:
            raise AssertionError from error
        raise


class CrashBoundary(StrEnum):
    BEFORE_COMMIT = "before-commit"
    AFTER_COMMIT = "after-commit"
    AFTER_ACCEPTANCE = "after-acceptance"
    AFTER_REPLY = "after-reply"


class CallbackState(StrategyState):
    """Persisted strategy facts, committed atomically with core and delivery cursor."""

    resumes: tuple[ContinuationId, ...] = ()
    completions: tuple[InvocationId, ...] = ()


def suspended_run() -> tuple[CoreState, TurnSpec, JobObserved]:
    """Ownership and paid work already exist before the crash window starts."""
    state = initial_state()
    scope = Scope(owner=state.run.run_id, generation=0)
    spec = SessionSpec(
        session_id=SessionId(root="session"),
        role_id=RoleId(root="worker"),
        policy="reuse",
        lifetime="owner",
        access=Access.READ_ONLY,
    )
    invocation = InvocationRef(
        session_id=spec.session_id, invocation_id=InvocationId(root="original"), generation=0
    )
    next_ref = invocation.model_copy(update={"invocation_id": InvocationId(root="canonical-next")})
    original_turn = TurnSpec(
        session=spec,
        invocation_id=invocation.invocation_id,
        workspace=scope,
        prompts=(),
        output_schema=SchemaRef(name="result", version=1),
        deadline_at=100.0,
        charge_class="free",
    )
    turn = TurnSpec(
        session=spec,
        invocation_id=next_ref.invocation_id,
        continuation_id=ContinuationId(root="continuation"),
        workspace=scope,
        prompts=(),
        output_schema=SchemaRef(name="result", version=1),
        deadline_at=100.0,
        charge_class="resume",
    )
    resource = ResourceId(root="owned-job")
    plan = MeasurementPlan(
        purpose="official",
        candidate=state.run.facts.baseline,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        stages=(MeasurementStage(stage_id="measure", execution_budget=10.0),),
        policy="ordered",
        recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
        submitted_at=0.0,
        queue_allowance=0.0,
        deadline_at=10.0,
    )
    # The paid measurement was authorized by a real accepted Measure decision.
    measured = step(
        state.model_copy(
            update={
                "intents": core.IntentsState(
                    recovery=core.RecoveryBarrier(phase=core.RecoveryPhase.READY)
                )
            }
        ),
        DecisionSubmitted(
            decision=core.Measure(
                decision_id=DecisionId(root="measure-decision"), scope=scope, plan=plan
            ),
            expected_revision=0,
        ),
    )
    (submission,) = measured.requests
    assert isinstance(submission, core.SubmitMeasurement)
    job_request = submission.request_id
    assert job_request is not None
    evidence = EvidenceRef(
        kind=EvidenceKind.CORRECTNESS,
        purpose="official",
        scope=scope,
        source_request=job_request,
        evidence_id=EvidenceId(root="original-evidence"),
        candidate=state.run.facts.baseline,
        observation_sequence=1,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        provenance="trusted",
        status=ObservationStatus.SUCCEEDED,
    )
    yielded = Observation(
        event_id=EventId(root="original-yielded"),
        request_id=RequestId(root="original-dispatch"),
        scope=scope,
        sequence=1,
        observed_at=1.0,
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
        resource_id=ResourceId(root="conversation"),
    )
    dispatch = DispatchTurn(
        request_id=yielded.request_id, scope=scope, deadline_at=100.0, turn=original_turn
    )
    job = OwnedJob(
        resource_id=resource,
        scope=scope,
        plan=plan,
        status=ObservationStatus.PENDING,
        submission_id=job_request,
    )
    assert turn.continuation_id is not None
    continuation = Continuation(
        continuation_id=turn.continuation_id,
        invocation=invocation,
        next_invocation=next_ref,
        jobs=(resource,),
        deadline_at=10.0,
        phase=ContinuationPhase.WAITING,
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": measured.state.run.receipts}),
            "sessions": SessionsState(
                invocations=(
                    core.Invocation(
                        invocation=invocation,
                        scope=scope,
                        turn=original_turn,
                        phase=SessionPhase.SUSPENDED,
                        observation=yielded,
                    ),
                ),
                inputs=(
                    core.InputRecord(
                        input=core.SessionInput(
                            input_id=core.InputId(root="reserved-input"),
                            target=core.ScopeInputTarget(scope=scope),
                            artifact=ArtifactRef(
                                artifact_id=ArtifactId(root="reserved-input"), digest="input"
                            ),
                            received_at=0.0,
                            sequence=0,
                        ),
                        reserved_to=invocation,
                    ),
                ),
                sessions=(
                    SessionView(
                        spec=spec,
                        scope=scope,
                        generation=0,
                        phase=SessionPhase.SUSPENDED,
                        invocation=invocation.invocation_id,
                        accepted=True,
                        resource_id=yielded.resource_id,
                        continuation_id=continuation.continuation_id,
                    ),
                ),
            ),
            "intents": core.IntentsState(
                recovery=core.RecoveryBarrier(phase=core.RecoveryPhase.READY),
                intents=(
                    core.Intent(
                        request_id=yielded.request_id,
                        request=dispatch,
                        payload_digest=_digest(dispatch),
                        lifecycle=core.LifecycleClass.SESSION_TURN,
                        phase=core.IntentPhase.COMPLETED,
                        sequence=yielded.sequence,
                        observation=yielded,
                        reconcile_deadline_at=100.0,
                    ),
                    core.Intent(
                        request_id=job_request,
                        request=submission,
                        payload_digest=_digest(submission),
                        lifecycle=core.LifecycleClass.OWNED_JOB,
                        phase=core.IntentPhase.DISPATCHED,
                        reconcile_deadline_at=10.0,
                    ),
                ),
            ),
            "evaluation": EvaluationState(
                jobs=(job,),
                continuations=(continuation,),
                submission_budgets=measured.state.evaluation.submission_budgets,
            ),
            "attempts": core.AttemptsState(
                attempts=(
                    core.AttemptView(
                        attempt_id=core.AttemptId(root="paid-owner"),
                        item_id=core.ItemId(root="paid-item"),
                        generation=0,
                        phase=core.AttemptPhase.ACTIVE,
                        workspace=core.WorkspacePlan(
                            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
                        ),
                        budget=core.AttemptBudget(),
                        charges=(
                            core.ChargeReceipt(
                                charge_id=core.ChargeId(root="admission"),
                                kind=core.ChargeKind.ADMISSION,
                                charged=1,
                            ),
                        ),
                    ),
                )
            ),
        }
    )
    observed = JobObserved(
        resource_id=resource,
        observation=Observation(
            event_id=EventId(root="job-completed"),
            request_id=job_request,
            scope=scope,
            sequence=1,
            observed_at=9.0,
            status=ObservationStatus.SUCCEEDED,
            resource_id=resource,
            accepted=True,
            terminal=True,
            released=True,
        ),
        evidence=(evidence,),
        evaluation_result=core.EvaluationTerminalFacts(
            stages=(
                core.EvaluationStageResult(
                    stage_id="measure", outcome=core.EvaluationStageOutcome.PASSED
                ),
            ),
            accuracy_passed=True,
        ),
    )
    return state, turn, observed


def callback_commit(
    envelope: RunEnvelope[CallbackState], result: Transition
) -> RunEnvelope[CallbackState]:
    """Model the shell's atomic state plus callback transaction, without I/O."""
    callback = envelope.strategy
    for event in result.events:
        if event.kind == "resume_authorized":
            assert event.continuation_id not in callback.resumes
            callback = callback.model_copy(
                update={"resumes": (*callback.resumes, event.continuation_id)}
            )
        elif isinstance(event, TurnResult):
            assert event.invocation.invocation_id not in callback.completions
            callback = callback.model_copy(
                update={"completions": (*callback.completions, event.invocation.invocation_id)}
            )
    return envelope.model_copy(
        update={
            "core": result.state,
            "strategy": callback,
            "event_cursor": EventCursor(
                sequence=envelope.event_cursor.sequence + len(result.events)
            ),
        }
    )


def execute(
    initial: RunEnvelope[CallbackState],
    turn: TurnSpec,
    job: JobObserved,
    boundary: CrashBoundary | None,
    *,
    reverse: bool,
) -> tuple[RunEnvelope[CallbackState], tuple[InvocationId, ...]]:
    # Store bytes survive host restart; executor acceptance survives independently.
    assert turn.continuation_id is not None
    deadline = DeadlineReached(continuation_id=turn.continuation_id, now_at=10.0)
    durable = initial.model_dump_json()
    accepted: dict[InvocationId, str] = {}
    current = initial
    events = (deadline, job, job, deadline) if reverse else (job, deadline, deadline, job)
    for event in events:
        current = callback_commit(current, step(current.core, event))
        durable = current.model_dump_json()
    assert current.strategy.resumes == (turn.continuation_id,)
    decision = RequestTurn(
        decision_id=DecisionId(root="resume-decision"),
        scope=Scope(owner=current.core.run.run_id, generation=0),
        turn=turn,
    )
    submitted = DecisionSubmitted(decision=decision, expected_revision=current.revision)
    proposed = callback_commit(current, step(current.core, submitted))
    if boundary != CrashBoundary.BEFORE_COMMIT:
        durable = proposed.model_dump_json()
    if boundary in (None, CrashBoundary.AFTER_ACCEPTANCE, CrashBoundary.AFTER_REPLY):
        accepted[turn.invocation_id] = turn.model_dump_json()
    current = RunEnvelope[CallbackState].model_validate_json(durable)
    if boundary == CrashBoundary.BEFORE_COMMIT:
        current = callback_commit(current, step(current.core, submitted))
    else:
        replay = step(current.core, submitted)
        assert replay.requests == replay.events == ()
    # Replay the durable outbox; stable invocation identity makes acceptance idempotent.
    for intent in current.core.intents.intents:
        request = intent.request
        # Completed work was acknowledged before the crash window and is never replayed.
        if intent.phase != core.IntentPhase.COMPLETED and isinstance(
            request, DispatchTurn | ResumeSessionTurn
        ):
            payload = request.turn.model_dump_json()
            prior = accepted.setdefault(request.turn.invocation_id, payload)
            assert prior == payload
    assert tuple(accepted) == (turn.invocation_id,)
    dispatch = next(
        intent.request
        for intent in current.core.intents.intents
        if intent.request.kind in ("dispatch_turn", "resume_session_turn")
    )
    assert dispatch.request_id is not None
    completed = TurnObserved(
        invocation=InvocationRef(
            session_id=turn.session.session_id, invocation_id=turn.invocation_id, generation=0
        ),
        observation=Observation(
            event_id=EventId(root="resume-completed"),
            request_id=dispatch.request_id,
            scope=dispatch.scope,
            sequence=1,
            observed_at=11.0,
            status=ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=True,
        ),
    )
    replied = callback_commit(current, step(current.core, completed))
    if boundary == CrashBoundary.AFTER_REPLY:
        # A reply whose atomic commit was lost must be delivered on recovery.
        current = RunEnvelope[CallbackState].model_validate_json(current.model_dump_json())
        replied = callback_commit(current, step(current.core, completed))
    current = callback_commit(replied, step(replied.core, completed))
    return current, tuple(accepted)


def test_crash_boundaries_do_not_duplicate_resume_or_paid_work() -> None:
    original, turn, job = suspended_run()
    # Canonical source facts commit in the request ledger before the measurement
    # leaf consumes the job observation.
    ingress = core.step(
        original,
        core.RequestObserved(
            observation=job.observation,
            evidence=job.evidence,
            evaluation_result=job.evaluation_result,
        ),
    )
    (submitted,) = (
        row for row in ingress.state.intents.intents if row.request_id == job.observation.request_id
    )
    assert submitted.observation == job.observation
    assert submitted.phase == core.IntentPhase.COMPLETED
    initial = RunEnvelope[CallbackState](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        fence=HostFence(host_id=HostId(root="host"), epoch=1),
        strategy_id=original.run.declaration.strategy_id,
        state_schema=SchemaRef(name="callbacks", version=1),
        core=ingress.state,
        strategy=CallbackState(schema_version=1),
        event_cursor=EventCursor(sequence=0),
    )

    reference, accepted = execute(initial, turn, job, None, reverse=False)
    for boundary in CrashBoundary:
        for reverse in (False, True):
            result, restarted_acceptance = execute(initial, turn, job, boundary, reverse=reverse)
            assert result.strategy == reference.strategy
            assert result.event_cursor == reference.event_cursor
            assert restarted_acceptance == accepted
            assert (
                core.project(result.core).scheduling.charged
                == core.project(reference.core).scheduling.charged
                == 1
            )
            assert (
                core.project(result.core).scheduling.refunded
                == core.project(reference.core).scheduling.refunded
                == 0
            )
            assert {item.evidence_id for item in result.core.evaluation.evidence} == {
                EvidenceId(root="original-evidence")
            }
            assert result.core.sessions.inputs == reference.core.sessions.inputs
