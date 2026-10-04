"""Continuation authorization uses durable yield, checkpoint and job proofs."""

from itertools import product
from typing import ClassVar, Literal

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core


class TurnOutcome(core.Value):
    """Minimal owning-library output for a registered turn."""

    complete: bool = True


class RegisteredTurn(core.OperationRequest):
    """Registered turns carry their immutable normalized turn payload."""

    kind: Literal["test.continuation-turn"] = "test.continuation-turn"
    lifecycle: Literal[core.LifecycleClass.SESSION_TURN] = core.LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[BaseModel]] = TurnOutcome
    turn: core.TurnSpec


def normalize_turn(request: core.OperationRequest) -> core.TurnSpec:
    assert isinstance(request, RegisteredTurn)
    return request.turn


def normalize_reopen(request: core.OperationRequest) -> core.ScopeReopenNormalization:
    assert isinstance(request, core.ScopedAdmissionReopen)
    return core.ScopeReopenNormalization(
        attempt=request.attempt,
        continuation_id=request.continuation_id,
        park_authority=request.park_authority,
        resolved_cancelled_jobs=request.resolved_cancelled_jobs,
    )


def operation_codec() -> core.OperationRegistry:
    return core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=core.OperationDescriptor(
                    kind="evaluation.scope.reopen",
                    lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
                    request_schema=core.SchemaRef(name="reopen", version=1),
                    outcome_schema=core.SchemaRef(name="reopened", version=1),
                    inspect=True,
                    normalization=core.OperationNormalizationKind.SCOPE_REOPEN,
                ),
                request_model=core.ScopedAdmissionReopen,
                outcome_model=core.ScopedAdmissionReopenOutcome,
                normalize_scope_reopen=normalize_reopen,
            ),
            core.OperationRegistration(
                descriptor=core.OperationDescriptor(
                    kind="test.continuation-turn",
                    lifecycle=core.LifecycleClass.SESSION_TURN,
                    request_schema=core.SchemaRef(name="turn", version=1),
                    outcome_schema=core.SchemaRef(name="turn-output", version=1),
                    inspect=True,
                    cancel=True,
                    watch=True,
                ),
                request_model=RegisteredTurn,
                outcome_model=TurnOutcome,
                normalize_turn=normalize_turn,
            ),
        )
    )


def roundtrip(state: core.CoreState) -> core.CoreState:
    envelope = core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=1),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )
    codec = operation_codec()
    return codec.decode_envelope(
        core.RunEnvelope[core.StrategyState], codec.encode_envelope(envelope)
    ).core


def persisted_step(state: core.CoreState, event: core.CoreEvent) -> core.Transition:
    original = state.model_dump_json()
    result = core.step(state, event)
    assert result == core.step(roundtrip(state), event)
    assert state.model_dump_json() == original
    assert roundtrip(result.state) == result.state
    assert core.project(result.state).attempts == core.project(state).attempts
    assert core.project(result.state).scheduling.charged == core.project(state).scheduling.charged
    assert core.project(result.state).scheduling.refunded == core.project(state).scheduling.refunded
    assert result.state.evaluation.jobs == state.evaluation.jobs
    assert result.state.evaluation.registered_jobs == state.evaluation.registered_jobs
    assert result.state.evaluation.evidence == state.evaluation.evidence
    assert result.state.evaluation.submission_budgets == state.evaluation.submission_budgets
    assert result.state.scheduling == state.scheduling
    assert result.state.sessions == state.sessions
    assert result.state.settlement == state.settlement
    return result


def observation(
    scope: core.Scope,
    request: core.RequestId,
    resource: core.ResourceId | None = None,
    *,
    terminal: bool = True,
    status: core.ObservationStatus = core.ObservationStatus.SUCCEEDED,
) -> core.Observation:
    return core.Observation(
        event_id=core.EventId(root=f"observation-{request.root}"),
        request_id=request,
        scope=scope,
        sequence=1,
        observed_at=1.0,
        status=status,
        resource_id=resource,
        accepted=True,
        terminal=terminal,
    )


def with_wait(state: core.CoreState, continuation: core.Continuation) -> core.CoreState:
    evaluation = state.evaluation.model_copy(update={"continuations": (continuation,)})
    return state.model_copy(update={"evaluation": evaluation})


def fixture(
    *, registered: bool = False, run_owned: bool = False, settled: bool = False
) -> tuple[core.CoreState, core.Continuation]:
    state = core.initial_state()
    scope = core.Scope(
        owner=state.run.run_id if run_owned else core.AttemptId(root="attempt"), generation=0
    )
    admission = None if run_owned else core.DecisionId(root="original-episode")
    invocation = core.InvocationRef(
        session_id=core.SessionId(root="session"),
        invocation_id=core.InvocationId(root="yielded"),
        generation=0,
    )
    successor = invocation.model_copy(update={"invocation_id": core.InvocationId(root="resume")})
    spec = core.SessionSpec(
        session_id=invocation.session_id,
        role_id=core.RoleId(root="opaque-role"),
        policy="reuse",
        lifetime="owner",
        access=core.Access.READ_ONLY if run_owned else core.Access.WRITE_CANDIDATE,
    )
    turn = core.TurnSpec(
        session=spec,
        invocation_id=invocation.invocation_id,
        workspace=scope,
        prompts=(),
        output_schema=core.SchemaRef(name="output", version=1),
        deadline_at=100.0,
        charge_class="paid",
    )
    record = core.Invocation(
        invocation=invocation,
        scope=scope,
        turn=turn,
        phase=core.SessionPhase.SUSPENDED,
        observation=observation(scope, core.RequestId(root="turn")).model_copy(
            update={"admission_id": admission}
        ),
    )
    session = core.SessionView(
        spec=spec,
        scope=scope,
        generation=0,
        phase=core.SessionPhase.SUSPENDED,
        invocation=invocation.invocation_id,
        accepted=True,
        resource_id=core.ResourceId(root="session-lease"),
        continuation_id=core.ContinuationId(root="wait"),
    )
    attempt = core.AttemptView(
        attempt_id=core.AttemptId(root="attempt"),
        item_id=core.ItemId(root="item"),
        generation=0,
        phase=core.AttemptPhase.ACTIVE,
        admission_id=admission,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(paid_invocation_limit=4),
        checkpoints=(
            core.AttemptCheckpoint(
                invocation=invocation,
                request_id=core.RequestId(root="checkpoint"),
                revision=state.run.facts.baseline,
                retention="wip",
            ),
        ),
        sessions=(invocation.session_id,),
        charges=tuple(
            core.ChargeReceipt(
                charge_id=core.ChargeId(root=kind.value),
                kind=kind,
                charged=1,
                invocation_id=invocation.invocation_id,
            )
            for kind in core.ChargeKind
        ),
    )
    plan = core.MeasurementPlan(
        purpose="profile" if run_owned else "official",
        candidate=state.run.facts.baseline,
        evaluator_digest=state.run.facts.evaluator_digest,
        workload_digest=state.run.facts.workload_digest,
        environment_digest=state.run.facts.environment_digest,
        stages=(core.MeasurementStage(stage_id="measure", execution_budget=20.0),),
        policy="ordered",
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="recipe"),
        submitted_at=0.0,
        queue_allowance=0.0,
        deadline_at=20.0,
    )
    jobs = tuple(
        core.OwnedJob(
            resource_id=core.ResourceId(root=f"job-{index}"),
            submission_id=core.RequestId(root=f"submission-{index}"),
            scope=scope,
            plan=plan,
            status=core.ObservationStatus.SUCCEEDED if settled else core.ObservationStatus.PENDING,
            terminal=settled,
            observation=observation(
                scope,
                core.RequestId(root=f"submission-{index}"),
                core.ResourceId(root=f"job-{index}"),
                terminal=settled,
                status=core.ObservationStatus.SUCCEEDED
                if settled
                else core.ObservationStatus.PENDING,
            ),
        )
        for index in range(3)
    )
    custom = tuple(
        core.RegisteredOwnedJob(
            operation_id=core.OperationId(root=f"custom-{index}"),
            request_id=job.submission_id,
            scope=scope,
            resource_pool=core.PoolId(root="pool"),
            resource_id=job.resource_id,
            status=job.status,
            terminal=job.terminal,
            observation=job.observation,
        )
        for index, job in enumerate(jobs)
    )
    continuation = core.Continuation(
        continuation_id=core.ContinuationId(root="wait"),
        invocation=invocation,
        next_invocation=successor,
        jobs=tuple(job.resource_id for job in jobs),
        deadline_at=10.0,
        phase=core.ContinuationPhase.WAITING,
    )
    intent = core.Intent(
        request_id=core.RequestId(root="turn"),
        request=core.DispatchTurn(
            request_id=core.RequestId(root="turn"),
            scope=scope,
            deadline_at=100.0,
            admission_id=admission,
            turn=turn,
        ),
        payload_digest="canonical-turn",
        lifecycle=core.LifecycleClass.SESSION_TURN,
        phase=core.IntentPhase.COMPLETED,
        observation=record.observation,
        reconcile_deadline_at=100.0,
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=() if run_owned else (attempt,)),
            "sessions": core.SessionsState(sessions=(session,), invocations=(record,)),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
            "evaluation": core.EvaluationState(
                jobs=() if registered else jobs,
                registered_jobs=custom if registered else (),
            ),
        }
    )
    return state, continuation


