"""Continuation authorization uses durable yield, checkpoint and job proofs."""

from itertools import product
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


def roundtrip(state: core.CoreState) -> core.CoreState:
    """Reload the same atomic envelope the host persists."""
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


def fixture(
    *, registered: bool = False, run_owned: bool = False, settled: bool = False
) -> tuple[core.CoreState, core.Continuation]:
    state = core.initial_state()
    scope = core.Scope(
        owner=state.run.run_id if run_owned else core.AttemptId(root="attempt"), generation=0
    )
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
        access=core.Access.WRITE_CANDIDATE,
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
        observation=observation(scope, core.RequestId(root="turn")),
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
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=() if run_owned else (attempt,)),
            "sessions": core.SessionsState(sessions=(session,), invocations=(record,)),
            "intents": state.intents.model_copy(
                update={
                    "intents": (
                        core.Intent(
                            request_id=core.RequestId(root="turn"),
                            request=core.DispatchTurn(
                                request_id=core.RequestId(root="turn"),
                                scope=scope,
                                deadline_at=100.0,
                                turn=turn,
                            ),
                            payload_digest="canonical-turn",
                            lifecycle=core.LifecycleClass.SESSION_TURN,
                            phase=core.IntentPhase.COMPLETED,
                            observation=record.observation,
                            reconcile_deadline_at=100.0,
                        ),
                    )
                }
            ),
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
    state = state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)})
        }
    )
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
    state = state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)})
        }
    )
    result = persisted_step(
        state, core.DeadlineReached(continuation_id=continuation.continuation_id, now_at=now)
    )
    assert result.state.evaluation.continuations == (continuation,)
    assert result.requests == ()
    assert result.events == ()


@given(now=st.floats(min_value=0, max_value=9.99, allow_nan=False, allow_infinity=False))
def test_early_deadline_never_authorizes_or_cancels(now: float) -> None:
    state, continuation = fixture()
    state = state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)})
        }
    )
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
    state = state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)})
        }
    )
    authority = core.RequestId(root="park") if disposition == "park" else None
    if authority is not None:
        owner = state.attempts.attempts[0]
        close = core.CloseAttemptScope(
            request_id=authority,
            scope=state.sessions.invocations[0].scope,
            deadline_at=100.0,
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
