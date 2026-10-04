"""Complete root manifests cannot erase provisional or discovered ownership."""

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


def released_observation(scope: core.Scope, resource: core.ResourceId | None) -> core.Observation:
    return core.Observation(
        event_id=core.EventId(root="released"),
        request_id=core.RequestId(root="submission"),
        scope=scope,
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
        resource_id=resource,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
    )


def close_run(state: core.CoreState) -> core.RunStatus:
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.CLOSING,
                    "result": core.RunResultProposal(outcome="cancelled", reason="cleanup"),
                }
            )
        }
    )
    clock = core.ClockAdvanced(now_at=1.0)
    result = core.trace_step(
        state,
        clock,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=clock,
                    change=core.SchedulingChange(
                        state=state.scheduling, signals=(core.RunDrained(),)
                    ),
                ),
            )
        ),
    )
    return result.state.run.status


@given(
    status=st.sampled_from(list(core.ObservationStatus)),
    accepted=st.booleans(),
    identified=st.booleans(),
)
def test_registered_job_missing_identity_or_unknown_facts_retain_provisional_ownership(
    status: core.ObservationStatus, *, accepted: bool, identified: bool
) -> None:
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    resource = core.ResourceId(root="job") if identified else None
    observation = released_observation(scope, resource).model_copy(
        update={"status": status, "accepted": accepted}
    )
    job = core.RegisteredOwnedJob(
        operation_id=core.OperationId(root="operation"),
        request_id=observation.request_id,
        scope=scope,
        resource_pool=core.PoolId(root="jobs"),
        resource_id=resource,
        status=status,
        terminal=True,
        released=True,
        observation=observation,
    )
    state = state.model_copy(update={"evaluation": core.EvaluationState(registered_jobs=(job,))})
    conclusive = status not in (core.ObservationStatus.UNKNOWN, core.ObservationStatus.PENDING)
    nonownership = not accepted and status in (
        core.ObservationStatus.REJECTED,
        core.ObservationStatus.FAILED,
        core.ObservationStatus.CANCELLED,
    )
    expected = (
        core.RunStatus.TERMINAL
        if conclusive and (identified or nonownership)
        else core.RunStatus.CLOSING
    )
    assert close_run(state) == expected


@given(
    source=st.sampled_from(["stored", "observed"]),
    child_released=st.booleans(),
    matching_scope=st.booleans(),
)
def test_builtin_root_release_keeps_discovered_child_until_exact_child_release(
    source: str, *, child_released: bool, matching_scope: bool
) -> None:
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    root, child_id = core.ResourceId(root="job"), core.ResourceId(root="child")
    observation = released_observation(scope, root).model_copy(
        update={"children": (child_id,) if source == "observed" else ()}
    )
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
    job = core.OwnedJob(
        resource_id=root,
        submission_id=observation.request_id,
        scope=scope,
        observation=observation,
        children=(child_id,) if source == "stored" else (),
        plan=plan,
        status=core.ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
    )
    child_scope = scope if matching_scope else core.Scope(owner=state.run.run_id, generation=1)
    child_observation = released_observation(child_scope, child_id) if child_released else None
    child = core.ChildLease(
        resource_id=child_id,
        scope=child_scope,
        source_requests=(observation.request_id,),
        observation=child_observation,
        observation_watermarks=(
            (
                core.ChildObservationWatermark(
                    source_request=child_observation.request_id, observation=child_observation
                ),
            )
            if child_observation is not None
            else ()
        ),
        watermark_history_complete=child_observation is not None,
    )
    state = state.model_copy(
        update={
            "evaluation": core.EvaluationState(jobs=(job,)),
            "intents": state.intents.model_copy(update={"children": (child,)}),
        }
    )
    expected = (
        core.RunStatus.TERMINAL if child_released and matching_scope else core.RunStatus.CLOSING
    )
    assert close_run(state) == expected


@given(field=st.sampled_from(["request_id", "scope", "resource_id"]))
def test_released_job_requires_exact_observation_correspondence(field: str) -> None:
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    resource = core.ResourceId(root="job")
    observation = released_observation(scope, resource)
    observation = observation.model_copy(
        update={
            field: {
                "request_id": core.RequestId(root="wrong"),
                "scope": core.Scope(owner=state.run.run_id, generation=1),
                "resource_id": core.ResourceId(root="wrong"),
            }[field]
        }
    )
    job = core.RegisteredOwnedJob(
        operation_id=core.OperationId(root="operation"),
        request_id=core.RequestId(root="submission"),
        scope=scope,
        resource_pool=core.PoolId(root="jobs"),
        resource_id=resource,
        terminal=True,
        released=True,
        observation=observation,
    )
    state = state.model_copy(update={"evaluation": core.EvaluationState(registered_jobs=(job,))})
    assert close_run(state) == core.RunStatus.CLOSING


@given(pending_child=st.booleans())
def test_inspection_command_success_cannot_release_its_parent_targets_child(
    *, pending_child: bool
) -> None:
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    root, child = core.ResourceId(root="parent"), core.ResourceId(root="child")
    observation = released_observation(scope, root)
    job = core.RegisteredOwnedJob(
        operation_id=core.OperationId(root="parent"),
        request_id=observation.request_id,
        scope=scope,
        resource_pool=core.PoolId(root="jobs"),
        resource_id=root,
        terminal=True,
        released=True,
        children=(child,),
        observation=observation,
    )
    query = core.InspectRequest(
        scope=scope, deadline_at=100.0, target=observation.request_id, resource_id=child
    )
    clock = core.ClockAdvanced(now_at=0.0)
    state = core.trace_step(
        state,
        clock,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=clock,
                    change=core.SchedulingChange(state=state.scheduling, requests=(query,)),
                ),
            )
        ),
    ).state
    intent = state.intents.intents[0]
    command = released_observation(scope, child).model_copy(
        update={"request_id": intent.request_id}
    )
    intent = intent.model_copy(update={"phase": core.IntentPhase.COMPLETED, "observation": command})
    children = (
        (core.ChildLease(resource_id=child, scope=scope, source_requests=(job.request_id,)),)
        if pending_child
        else ()
    )
    state = state.model_copy(
        update={
            "evaluation": core.EvaluationState(registered_jobs=(job,)),
            "intents": state.intents.model_copy(
                update={"intents": (intent,), "children": children}
            ),
        }
    )
    assert close_run(state) == core.RunStatus.CLOSING