@pytest.mark.parametrize(("registered", "run_owned"), list(product([False, True], repeat=2)))
def test_suspension_registers_owned_wait_and_replay_preserves_outbox(
    *, registered: bool, run_owned: bool
) -> None:
    state, continuation = fixture(registered=registered, run_owned=run_owned)
    result = persisted_step(state, core.TurnSuspended(continuation=continuation))
    assert result.state.evaluation.continuations == (continuation,)
    assert result.events == ()
    assert len(result.requests) == len(continuation.jobs)
    resources = []
    for request in result.requests:
        assert isinstance(request, core.ObserveOwnedJob)
        resources.append(request.resource_id)
    assert set(resources) == set(continuation.jobs)
    replay = persisted_step(result.state, core.TurnSuspended(continuation=continuation))
    assert replay.requests == ()
    assert replay.events == ()
    assert core.pending_requests(replay.state.intents) == core.pending_requests(
        result.state.intents
    )


@pytest.mark.parametrize(("registered", "run_owned"), list(product([False, True], repeat=2)))
def test_already_settled_wait_authorizes_exact_successor_once(
    *, registered: bool, run_owned: bool
) -> None:
    state, continuation = fixture(registered=registered, run_owned=run_owned, settled=True)
    result = persisted_step(state, core.TurnSuspended(continuation=continuation))
    assert result.requests == ()
    assert len(result.events) == 1
    feedback = result.events[0]
    assert isinstance(feedback, core.ResumeAuthorized)
    assert feedback.continuation_id == continuation.continuation_id
    assert feedback.next_invocation == continuation.next_invocation
    assert feedback.timeout is None
    assert result.state.evaluation.continuations[0].phase == core.ContinuationPhase.AUTHORIZED
    replay = persisted_step(result.state, core.TurnSuspended(continuation=continuation))
    assert replay.events == ()
    assert replay.requests == ()


@pytest.mark.parametrize("registered", [False, True])
@given(order=st.permutations((0, 1, 2)), duplicates=st.lists(st.integers(0, 2), max_size=10))
def test_wait_all_reordering_duplicate_and_stale_notifications_authorize_once(
    *, registered: bool, order: tuple[int, ...], duplicates: list[int]
) -> None:
    state, continuation = fixture(registered=registered)
    state = with_wait(state, continuation)
    authorizations = []
    for index in [*order, *duplicates]:
        field = "registered_jobs" if registered else "jobs"
        jobs = list(getattr(state.evaluation, field))
        job = jobs[index]
        assert job.observation is not None
        jobs[index] = job.model_copy(
            update={
                "status": core.ObservationStatus.SUCCEEDED,
                "terminal": True,
                "observation": job.observation.model_copy(
                    update={"status": core.ObservationStatus.SUCCEEDED, "terminal": True}
                ),
            }
        )
        # These are A-owned committed facts, supplied to B through its public wake signal.
        state = state.model_copy(
            update={"evaluation": state.evaluation.model_copy(update={field: tuple(jobs)})}
        )
        result = persisted_step(
            state,
            core.ContinuationJobsChanged(
                resource_id=continuation.jobs[index], observation_sequence=1
            ),
        )
        authorizations.extend(result.events)
        assert result.requests == ()
        assert len(authorizations) <= 1
        if not all(job.terminal for job in jobs):
            assert result.events == ()
        state = result.state
        stale = persisted_step(
            state,
            core.ContinuationJobsChanged(
                resource_id=continuation.jobs[index], observation_sequence=0
            ),
        )
        assert stale.events == ()
        assert stale.requests == ()
        state = stale.state
    assert len(authorizations) == 1
    assert isinstance(authorizations[0], core.ResumeAuthorized)


@pytest.mark.parametrize(
    ("charge", "predecessor"),
    list(product(["paid", "correction", "resume", "free"], [False, True])),
)
@given(
    accepted=st.booleans(),
    terminal=st.booleans(),
    status=st.sampled_from(list(core.ObservationStatus)),
)
def test_admission_requires_conclusive_accepted_yield_for_every_turn_variant(
    *,
    charge: str,
    predecessor: bool,
    accepted: bool,
    terminal: bool,
    status: core.ObservationStatus,
) -> None:
    state, continuation = fixture(settled=True)
    invocation = state.sessions.invocations[0]
    assert invocation.observation is not None
    turn = invocation.turn.model_copy(
        update={
            "charge_class": charge,
            "predecessor": continuation.invocation if predecessor else None,
        }
    )
    invocation = invocation.model_copy(
        update={
            "turn": turn,
            "observation": invocation.observation.model_copy(
                update={"accepted": accepted, "terminal": terminal, "status": status}
            ),
        }
    )
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"invocations": (invocation,)})}
    )
    intent = state.intents.intents[0]
    state = state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={
                    "intents": (
                        intent.model_copy(
                            update={
                                "request": intent.request.model_copy(update={"turn": turn}),
                                "observation": invocation.observation,
                            }
                        ),
                    )
                }
            )
        }
    )
    event = core.TurnSuspended(continuation=continuation)
    if status == core.ObservationStatus.UNKNOWN:
        result = persisted_step(state, event)
        assert result.events == ()
        assert result.state.evaluation.continuations == ()
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], core.InspectTurn)
    elif not accepted or not terminal or status != core.ObservationStatus.SUCCEEDED:
        with pytest.raises(core.ContractError):
            core.step(state, event)
        assert state.evaluation.continuations == ()
    else:
        result = persisted_step(state, event)
        assert len(result.events) == 1


@pytest.mark.parametrize("checkpoint", [None, "other-invocation", "no-invocation"])
def test_checkpoint_request_or_unrelated_retention_never_proves_yield(
    checkpoint: str | None,
) -> None:
    state, continuation = fixture(settled=True)
    owner = state.attempts.attempts[0]
    retained = owner.checkpoints[0]
    attributed = continuation.next_invocation if checkpoint == "other-invocation" else None
    owner = owner.model_copy(
        update={
            "checkpoints": ()
            if checkpoint is None
            else (retained.model_copy(update={"invocation": attributed}),),
            "pending_intents": (core.RequestId(root="checkpoint"),),
        }
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    with pytest.raises(core.ContractError, match="checkpoint"):
        core.step(state, core.TurnSuspended(continuation=continuation))
    assert state.evaluation.continuations == ()


@pytest.mark.parametrize(
    "phase",
    [
        core.ContinuationPhase.AUTHORIZED,
        core.ContinuationPhase.RESUMED,
        core.ContinuationPhase.CANCELLED,
    ],
)
@given(now=st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False))
def test_terminal_authorization_and_cancellation_ignore_repeated_deadlines(
    phase: core.ContinuationPhase, now: float
) -> None:
    state, continuation = fixture()
    continuation = continuation.model_copy(update={"phase": phase})
    state = with_wait(state, continuation)
    result = persisted_step(
        state, core.DeadlineReached(continuation_id=continuation.continuation_id, now_at=now)
    )
    assert result.state.evaluation.continuations == (continuation,)
    assert result.requests == ()
    assert result.events == ()


