"""Kernel finality cannot accept missing or unrelated physical release facts."""

import hashlib
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


def digest(value: core.Value) -> str:
    return hashlib.sha256(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def stopped(state: core.CoreState) -> core.CoreState:
    proposal = core.RunResultProposal(outcome="cancelled", reason="cleanup")
    stop = core.Stop(
        decision_id=core.DecisionId(root="stop"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        result=proposal,
        mode="drain",
    )
    return state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.CLOSING,
                    "result": proposal,
                    "receipts": (
                        core.DecisionReceipt(
                            decision_id=stop.decision_id,
                            decision=stop,
                            payload_digest=digest(stop),
                            feedback=core.Accepted(decision_id=stop.decision_id),
                        ),
                    ),
                }
            )
        }
    )


def drain(state: core.CoreState) -> core.Transition:
    codec = core.OperationRegistry()
    envelope = core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=0),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )
    restored = codec.decode_envelope(type(envelope), codec.encode_envelope(envelope)).core
    assert restored == state
    clock = core.ClockAdvanced(now_at=state.run.now_at + 1.0)
    result = core.trace_step(
        restored,
        clock,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=clock,
                    change=core.SchedulingChange(
                        state=restored.scheduling, signals=(core.RunDrained(),)
                    ),
                ),
            )
        ),
    )
    assert restored == state
    return result


def released_job(identity: int) -> tuple[core.CoreState, core.OwnedJob, core.Intent]:
    state = stopped(core.initial_state())
    scope = core.Scope(owner=core.AttemptId(root=f"attempt:{identity}"), generation=identity)
    plan = core.MeasurementPlan(
        purpose="official",
        candidate=state.run.facts.baseline,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        stages=(core.MeasurementStage(stage_id="stage", execution_budget=1.0),),
        policy="ordered",
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="recipe"),
        submitted_at=0.0,
        queue_allowance=0.0,
        deadline_at=1.0,
    )
    request = core.SubmitMeasurement(
        request_id=core.RequestId(root=f"submission:{identity}"),
        scope=scope,
        admission_id=core.DecisionId(root=f"episode:{identity}"),
        deadline_at=1.0,
        plan=plan,
    )
    assert request.request_id is not None
    observation = core.Observation(
        event_id=core.EventId(root="release"),
        request_id=request.request_id,
        scope=scope,
        admission_id=request.admission_id,
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
        resource_id=core.ResourceId(root="resource"),
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
    )
    assert observation.resource_id is not None
    job = core.OwnedJob(
        resource_id=observation.resource_id,
        submission_id=request.request_id,
        scope=scope,
        plan=plan,
        observation=observation,
        status=observation.status,
        terminal=True,
        released=True,
    )
    source = core.Intent(
        request_id=request.request_id,
        request=request,
        payload_digest=digest(request),
        lifecycle=core.LifecycleClass.OWNED_JOB,
        phase=core.IntentPhase.COMPLETED,
        observation=observation,
        reconcile_deadline_at=1.0,
    )
    return state, job, source


@pytest.mark.parametrize("variant", ["exact", "missing_episode", "old_episode", "wrong_status"])
@given(identity=st.integers(min_value=0, max_value=10000))
def test_run_finality_requires_job_source_episode_and_status(variant: str, identity: int) -> None:
    state, job, source = released_job(identity)
    observation = job.observation
    assert observation is not None
    if variant == "missing_episode":
        job = job.model_copy(
            update={"observation": observation.model_copy(update={"admission_id": None})}
        )
    elif variant == "old_episode":
        job = job.model_copy(
            update={
                "observation": observation.model_copy(
                    update={"admission_id": core.DecisionId(root="old")}
                )
            }
        )
    elif variant == "wrong_status":
        job = job.model_copy(update={"status": core.ObservationStatus.FAILED})
    state = state.model_copy(
        update={
            "evaluation": core.EvaluationState(jobs=(job,)),
            "intents": state.intents.model_copy(update={"intents": (source,)}),
        }
    )
    result = drain(state)
    assert result.state.run.status == (
        core.RunStatus.TERMINAL if variant == "exact" else core.RunStatus.CLOSING
    )
    assert any(isinstance(event, core.RunEnded) for event in result.events) == (variant == "exact")