@given(now=st.floats(min_value=0, max_value=9.99, allow_nan=False, allow_infinity=False))
def test_early_deadline_never_authorizes_or_cancels(now: float) -> None:
    state, continuation = fixture()
    state = with_wait(state, continuation)
    result = persisted_step(
        state, core.DeadlineReached(continuation_id=continuation.continuation_id, now_at=now)
    )
    assert result.state.evaluation.continuations == (continuation,)
    assert result.events == ()
    assert result.requests == ()


@pytest.mark.parametrize("disposition", ["park", "cancel"])
def test_retirement_records_exact_park_authority_without_resume(
    disposition: Literal["park", "cancel"],
) -> None:
    state, continuation = fixture(settled=True)
    state = with_wait(state, continuation)
    authority = core.RequestId(root="park") if disposition == "park" else None
    if authority is not None:
        owner = state.attempts.attempts[0]
        assert owner.admission_id is not None
        owner = owner.model_copy(
            update={
                "phase": core.AttemptPhase.CLOSING,
                "closure": core.AttemptClosure(
                    disposition="park",
                    requested_at=1.0,
                    authority=authority,
                    admission_id=owner.admission_id,
                ),
            }
        )
        state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
        close = core.CloseAttemptScope(
            request_id=authority,
            scope=state.sessions.invocations[0].scope,
            deadline_at=100.0,
            admission_id=owner.admission_id,
            attempt=core.AttemptRef(attempt_id=owner.attempt_id, generation=0),
        )
        close_intent = core.Intent(
            request_id=authority,
            request=close,
            payload_digest="close",
            lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
            phase=core.IntentPhase.PREPARED,
            reconcile_deadline_at=100.0,
        )
        state = state.model_copy(
            update={
                "intents": state.intents.model_copy(
                    update={
                        "intents": (*state.intents.intents, close_intent),
                    }
                )
            }
        )
    event = core.ContinuationRetireRequested(
        continuation_id=continuation.continuation_id,
        disposition=disposition,
        park_authority=authority,
    )
    result = persisted_step(state, event)
    stored = result.state.evaluation.continuations[0]
    assert stored.phase == (
        core.ContinuationPhase.PARKED if disposition == "park" else core.ContinuationPhase.CANCELLED
    )
    assert stored.park_authority == authority
    assert result.events == ()
    assert result.requests == ()
    replay = persisted_step(result.state, event)
    assert replay.events == ()
    assert replay.requests == ()


def reopened_fixture() -> tuple[core.CoreState, core.Continuation, core.ContinuationScopeReopened]:
    state, continuation = fixture(settled=True)
    codec = operation_codec()
    scope = state.sessions.invocations[0].scope
    owner = state.attempts.attempts[0].model_copy(
        update={"admission_id": core.DecisionId(root="reentry")}
    )
    park_authority = core.RequestId(root="park")
    payload = core.ScopedAdmissionReopen(
        attempt=core.AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
        continuation_id=continuation.continuation_id,
        park_authority=park_authority,
        resolved_cancelled_jobs=(),
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="reopen"),
            scope=core.Scope(owner=state.run.run_id, generation=0),
            deadline_at=100.0,
            request=payload,
        )
    )
    request = core.ExecuteRegisteredOperation(
        request_id=core.RequestId(root="operation:reopen"),
        operation_id=core.OperationId(root="operation:reopen"),
        decision_id=decision.decision_id,
        scope=decision.scope,
        deadline_at=decision.deadline_at,
        operation=codec.encode(payload),
        retry_limit=state.run.limits.max_retries,
        admission_id=owner.admission_id,
    )
    assert request.request_id is not None
    proof = observation(decision.scope, request.request_id).model_copy(
        update={"admission_id": owner.admission_id}
    )
    outcome = core.ScopedAdmissionReopenOutcome(scope=scope, admission="reopened")
    intent = core.Intent.model_validate(
        {
            "request_id": request.request_id,
            "request": request,
            "payload_digest": "reopen-payload",
            "lifecycle": core.LifecycleClass.IDEMPOTENT_WRITE,
            "phase": core.IntentPhase.COMPLETED,
            "observation": proof,
            "outcome_schema": request.operation.schema_ref.outcome_schema,
            "outcome_json": codec.encode_outcome(request.operation.schema_ref, outcome),
            "reconcile_deadline_at": 100.0,
        },
        context={"operation_registry": codec},
    )
    continuation = continuation.model_copy(
        update={
            "phase": core.ContinuationPhase.REOPENING,
            "park_authority": park_authority,
            "reopen_authority": request.request_id,
        }
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest="reopen-decision",
        feedback=core.Accepted(decision_id=decision.decision_id),
        request_ids=(request.request_id,),
    )
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "receipts": (receipt,),
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                }
            ),
            "attempts": core.AttemptsState(attempts=(owner,)),
            "evaluation": state.evaluation.model_copy(
                update={
                    "continuations": (continuation,),
                    "jobs": tuple(
                        job.model_copy(
                            update={
                                "released": True,
                                "observation": job.observation.model_copy(
                                    update={"released": True, "children_complete": True}
                                ),
                            }
                        )
                        for job in state.evaluation.jobs
                        if job.observation is not None
                    ),
                }
            ),
            "intents": state.intents.model_copy(
                update={"intents": (*state.intents.intents, intent)}
            ),
        }
    )
    event = core.ContinuationScopeReopened(
        continuation_id=continuation.continuation_id,
        park_authority=park_authority,
        observation=proof,
    )
    return state, continuation, event


def test_positive_registered_reopen_proof_authorizes_same_conversation_once() -> None:
    state, continuation, event = reopened_fixture()
    result = persisted_step(state, event)
    assert result.requests == ()
    assert result.events == (
        core.ResumeAuthorized(
            continuation_id=continuation.continuation_id,
            next_invocation=continuation.next_invocation,
            evidence=(),
        ),
    )
    assert result.state.evaluation.continuations[0].phase == core.ContinuationPhase.AUTHORIZED
    replay = persisted_step(result.state, event)
    assert replay.requests == ()
    assert replay.events == ()


@pytest.mark.parametrize("admission", ["reopened", "closed", "unknown"])
@given(
    status=st.sampled_from(tuple(core.ObservationStatus)),
    accepted=st.booleans(),
    terminal=st.booleans(),
)
def test_reopen_requires_positive_typed_admission_and_inspects_every_unknown(
    *,
    admission: Literal["reopened", "closed", "unknown"],
    status: core.ObservationStatus,
    accepted: bool,
    terminal: bool,
) -> None:
    state, continuation, event = reopened_fixture()
    codec = operation_codec()
    intent = state.intents.intents[-1]
    assert isinstance(intent.request, core.ExecuteRegisteredOperation)
    proof = event.observation.model_copy(
        update={"status": status, "accepted": accepted, "terminal": terminal}
    )
    outcome = core.ScopedAdmissionReopenOutcome(
        scope=state.sessions.invocations[0].scope, admission=admission
    )
    intent = core.Intent.model_validate(
        {
            **intent.model_dump(),
            "observation": proof,
            "outcome_json": codec.encode_outcome(intent.request.operation.schema_ref, outcome),
        },
        context={"operation_registry": codec},
    )
    state = state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={"intents": (*state.intents.intents[:-1], intent)}
            )
        }
    )
    result = persisted_step(state, event.model_copy(update={"observation": proof}))
    if status == core.ObservationStatus.UNKNOWN or admission == "unknown":
        assert result.events == ()
        assert result.state.evaluation.continuations == (continuation,)
        assert len(result.requests) == 1
        request = result.requests[0]
        assert isinstance(request, core.InspectRequest)
        assert request.target == continuation.reopen_authority
        assert request.scope == intent.request.scope
    elif (
        status == core.ObservationStatus.SUCCEEDED
        and accepted
        and terminal
        and admission == "reopened"
    ):
        assert len(result.events) == 1
        assert isinstance(result.events[0], core.ResumeAuthorized)
    else:
        assert result.events == ()
        assert result.requests == ()
        assert result.state.evaluation.continuations == (continuation,)


@pytest.mark.parametrize(
    "guard",
    ["park-authority", "reopen-request", "episode", "scope", "generation", "owner-terminal"],
)
def test_stale_reopen_observations_cannot_authorize_resume(guard: str) -> None:
    state, continuation, event = reopened_fixture()
    if guard == "park-authority":
        event = event.model_copy(update={"park_authority": core.RequestId(root="previous-park")})
    elif guard == "reopen-request":
        event = event.model_copy(
            update={
                "observation": event.observation.model_copy(
                    update={"request_id": core.RequestId(root="previous-reopen")}
                )
            }
        )
    elif guard == "episode":
        event = event.model_copy(
            update={
                "observation": event.observation.model_copy(
                    update={"admission_id": core.DecisionId(root="old-episode")}
                )
            }
        )
    elif guard in ("scope", "generation"):
        scope = event.observation.scope.model_copy(
            update={"owner": core.RunId(root="different-run")}
            if guard == "scope"
            else {"generation": 1}
        )
        event = event.model_copy(
            update={"observation": event.observation.model_copy(update={"scope": scope})}
        )
    else:
        owner = state.attempts.attempts[0].model_copy(update={"phase": core.AttemptPhase.TERMINAL})
        state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    if guard in ("episode", "scope", "generation"):
        with pytest.raises(core.ContractError):
            core.step(state, event)
        return
    result = persisted_step(state, event)
    assert result.events == ()
    assert result.requests == ()
    assert result.state.evaluation.continuations == (continuation,)


@pytest.mark.parametrize(
    ("charge", "predecessor"),
    list(product(["paid", "correction", "resume", "free"], [False, True])),
)
@given(
    guard=st.sampled_from(
        (
            "session",
            "lease",
            "session-scope",
            "session-generation",
            "session-invocation",
            "session-accepted",
            "intent",
            "intent-phase",
            "intent-observation",
            "checkpoint",
        )
    )
)
def test_yield_proof_cannot_be_bypassed_by_optional_predecessor_or_charge_class(
    *, charge: str, predecessor: bool, guard: str
) -> None:
    state, continuation = fixture(settled=True)
    invocation = state.sessions.invocations[0]
    turn = invocation.turn.model_copy(
        update={
            "charge_class": charge,
            "predecessor": continuation.next_invocation if predecessor else None,
        }
    )
    invocation = invocation.model_copy(update={"turn": turn})
    intent = state.intents.intents[0]
    intent = intent.model_copy(update={"request": intent.request.model_copy(update={"turn": turn})})
    sessions = state.sessions.model_copy(update={"invocations": (invocation,)})
    session = sessions.sessions[0]
    if guard == "session":
        sessions = sessions.model_copy(update={"sessions": ()})
    elif guard in (
        "lease",
        "session-scope",
        "session-generation",
        "session-invocation",
        "session-accepted",
    ):
        fields = {
            "lease": {"resource_id": None},
            "session-scope": {
                "scope": core.Scope(owner=core.AttemptId(root="other"), generation=0)
            },
            "session-generation": {"generation": 1},
            "session-invocation": {"invocation": None},
            "session-accepted": {"accepted": False},
        }
        session = session.model_copy(update=fields[guard])
        sessions = sessions.model_copy(update={"sessions": (session,)})
    intents = () if guard == "intent" else (intent,)
    if guard == "intent-phase":
        intents = (intent.model_copy(update={"phase": core.IntentPhase.DISPATCHED}),)
    elif guard == "intent-observation":
        intents = (intent.model_copy(update={"observation": None}),)
    state = state.model_copy(
        update={
            "sessions": sessions,
            "intents": state.intents.model_copy(update={"intents": intents}),
        }
    )
    if guard == "checkpoint":
        owner = state.attempts.attempts[0].model_copy(update={"checkpoints": ()})
        state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    with pytest.raises(core.ContractError):
        core.step(state, core.TurnSuspended(continuation=continuation))
    assert state.evaluation.continuations == ()


@pytest.mark.parametrize("registered", [False, True])
@given(
    foreign_generation=st.booleans(),
    owner_phase=st.sampled_from(tuple(core.AttemptPhase)),
    event_kind=st.sampled_from(("deadline", "park", "cancel")),
)
def test_foreign_dependencies_never_authorize_cleanup_even_for_inactive_owner(
    *,
    registered: bool,
    foreign_generation: bool,
    owner_phase: core.AttemptPhase,
    event_kind: str,
) -> None:
    state, continuation = fixture(registered=registered)
    field = "registered_jobs" if registered else "jobs"
    jobs = list(getattr(state.evaluation, field))
    jobs[0] = jobs[0].model_copy(
        update={
            "scope": core.Scope(
                owner=jobs[0].scope.owner if foreign_generation else core.AttemptId(root="foreign"),
                generation=1 if foreign_generation else 0,
            )
        }
    )
    owner = state.attempts.attempts[0].model_copy(update={"phase": owner_phase})
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "evaluation": state.evaluation.model_copy(
                update={field: tuple(jobs), "continuations": (continuation,)}
            ),
        }
    )
    event = (
        core.DeadlineReached(continuation_id=continuation.continuation_id, now_at=10.0)
        if event_kind == "deadline"
        else core.ContinuationRetireRequested(
            continuation_id=continuation.continuation_id,
            disposition="park" if event_kind == "park" else "cancel",
            park_authority=core.RequestId(root="park") if event_kind == "park" else None,
        )
    )
    original = state.model_dump_json()
    try:
        result = persisted_step(state, event)
    except core.ContractError:
        assert state.model_dump_json() == original
    else:
        assert result.requests == ()
        assert result.events == ()