@pytest.mark.parametrize(
    "status",
    [
        core.ObservationStatus.SUCCEEDED,
        core.ObservationStatus.REJECTED,
        core.ObservationStatus.FAILED,
        core.ObservationStatus.CANCELLED,
    ],
)
@given(identity=st.integers(min_value=0, max_value=10000))
def test_accepted_session_turn_without_resource_keeps_run_closing(
    status: core.ObservationStatus, identity: int
) -> None:
    state, _, source = released_job(identity)
    session = core.SessionSpec(
        session_id=core.SessionId(root=f"session:{identity}"),
        role_id=core.RoleId(root="role"),
        policy="fresh",
        lifetime="ephemeral",
        access=core.Access.READ_ONLY,
    )
    request = core.DispatchTurn(
        request_id=source.request_id,
        scope=source.request.scope,
        admission_id=source.request.admission_id,
        deadline_at=100.0,
        turn=core.TurnSpec(
            session=session,
            invocation_id=core.InvocationId(root="invocation"),
            workspace=source.request.scope,
            prompts=(),
            output_schema=core.SchemaRef(name="output", version=1),
            deadline_at=100.0,
            charge_class="free",
        ),
    )
    assert source.observation is not None
    observation = source.observation.model_copy(
        update={
            "resource_id": None,
            "status": status,
            "accepted": status == core.ObservationStatus.SUCCEEDED,
        }
    )
    intent = source.model_copy(
        update={
            "request": request,
            "payload_digest": digest(request),
            "lifecycle": core.LifecycleClass.SESSION_TURN,
            "observation": observation,
        }
    )
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (intent,)})}
    )
    result = drain(state)
    negative = status != core.ObservationStatus.SUCCEEDED
    assert result.state.run.status == (
        core.RunStatus.TERMINAL if negative else core.RunStatus.CLOSING
    )
    assert any(isinstance(event, core.RunEnded) for event in result.events) == negative


@given(identity=st.integers(min_value=0, max_value=10000))
def test_complete_empty_cleanup_debt_can_end_run(identity: int) -> None:
    state = stopped(core.initial_state())
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"now_at": float(identity)})}
    )
    result = drain(state)
    assert result.state.run.status == core.RunStatus.TERMINAL
    assert isinstance(result.events[-1], core.RunEnded)


@pytest.mark.parametrize("kind", ["intent", "child"])
@pytest.mark.parametrize("variant", ["exact", "missing_episode", "old_episode"])
@given(identity=st.integers(min_value=0, max_value=10000))
def test_every_ownership_source_keeps_its_recorded_episode(
    kind: str, variant: str, identity: int
) -> None:
    state, _, source = released_job(identity)
    observation = source.observation
    assert observation is not None
    if variant != "exact":
        observation = observation.model_copy(
            update={
                "admission_id": None
                if variant == "missing_episode"
                else core.DecisionId(root="old")
            }
        )
    if kind == "intent":
        source = source.model_copy(update={"observation": observation})
        intents = state.intents.model_copy(update={"intents": (source,)})
    else:
        assert observation.resource_id is not None
        child = core.ChildLease(
            resource_id=observation.resource_id,
            scope=observation.scope,
            source_requests=(source.request_id,),
            observation=observation,
            observation_watermarks=(
                core.ChildObservationWatermark(
                    source_request=source.request_id, observation=observation
                ),
            ),
            watermark_history_complete=True,
        )
        intents = state.intents.model_copy(update={"intents": (source,), "children": (child,)})
    state = state.model_copy(update={"intents": intents})
    result = drain(state)
    assert result.state.run.status == (
        core.RunStatus.TERMINAL if variant == "exact" else core.RunStatus.CLOSING
    )
    assert any(isinstance(event, core.RunEnded) for event in result.events) == (variant == "exact")