@pytest.mark.parametrize("access", tuple(core.Access))
def test_run_owned_profiler_cannot_bypass_writable_checkpoint_guard(access: core.Access) -> None:
    state, continuation = fixture(run_owned=True, settled=True)
    invocation = state.sessions.invocations[0]
    spec = invocation.turn.session.model_copy(update={"access": access})
    turn = invocation.turn.model_copy(update={"session": spec})
    invocation = invocation.model_copy(update={"turn": turn})
    session = state.sessions.sessions[0].model_copy(update={"spec": spec})
    intent = state.intents.intents[0]
    intent = intent.model_copy(update={"request": intent.request.model_copy(update={"turn": turn})})
    state = state.model_copy(
        update={
            "sessions": state.sessions.model_copy(
                update={"sessions": (session,), "invocations": (invocation,)}
            ),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    )
    if access == core.Access.WRITE_CANDIDATE:
        with pytest.raises(core.ContractError, match="checkpoint"):
            core.step(state, core.TurnSuspended(continuation=continuation))
    else:
        result = persisted_step(state, core.TurnSuspended(continuation=continuation))
        assert len(result.events) == 1
        assert isinstance(result.events[0], core.ResumeAuthorized)


@given(sequence=st.integers(min_value=0, max_value=100))
def test_late_job_changes_cannot_rewrite_frozen_timeout_or_resume(sequence: int) -> None:
    state, continuation = fixture(settled=True)
    timeout = core.TimedOut(
        deadline_at=10.0,
        reached_at=10.0,
        unfinished=tuple(
            core.JobTimeout(resource_id=job, progress=None) for job in continuation.jobs
        ),
    )
    continuation = continuation.model_copy(
        update={"phase": core.ContinuationPhase.AUTHORIZED, "timeout": timeout}
    )
    jobs = []
    for job in state.evaluation.jobs:
        assert job.observation is not None
        changed = job.observation.model_copy(update={"sequence": sequence, "observed_at": 20.0})
        jobs.append(job.model_copy(update={"observation": changed}))
    evaluation = state.evaluation.model_copy(
        update={"jobs": tuple(jobs), "continuations": (continuation,)}
    )
    state = state.model_copy(update={"evaluation": evaluation})
    for job in continuation.jobs:
        result = persisted_step(
            state, core.ContinuationJobsChanged(resource_id=job, observation_sequence=sequence)
        )
        assert result.events == ()
        assert result.requests == ()
        assert result.state.evaluation.continuations == (continuation,)
        state = result.state
    assert state.evaluation.continuations[0].timeout == timeout
    assert all(job.progress is None for job in timeout.unfinished)


def resume_history(
    state: core.CoreState,
    continuation: core.Continuation,
    invocation: core.Invocation,
    prior_invocation: core.InvocationRef,
) -> tuple[core.CoreState, tuple[core.Invocation, ...]]:
    prior = continuation.model_copy(
        update={
            "continuation_id": core.ContinuationId(root="prior-wait"),
            "invocation": prior_invocation,
            "next_invocation": invocation.invocation,
            "phase": core.ContinuationPhase.AUTHORIZED,
        }
    )
    evaluation = state.evaluation.model_copy(update={"continuations": (prior,)})
    state = state.model_copy(update={"evaluation": evaluation})
    previous_turn = invocation.turn.model_copy(
        update={"invocation_id": prior_invocation.invocation_id}
    )
    history = (
        invocation.model_copy(update={"invocation": prior_invocation, "turn": previous_turn}),
    )
    return state, history


@pytest.mark.parametrize("source", ["dispatch", "resume", "registered"])
@pytest.mark.parametrize(
    "proof",
    [
        "matching",
        "manifest-equal",
        "manifest-conflict",
        "attempt-admission",
        "request-admission",
        "observation-admission",
        "closure",
        "ancestry",
    ],
)
@given(charge=st.sampled_from(("paid", "correction", "resume", "free")), predecessor=st.booleans())
def test_all_canonical_turn_sources_authorize_without_new_attempt_charge(
    *, source: str, proof: str, charge: str, predecessor: bool
) -> None:
    state, continuation = fixture(settled=True)
    invocation = state.sessions.invocations[0]
    prior_invocation = invocation.invocation.model_copy(
        update={"invocation_id": core.InvocationId(root="preceding")}
    )
    turn = invocation.turn.model_copy(
        update={
            "charge_class": charge,
            "predecessor": prior_invocation if predecessor else None,
            "continuation_id": core.ContinuationId(root="prior-wait")
            if proof == "ancestry"
            else None,
        }
    )
    invocation = invocation.model_copy(update={"turn": turn})
    history = ()
    if source == "resume" and proof != "ancestry":
        state, history = resume_history(state, continuation, invocation, prior_invocation)
    intent = state.intents.intents[0]
    if source == "dispatch":
        request: core.Request = core.DispatchTurn(
            request_id=intent.request_id,
            scope=invocation.scope,
            deadline_at=100.0,
            admission_id=intent.request.admission_id,
            turn=turn,
        )
    elif source == "resume":
        request = core.ResumeSessionTurn(
            request_id=intent.request_id,
            scope=invocation.scope,
            deadline_at=100.0,
            admission_id=intent.request.admission_id,
            turn=turn,
            continuation_id=core.ContinuationId(root="prior-wait"),
        )
    else:
        codec = operation_codec()
        payload = RegisteredTurn(turn=turn)
        decision = codec.validate_decision(
            core.Operation(
                decision_id=core.DecisionId(root="registered-turn"),
                scope=invocation.scope,
                deadline_at=100.0,
                request=payload,
            )
        )
        request = core.ExecuteRegisteredOperation(
            request_id=intent.request_id,
            operation_id=core.OperationId(root="operation:registered-turn"),
            decision_id=decision.decision_id,
            scope=invocation.scope,
            deadline_at=100.0,
            admission_id=intent.request.admission_id,
            operation=codec.encode(payload),
            retry_limit=state.run.limits.max_retries,
        )
        invocation = invocation.model_copy(update={"registered_operation": request.operation_id})
        receipt = core.DecisionReceipt(
            decision_id=decision.decision_id,
            decision=decision,
            payload_digest="turn-extension",
            feedback=core.Accepted(decision_id=decision.decision_id),
            request_ids=(intent.request_id,),
        )
        run = state.run.model_copy(
            update={
                "receipts": (receipt,),
                "capabilities": core.Capabilities(operations=codec.descriptors),
            }
        )
        state = state.model_copy(update={"registry": codec.descriptors, "run": run})
    intent = intent.model_copy(update={"request": request})
    if proof in ("manifest-equal", "manifest-conflict"):
        manifest = (
            continuation
            if proof == "manifest-equal"
            else continuation.model_copy(update={"deadline_at": 9.0})
        )
        intent = intent.model_copy(update={"suspension": manifest})
    if proof == "attempt-admission":
        owner = state.attempts.attempts[0].model_copy(update={"admission_id": None})
        state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    elif proof == "request-admission":
        intent = intent.model_copy(
            update={"request": request.model_copy(update={"admission_id": None})}
        )
    elif proof == "observation-admission":
        assert invocation.observation is not None
        observation_without_episode = invocation.observation.model_copy(
            update={"admission_id": None}
        )
        invocation = invocation.model_copy(update={"observation": observation_without_episode})
        intent = intent.model_copy(update={"observation": observation_without_episode})
    elif proof == "closure":
        owner = state.attempts.attempts[0]
        assert owner.admission_id is not None
        owner = owner.model_copy(
            update={
                "closure": core.AttemptClosure(
                    disposition="park",
                    requested_at=1.0,
                    authority=core.RequestId(root="close"),
                    admission_id=owner.admission_id,
                )
            }
        )
        state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    state = state.model_copy(
        update={
            "sessions": state.sessions.model_copy(update={"invocations": (*history, invocation)}),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    )
    if proof not in ("matching", "manifest-equal"):
        with pytest.raises(core.ContractError):
            core.step(state, core.TurnSuspended(continuation=continuation))
        return
    result = persisted_step(state, core.TurnSuspended(continuation=continuation))
    assert result.requests == ()
    assert len(result.events) == 1
    assert isinstance(result.events[0], core.ResumeAuthorized)
    assert result.events[0].next_invocation == continuation.next_invocation


@pytest.mark.parametrize("at", [9.0, 10.0, 11.0])
def test_terminal_observation_requires_deadline_freeze_before_boundary_authorization(
    at: float,
) -> None:
    state, continuation = fixture(settled=True)
    jobs = tuple(
        job.model_copy(
            update={"observation": job.observation.model_copy(update={"observed_at": at})}
        )
        for job in state.evaluation.jobs
        if job.observation is not None
    )
    state = state.model_copy(
        update={"evaluation": state.evaluation.model_copy(update={"jobs": jobs})}
    )
    event = core.TurnSuspended(continuation=continuation)
    if at < continuation.deadline_at:
        result = persisted_step(state, event)
        assert len(result.events) == 1
        assert isinstance(result.events[0], core.ResumeAuthorized)
        assert result.events[0].timeout is None
    else:
        # B receives only post-update facts. The frozen contract lacks the prior
        # progress snapshot, so it must reject rather than invent timeout evidence.
        with pytest.raises(core.ContractError, match="deadline"):
            core.step(state, event)


@pytest.mark.parametrize("field", ["next_invocation", "jobs", "deadline_at"])
def test_duplicate_continuation_identity_rejects_changed_immutable_payload(field: str) -> None:
    state, continuation = fixture()
    first = persisted_step(state, core.TurnSuspended(continuation=continuation))
    values = {
        "next_invocation": continuation.next_invocation.model_copy(
            update={"invocation_id": core.InvocationId(root="changed")}
        ),
        "jobs": continuation.jobs[:1],
        "deadline_at": 9.0,
    }
    with pytest.raises(core.ContractError, match="immutable"):
        core.step(
            first.state,
            core.TurnSuspended(continuation=continuation.model_copy(update={field: values[field]})),
        )
    assert first.state.evaluation.continuations == (continuation,)


@pytest.mark.parametrize("phase", tuple(core.AttemptPhase))
@given(run_status=st.sampled_from(tuple(core.RunStatus)), closing=st.booleans())
def test_current_live_owner_guard_holds_at_admission_and_delayed_authorization(
    *, phase: core.AttemptPhase, run_status: core.RunStatus, closing: bool
) -> None:
    state, continuation = fixture(settled=True)
    owner = state.attempts.attempts[0]
    closure = (
        core.AttemptClosure(
            disposition="park",
            requested_at=1.0,
            authority=core.RequestId(root="close"),
            admission_id=core.DecisionId(root="episode"),
        )
        if closing
        else None
    )
    owner = owner.model_copy(update={"phase": phase, "closure": closure})
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "run": state.run.model_copy(update={"status": run_status}),
        }
    )
    permitted = (
        phase == core.AttemptPhase.ACTIVE and run_status == core.RunStatus.RUNNING and not closing
    )
    if permitted:
        result = persisted_step(state, core.TurnSuspended(continuation=continuation))
        assert len(result.events) == 1
    else:
        with pytest.raises(core.ContractError):
            core.step(state, core.TurnSuspended(continuation=continuation))
    state = with_wait(state, continuation)
    result = persisted_step(
        state,
        core.ContinuationJobsChanged(resource_id=continuation.jobs[0], observation_sequence=1),
    )
    assert bool(result.events) == permitted
    assert result.requests == ()


@pytest.mark.parametrize("park_authority", [None, "canonical", "other"])
def test_parking_published_authorization_cannot_erase_once_only_receipt(
    park_authority: str | None,
) -> None:
    state, continuation = fixture(settled=True)
    authorized = persisted_step(state, core.TurnSuspended(continuation=continuation))
    assert len(authorized.events) == 1
    event = core.ContinuationRetireRequested(
        continuation_id=continuation.continuation_id,
        disposition="park",
        park_authority=core.RequestId(root=park_authority) if park_authority else None,
    )
    with pytest.raises(core.ContractError):
        core.step(authorized.state, event)
    assert authorized.state.evaluation.continuations[0].phase == core.ContinuationPhase.AUTHORIZED
    duplicate = persisted_step(authorized.state, core.TurnSuspended(continuation=continuation))
    assert duplicate.events == ()
    assert duplicate.requests == ()


@pytest.mark.parametrize("registered", [False, True])
@given(status=st.sampled_from(tuple(core.ObservationStatus)), terminal=st.booleans())
def test_only_conclusive_terminal_dependencies_complete_wait_all(
    *,
    registered: bool,
    status: core.ObservationStatus,
    terminal: bool,
) -> None:
    state, continuation = fixture(registered=registered, settled=True)
    field = "registered_jobs" if registered else "jobs"
    jobs = list(getattr(state.evaluation, field))
    job = jobs[0]
    assert job.observation is not None
    jobs[0] = job.model_copy(
        update={
            "status": status,
            "terminal": terminal,
            "observation": job.observation.model_copy(
                update={"status": status, "terminal": terminal}
            ),
        }
    )
    state = state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(
                update={
                    field: tuple(jobs),
                    "continuations": (continuation,),
                }
            )
        }
    )
    result = persisted_step(
        state,
        core.ContinuationJobsChanged(
            resource_id=continuation.jobs[0],
            observation_sequence=1,
        ),
    )
    conclusive = terminal and status not in (
        core.ObservationStatus.PENDING,
        core.ObservationStatus.UNKNOWN,
    )
    assert bool(result.events) == conclusive
    if status == core.ObservationStatus.UNKNOWN:
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], core.InspectOwnedJob)
        assert result.requests[0].resource_id == continuation.jobs[0]
    else:
        assert result.requests == ()
    if conclusive:
        assert isinstance(result.events[0], core.ResumeAuthorized)
    else:
        assert result.state.evaluation.continuations == (continuation,)


@pytest.mark.parametrize("registered", [False, True])
@given(
    terminal=st.booleans(),
    progress_present=st.booleans(),
    fields=st.tuples(st.booleans(), st.booleans(), st.booleans(), st.booleans(), st.booleans()),
)
def test_unknown_job_always_requests_inspection_for_all_optional_progress_fields(
    *,
    registered: bool,
    terminal: bool,
    progress_present: bool,
    fields: tuple[bool, bool, bool, bool, bool],
) -> None:
    state, continuation = fixture(registered=registered)
    field = "registered_jobs" if registered else "jobs"
    jobs = list(getattr(state.evaluation, field))
    job = jobs[0]
    assert job.observation is not None
    stage, queued, ran, reason, estimate = fields
    progress = (
        core.JobProgress(
            observation_sequence=job.observation.sequence,
            observed_at=job.observation.observed_at,
            state="unknown",
            stage_id="measure" if stage else None,
            queued_s=0.0 if queued else None,
            ran_s=2.0 if ran else None,
            pending_reason="waiting" if reason else None,
            estimated_start_at=3.0 if estimate else None,
        )
        if progress_present
        else None
    )
    jobs[0] = job.model_copy(
        update={
            "status": core.ObservationStatus.UNKNOWN,
            "terminal": terminal,
            "observation": job.observation.model_copy(
                update={"status": core.ObservationStatus.UNKNOWN, "terminal": terminal}
            ),
            "progress": progress,
        }
    )
    state = state.model_copy(
        update={"evaluation": state.evaluation.model_copy(update={field: tuple(jobs)})}
    )
    result = persisted_step(state, core.TurnSuspended(continuation=continuation))
    assert result.events == ()
    assert len(result.requests) == 3
    inspection = tuple(
        request for request in result.requests if isinstance(request, core.InspectOwnedJob)
    )
    assert len(inspection) == 1
    assert inspection[0].resource_id == continuation.jobs[0]
    assert result.state.evaluation.continuations == (continuation,)


def parked_fixture() -> tuple[core.CoreState, core.ContinuationReopenRequested]:
    state, continuation, _ = reopened_fixture()
    codec = operation_codec()
    scope = state.sessions.invocations[0].scope
    admission = core.DecisionId(root="original-episode")
    assert continuation.park_authority is not None
    owner = state.attempts.attempts[0].model_copy(
        update={
            "phase": core.AttemptPhase.PARKED,
            "admission_id": admission,
            "closure": core.AttemptClosure(
                disposition="park",
                requested_at=1.0,
                authority=continuation.park_authority,
                admission_id=admission,
            ),
        }
    )
    close = core.CloseAttemptScope(
        request_id=continuation.park_authority,
        scope=scope,
        deadline_at=100.0,
        admission_id=admission,
        attempt=core.AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
    )
    close_proof = observation(scope, continuation.park_authority).model_copy(
        update={
            "released": True,
            "admission_id": admission,
        }
    )
    close_intent = core.Intent(
        request_id=continuation.park_authority,
        request=close,
        payload_digest="close",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.COMPLETED,
        observation=close_proof,
        reconcile_deadline_at=100.0,
    )
    continuation = continuation.model_copy(
        update={
            "phase": core.ContinuationPhase.PARKED,
            "reopen_authority": None,
        }
    )
    decision = state.run.receipts[0].decision
    assert isinstance(decision, core.Operation)
    normalization = decision.normalized_scope_reopen
    assert normalization is not None
    reopen_request = state.intents.intents[-1].request
    assert isinstance(reopen_request, core.ExecuteRegisteredOperation)
    reopen_request = reopen_request.model_copy(update={"admission_id": None})
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)}),
            "intents": state.intents.model_copy(
                update={"intents": (*state.intents.intents[:-1], close_intent)}
            ),
        }
    )
    assert codec.validate_decision(decision) == decision
    return state, core.ContinuationReopenRequested(
        request=reopen_request, normalization=normalization
    )


@pytest.mark.parametrize("registered", [False, True])
@given(
    terminal=st.booleans(),
    released=st.booleans(),
    status=st.sampled_from(tuple(core.ObservationStatus)),
    timeout_present=st.booleans(),
)
def test_scope_close_and_timeout_never_substitute_for_dependency_release(
    *,
    registered: bool,
    terminal: bool,
    released: bool,
    status: core.ObservationStatus,
    timeout_present: bool,
) -> None:
    conclusive = terminal and status not in (
        core.ObservationStatus.PENDING,
        core.ObservationStatus.UNKNOWN,
    )
    # Positive forwarding composes with the independently owned Attempts B
    # leaf. Positive external reopen feedback is exercised separately above.
    assume(not (conclusive and released))
    state, event = parked_fixture()
    job = state.evaluation.jobs[0]
    assert job.observation is not None
    job = job.model_copy(
        update={
            "status": status,
            "terminal": terminal,
            "released": released,
            "observation": job.observation.model_copy(
                update={"status": status, "terminal": terminal, "released": released}
            ),
        }
    )
    continuation = state.evaluation.continuations[0]
    if timeout_present:
        continuation = continuation.model_copy(
            update={
                "timeout": core.TimedOut(
                    deadline_at=10.0,
                    reached_at=10.0,
                    unfinished=(core.JobTimeout(resource_id=job.resource_id),),
                )
            }
        )
    jobs = (job, *state.evaluation.jobs[1:])
    registered_jobs = (
        tuple(
            core.RegisteredOwnedJob(
                operation_id=core.OperationId(root=f"custom-{index}"),
                request_id=row.submission_id,
                scope=row.scope,
                resource_pool=core.PoolId(root="pool"),
                resource_id=row.resource_id,
                status=row.status,
                terminal=row.terminal,
                released=row.released,
                observation=row.observation,
            )
            for index, row in enumerate(jobs)
        )
        if registered
        else ()
    )
    state = state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(
                update={
                    "jobs": () if registered else jobs,
                    "registered_jobs": registered_jobs,
                    "continuations": (continuation,),
                }
            )
        }
    )
    original = state.model_dump_json()
    with pytest.raises(core.ContractError):
        core.step(state, event)
    assert state.model_dump_json() == original


@pytest.mark.parametrize(
    ("closure_present", "episode_present"), [(False, False), (False, True), (True, False)]
)
def test_missing_park_closure_or_episode_never_grants_reopen(
    *,
    closure_present: bool,
    episode_present: bool,
) -> None:
    state, event = parked_fixture()
    owner = state.attempts.attempts[0]
    closure = owner.closure
    assert closure is not None
    owner = owner.model_copy(
        update={
            "closure": closure if closure_present else None,
            "admission_id": owner.admission_id if episode_present else None,
        }
    )
    intent = state.intents.intents[-1]
    if not episode_present:
        intent = intent.model_copy(
            update={
                "request": intent.request.model_copy(update={"admission_id": None}),
            }
        )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(
                update={"intents": (*state.intents.intents[:-1], intent)}
            ),
        }
    )
    with pytest.raises(core.ContractError):
        core.step(state, event)


@pytest.mark.parametrize("phase", tuple(core.ContinuationPhase))
@given(fields=st.tuples(st.booleans(), st.booleans(), st.booleans(), st.booleans(), st.booleans()))
def test_new_suspension_never_accepts_caller_manufactured_authority_or_feedback(
    phase: core.ContinuationPhase,
    fields: tuple[bool, bool, bool, bool, bool],
) -> None:
    state, continuation = fixture(settled=True)
    timeout, park, reopen, cancelled, evidence = fields
    owned = state.evaluation.jobs[0]
    fabricated = core.EvidenceRef(
        evidence_id=core.EvidenceId(root="fabricated"),
        kind=core.EvidenceKind.BENCHMARK,
        purpose="official",
        scope=owned.scope,
        source_request=owned.submission_id,
        candidate=state.run.facts.baseline,
        observation_sequence=1,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        provenance="self-report",
        status=core.ObservationStatus.SUCCEEDED,
    )
    continuation = continuation.model_copy(
        update={
            "phase": phase,
            "timeout": core.TimedOut(
                deadline_at=10.0,
                reached_at=10.0,
                unfinished=(core.JobTimeout(resource_id=owned.resource_id),),
            )
            if timeout
            else None,
            "park_authority": core.RequestId(root="park") if park else None,
            "reopen_authority": core.RequestId(root="reopen") if reopen else None,
            "cancelled_resolutions": (owned.resource_id,) if cancelled else (),
            "evidence": (fabricated,) if evidence else (),
        }
    )
    if phase == core.ContinuationPhase.WAITING and not any(fields):
        result = persisted_step(state, core.TurnSuspended(continuation=continuation))
        assert len(result.events) == 1
    else:
        with pytest.raises(core.ContractError):
            core.step(state, core.TurnSuspended(continuation=continuation))


@given(
    same_session=st.booleans(),
    same_generation=st.booleans(),
    distinct=st.booleans(),
    used=st.booleans(),
)
def test_resume_successor_must_be_distinct_unused_and_in_the_same_conversation(
    *,
    same_session: bool,
    same_generation: bool,
    distinct: bool,
    used: bool,
) -> None:
    state, continuation = fixture(settled=True)
    successor = continuation.next_invocation.model_copy(
        update={
            "session_id": continuation.invocation.session_id
            if same_session
            else core.SessionId(root="other-session"),
            "generation": continuation.invocation.generation if same_generation else 1,
            "invocation_id": continuation.next_invocation.invocation_id
            if distinct
            else continuation.invocation.invocation_id,
        }
    )
    continuation = continuation.model_copy(update={"next_invocation": successor})
    if used:
        existing = state.sessions.invocations[0].model_copy(update={"invocation": successor})
        state = state.model_copy(
            update={
                "sessions": state.sessions.model_copy(
                    update={"invocations": (*state.sessions.invocations, existing)}
                )
            }
        )
    if same_session and same_generation and distinct and not used:
        result = persisted_step(state, core.TurnSuspended(continuation=continuation))
        assert len(result.events) == 1
    else:
        with pytest.raises(core.ContractError):
            core.step(state, core.TurnSuspended(continuation=continuation))


@pytest.mark.parametrize("phase", tuple(core.SessionPhase))
def test_current_session_phase_is_required_even_with_a_completed_yield(
    phase: core.SessionPhase,
) -> None:
    state, continuation = fixture(settled=True)
    session = state.sessions.sessions[0].model_copy(update={"phase": phase})
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"sessions": (session,)})}
    )
    if phase in (core.SessionPhase.SUSPENDED, core.SessionPhase.CHECKPOINTED):
        result = persisted_step(state, core.TurnSuspended(continuation=continuation))
        assert len(result.events) == 1
    else:
        with pytest.raises(core.ContractError):
            core.step(state, core.TurnSuspended(continuation=continuation))


@pytest.mark.parametrize("frozen", [False, True])
@given(
    conflict=st.booleans(),
    duplicated=st.booleans(),
    provenance=st.sampled_from(("trusted", "self-report")),
    status=st.sampled_from(tuple(core.ObservationStatus)),
)
def test_feedback_deduplicates_equal_evidence_and_rejects_conflicting_identity(
    *,
    frozen: bool,
    conflict: bool,
    duplicated: bool,
    provenance: Literal["trusted", "self-report"],
    status: core.ObservationStatus,
) -> None:
    state, continuation = fixture(settled=True)
    first = state.evaluation.jobs[0]
    evidence = core.EvidenceRef(
        evidence_id=core.EvidenceId(root="shared"),
        kind=core.EvidenceKind.BENCHMARK,
        purpose="official",
        scope=first.scope,
        source_request=first.submission_id,
        candidate=state.run.facts.baseline,
        observation_sequence=1,
        evaluator_digest=state.run.facts.evaluator_digest,
        workload_digest=state.run.facts.workload_digest,
        environment_digest=state.run.facts.environment_digest,
        provenance=provenance,
        status=status,
        artifacts=(
            core.ArtifactRef(
                artifact_id=core.ArtifactId(root="measurement"), digest="measurement-digest"
            ),
        ),
    )
    duplicate = (
        evidence.model_copy(update={"environment_digest": "different"}) if conflict else evidence
    )
    if frozen:
        continuation = continuation.model_copy(
            update={
                "evidence": (evidence, duplicate) if duplicated else (evidence,),
                "timeout": core.TimedOut(
                    deadline_at=10.0,
                    reached_at=10.0,
                    unfinished=(core.JobTimeout(resource_id=first.resource_id),),
                ),
            }
        )
        state = with_wait(state, continuation)
        event: core.CoreEvent = core.ContinuationJobsChanged(
            resource_id=first.resource_id, observation_sequence=1
        )
    else:
        jobs = tuple(
            job.model_copy(
                update={
                    "evidence": (evidence,) if index == 0 else (duplicate,) if duplicated else ()
                }
            )
            for index, job in enumerate(state.evaluation.jobs)
        )
        state = state.model_copy(
            update={"evaluation": state.evaluation.model_copy(update={"jobs": jobs})}
        )
        event = core.TurnSuspended(continuation=continuation)
    if duplicated and (conflict or frozen):
        with pytest.raises(core.ContractError, match="evidence"):
            core.step(state, event)
    else:
        result = persisted_step(state, event)
        assert len(result.events) == 1
        feedback = result.events[0]
        assert isinstance(feedback, core.ResumeAuthorized)
        assert feedback.evidence == (evidence,)
        assert result.state.evaluation.continuations[0].evidence == (evidence,)
        assert feedback.timeout == continuation.timeout


@pytest.mark.parametrize(
    "proof",
    [
        "positive",
        "missing",
        "not-terminal",
        "not-released",
        "incomplete",
        "unknown",
        "wrong-request",
        "wrong-scope",
    ],
)
@given(recorded_on_parent=st.booleans(), nested=st.booleans(), parent_known=st.booleans())
def test_parent_release_never_substitutes_for_independent_child_proof(
    *,
    proof: str,
    recorded_on_parent: bool,
    nested: bool,
    parent_known: bool,
) -> None:
    state, continuation, event = reopened_fixture()
    parent = state.evaluation.jobs[0]
    assert parent.observation is not None
    child_id = core.ResourceId(root="child")
    middle_id = core.ResourceId(root="middle")
    source = core.RequestId(root="middle-submission") if nested else parent.submission_id
    middle = None
    on_parent = recorded_on_parent or proof in ("missing", "wrong-scope")
    if nested:
        middle = parent.model_copy(
            update={
                "resource_id": middle_id,
                "submission_id": source,
                "children": (child_id,) if on_parent else (),
                "observation": observation(parent.scope, source, middle_id).model_copy(
                    update={
                        "released": True,
                        "children_complete": True,
                        "children": (child_id,) if on_parent else (),
                    }
                ),
            }
        )
    if on_parent or nested:
        children = (middle_id,) if nested else (child_id,)
        parent = parent.model_copy(
            update={
                "children": children,
                "observation": parent.observation.model_copy(update={"children": children}),
            }
        )
    child_scope = (
        parent.scope
        if proof != "wrong-scope"
        else core.Scope(owner=core.AttemptId(root="foreign"), generation=0)
    )
    child_observation = observation(child_scope, source, child_id).model_copy(
        update={
            "released": True,
            "children_complete": True,
        }
    )
    bad_fields = {
        "not-terminal": {"terminal": False},
        "not-released": {"released": False},
        "incomplete": {"children_complete": False},
        "unknown": {"status": core.ObservationStatus.UNKNOWN},
        "wrong-request": {"request_id": core.RequestId(root="unrelated")},
    }
    child_observation = child_observation.model_copy(update=bad_fields.get(proof, {}))
    child = core.ChildLease(
        resource_id=child_id,
        scope=child_scope,
        source_requests=(source,),
        parent_resources=((middle_id if nested else parent.resource_id),) if parent_known else (),
        observation=child_observation,
    )
    state = state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(
                update={
                    "jobs": (parent, *state.evaluation.jobs[1:]) + ((middle,) if middle else ())
                }
            ),
            "intents": state.intents.model_copy(
                update={"children": () if proof == "missing" else (child,)}
            ),
        }
    )
    if proof == "positive":
        result = persisted_step(state, event)
        assert len(result.events) == 1
        assert isinstance(result.events[0], core.ResumeAuthorized)
        assert result.events[0].next_invocation == continuation.next_invocation
    else:
        with pytest.raises(core.ContractError, match="children"):
            core.step(state, event)


@pytest.mark.parametrize(
    "guard",
    [
        "receipt-rejected",
        "receipt-id",
        "receipt-failed",
        "target",
        "wire",
        "park-authority",
        "cancelled-missing",
        "cancelled-extra",
        "cancelled-duplicate",
    ],
)
def test_reopen_normalization_and_exact_cancelled_set_are_required_before_forwarding(
    guard: str,
) -> None:
    state, event = parked_fixture()
    receipt = state.run.receipts[0]
    if guard == "receipt-rejected":
        receipt = receipt.model_copy(
            update={
                "feedback": core.Rejected(
                    decision_id=receipt.decision_id,
                    code=core.RejectionCode.OWNERSHIP,
                    path=("scope",),
                    detail="rejected",
                )
            }
        )
    elif guard == "receipt-id":
        receipt = receipt.model_copy(update={"decision_id": core.DecisionId(root="other")})
    elif guard == "receipt-failed":
        receipt = receipt.model_copy(update={"completion": core.CompletionStatus.FAILED})
    elif guard == "target":
        event = event.model_copy(
            update={
                "normalization": event.normalization.model_copy(
                    update={
                        "attempt": core.AttemptRef(
                            attempt_id=core.AttemptId(root="other"), generation=0
                        )
                    }
                )
            }
        )
    elif guard == "wire":
        request = event.request.model_copy(update={"deadline_at": 99.0})
        event = event.model_copy(update={"request": request})
    elif guard == "park-authority":
        event = event.model_copy(
            update={
                "normalization": event.normalization.model_copy(
                    update={"park_authority": core.RequestId(root="other")}
                )
            }
        )
    else:
        first = state.evaluation.jobs[0]
        jobs = state.evaluation.jobs
        if guard in ("cancelled-missing", "cancelled-duplicate"):
            assert first.observation is not None
            first = first.model_copy(
                update={
                    "status": core.ObservationStatus.CANCELLED,
                    "observation": first.observation.model_copy(
                        update={"status": core.ObservationStatus.CANCELLED}
                    ),
                }
            )
            jobs = (first, *jobs[1:])
        resolution = (
            ()
            if guard == "cancelled-missing"
            else (first.resource_id,) * (2 if guard == "cancelled-duplicate" else 1)
        )
        codec = operation_codec()
        decision = receipt.decision
        assert isinstance(decision, core.Operation)
        assert isinstance(decision.request, core.ScopedAdmissionReopen)
        payload = decision.request.model_copy(update={"resolved_cancelled_jobs": resolution})
        if guard == "cancelled-duplicate":
            with pytest.raises(ValueError, match="duplicate"):
                codec.encode(payload)
            return
        decision = codec.validate_decision(decision.model_copy(update={"request": payload}))
        receipt = receipt.model_copy(update={"decision": decision})
        assert decision.normalized_scope_reopen is not None
        event = core.ContinuationReopenRequested(
            request=event.request.model_copy(update={"operation": codec.encode(payload)}),
            normalization=decision.normalized_scope_reopen,
        )
        state = state.model_copy(
            update={"evaluation": state.evaluation.model_copy(update={"jobs": jobs})}
        )
    state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": (receipt,)})})
    with pytest.raises(core.ContractError):
        core.step(state, event)


@given(fragment=st.text(alphabet="abc", min_size=1, max_size=8))
@example(fragment="b")
def test_unknown_job_inspection_identity_frames_continuation_and_resource(fragment: str) -> None:
    identities = []
    for continuation_root, job_root in ((f"a/{fragment}", "c"), ("a", f"{fragment}/c")):
        state, continuation = fixture()
        resource = core.ResourceId(root=job_root)
        job = state.evaluation.jobs[0]
        assert job.observation is not None
        observed = job.observation.model_copy(
            update={"resource_id": resource, "status": core.ObservationStatus.UNKNOWN}
        )
        job = job.model_copy(
            update={
                "resource_id": resource,
                "status": core.ObservationStatus.UNKNOWN,
                "observation": observed,
            }
        )
        continuation = continuation.model_copy(
            update={
                "continuation_id": core.ContinuationId(root=continuation_root),
                "jobs": (resource, *continuation.jobs[1:]),
            }
        )
        evaluation = state.evaluation.model_copy(
            update={"jobs": (job, *state.evaluation.jobs[1:]), "continuations": (continuation,)}
        )
        signal = core.ContinuationJobsChanged(resource_id=resource, observation_sequence=1)
        result = persisted_step(state.model_copy(update={"evaluation": evaluation}), signal)
        assert len(result.requests) == 1
        inspection = result.requests[0]
        assert isinstance(inspection, core.InspectOwnedJob)
        identities.append(inspection.request_id)
        duplicate = persisted_step(result.state, signal)
        assert duplicate.requests == ()
    assert identities[0] != identities[1]
