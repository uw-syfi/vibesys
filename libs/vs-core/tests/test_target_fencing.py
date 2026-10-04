"""Inspection facts retain canonical owner, resource and causal metadata."""

from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel

import vs_core.api as core

ORIGINAL_EPISODE = core.DecisionId(root="original-episode")


class Outcome(core.Value):
    complete: bool


class Alpha(core.OperationRequest):
    kind: Literal["test.alpha"] = "test.alpha"
    lifecycle: Literal[core.LifecycleClass.IDEMPOTENT_WRITE] = core.LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = Outcome


class Beta(core.OperationRequest):
    kind: Literal["test.beta"] = "test.beta"
    lifecycle: Literal[core.LifecycleClass.IDEMPOTENT_WRITE] = core.LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = Outcome


def codec() -> core.OperationRegistry:
    return core.OperationRegistry(
        tuple(
            core.OperationRegistration(
                descriptor=core.OperationDescriptor(
                    kind=model.model_fields["kind"].default,
                    lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
                    request_schema=core.SchemaRef(name=model.__name__, version=1),
                    outcome_schema=core.SchemaRef(name="outcome", version=1),
                ),
                request_model=model,
                outcome_model=Outcome,
            )
            for model in (Alpha, Beta)
        )
    )


def observation(
    scope: core.Scope, request: str, resource: core.ResourceId | None = None
) -> core.Observation:
    return core.Observation(
        event_id=core.EventId(root=request),
        request_id=core.RequestId(root=request),
        scope=scope,
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
        resource_id=resource,
    )


def prepare(state: core.CoreState, requests: tuple[core.Request, ...]) -> core.CoreState:
    recovery = core.RecoveryStarted(epoch=0, now_at=0.0)
    return core.trace_step(
        state,
        recovery,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=recovery,
                    change=core.IntentsChange(state=state.intents, requests=requests),
                ),
            )
        ),
    ).state


def registered_fixture() -> tuple[core.OperationRegistry, core.CoreState, core.RequestObserved]:
    state = core.initial_state()
    registry = codec()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    source = core.ExecuteRegisteredOperation(
        request_id=core.RequestId(root="original"),
        scope=scope,
        deadline_at=100.0,
        operation_id=core.OperationId(root="alpha"),
        operation=registry.encode(Alpha()),
        retry_limit=0,
    )
    query = core.InspectRequest(
        request_id=core.RequestId(root="query"),
        scope=scope,
        deadline_at=100.0,
        target=core.RequestId(root="original"),
    )
    state = state.model_copy(update={"registry": registry.descriptors})
    state = prepare(state, (source, query))
    return (
        registry,
        state,
        core.RequestObserved(
            observation=observation(scope, "query"),
            target=core.TargetObservation(observation=observation(scope, "original")),
        ),
    )


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("field", ["outcome", "outcome_json", "operation_schema", "outcome_schema"])
def test_each_unregistered_outcome_field_is_rejected_before_any_mutation(
    *, nested: bool, field: str
) -> None:
    registry, state, event = registered_fixture()
    values = {
        "outcome": Outcome(complete=True),
        "outcome_json": '{"complete":true}',
        "operation_schema": registry.encode(Alpha()).schema_ref,
        "outcome_schema": core.SchemaRef(name="outcome", version=1),
    }
    if nested:
        assert event.target is not None
        event = event.model_copy(
            update={"target": event.target.model_copy(update={field: values[field]})}
        )
    else:
        event = event.model_copy(update={field: values[field]})
    with pytest.raises(core.ContractError, match="proof"):
        core.step(state, event)


@pytest.mark.parametrize("nested", [False, True])
def test_registry_valid_outcome_cannot_substitute_a_different_canonical_descriptor(
    *, nested: bool
) -> None:
    registry, state, event = registered_fixture()
    fields = {
        "operation_schema": registry.encode(Beta()).schema_ref,
        "outcome_schema": core.SchemaRef(name="outcome", version=1),
        "outcome": Outcome(complete=True),
    }
    assert event.target is not None
    if nested:
        event = event.model_copy(update={"target": event.target.model_copy(update=fields)})
    else:
        event = core.RequestObserved(
            observation=event.target.observation,
            operation_schema=registry.encode(Beta()).schema_ref,
            outcome_schema=core.SchemaRef(name="outcome", version=1),
            outcome=Outcome(complete=True),
        )
    event = registry.validate_event(event)
    with pytest.raises(core.ContractError, match="canonical operation"):
        core.step(state, event)


@pytest.mark.parametrize("field", ["request_id", "scope", "resource_id"])
def test_generic_inspection_rejects_uncorrelated_target(field: str) -> None:
    _, state, event = registered_fixture()
    assert event.target is not None
    changes = {
        "request_id": core.RequestId(root="unrelated"),
        "scope": core.Scope(owner=state.run.run_id, generation=1),
        "resource_id": core.ResourceId(root="wrong-child"),
    }
    target = event.target
    if field == "resource_id":
        target = target.model_copy(
            update={
                "target_resource": changes[field],
                "observation": target.observation.model_copy(update={field: changes[field]}),
            }
        )
    else:
        target = target.model_copy(
            update={"observation": target.observation.model_copy(update={field: changes[field]})}
        )
    with pytest.raises(core.ContractError, match="target"):
        core.step(state, event.model_copy(update={"target": target}))


def turn(scope: core.Scope) -> core.TurnSpec:
    return core.TurnSpec(
        session=core.SessionSpec(
            session_id=core.SessionId(root="session"),
            role_id=core.RoleId(root="role"),
            policy="fresh",
            lifetime="owner",
            access=core.Access.WRITE_CANDIDATE,
        ),
        invocation_id=core.InvocationId(root="invocation"),
        workspace=scope,
        prompts=(),
        output_schema=core.SchemaRef(name="output", version=1),
        deadline_at=100.0,
        charge_class="paid",
    )


def turn_fixture(
    admission_id: core.DecisionId | None = ORIGINAL_EPISODE,
) -> tuple[core.CoreState, core.RequestObserved, core.InvocationRef]:
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    spec = turn(scope)
    invocation = core.InvocationRef(
        session_id=spec.session.session_id, invocation_id=spec.invocation_id, generation=3
    )
    original = core.DispatchTurn(
        request_id=core.RequestId(root="original"),
        scope=scope,
        deadline_at=100.0,
        turn=spec,
        decision_id=core.DecisionId(root="original-cause"),
        decision_dependencies=(core.DecisionId(root="original-dependency"),),
        admission_id=admission_id,
    )
    query = core.InspectTurn(
        request_id=core.RequestId(root="query"),
        scope=scope,
        deadline_at=100.0,
        invocation=invocation,
        decision_id=core.DecisionId(root="query-cause"),
        decision_dependencies=(core.DecisionId(root="query-dependency"),),
        admission_id=core.DecisionId(root="query-episode"),
    )
    state = state.model_copy(
        update={
            "sessions": core.SessionsState(
                invocations=(
                    core.Invocation(
                        invocation=invocation,
                        scope=scope,
                        turn=spec,
                        phase=core.SessionPhase.EXECUTING,
                    ),
                )
            )
        }
    )
    state = prepare(state, (original, query))
    event = core.RequestObserved(
        observation=observation(scope, "query"),
        target=core.TargetObservation(observation=observation(scope, "original")),
    )
    return state, event, invocation


def test_turn_inspection_cannot_name_an_unrelated_original_invocation() -> None:
    state, event, invocation = turn_fixture()
    state = state.model_copy(
        update={
            "sessions": state.sessions.model_copy(
                update={
                    "invocations": (
                        state.sessions.invocations[0].model_copy(
                            update={
                                "invocation": invocation.model_copy(
                                    update={"invocation_id": core.InvocationId(root="other")}
                                )
                            }
                        ),
                    )
                }
            )
        }
    )
    with pytest.raises(core.ContractError, match="invocation"):
        core.step(state, event)


def test_inspected_turn_successors_restore_original_cause_dependencies_and_episode() -> None:
    state, event, invocation = turn_fixture()
    assert event.target is not None
    signal = core.TurnObserved(invocation=invocation, observation=event.target.observation)
    request = core.InspectTurn(
        scope=signal.observation.scope, deadline_at=100.0, invocation=invocation
    )
    result = core.trace_step(
        state,
        event,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=event, change=core.IntentsChange(state=state.intents, signals=(signal,))
                ),
                core.TraceFrame(
                    signal=signal,
                    change=core.SessionsChange(state=state.sessions, requests=(request,)),
                ),
            )
        ),
    )
    successor = result.requests[0]
    assert successor.decision_id == core.DecisionId(root="original-cause")
    assert successor.decision_dependencies == (core.DecisionId(root="original-dependency"),)
    assert successor.admission_id == core.DecisionId(root="original-episode")


@pytest.mark.parametrize("inspection", ["request", "job"])
def test_known_child_cannot_masquerade_as_root_target(inspection: str) -> None:
    _, state, event = registered_fixture()
    assert event.target is not None
    child = core.ResourceId(root="child")
    original = state.intents.intents[0]
    root_observation = observation(
        original.request.scope, "original", core.ResourceId(root="root")
    ).model_copy(update={"children": (child,)})
    original = original.model_copy(update={"observation": root_observation})
    query = state.intents.intents[1]
    if inspection == "job":
        query = query.model_copy(
            update={
                "request": core.InspectOwnedJob(
                    request_id=query.request_id,
                    scope=query.request.scope,
                    deadline_at=100.0,
                    resource_id=child,
                )
            }
        )
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (original, query)})}
    )
    target = event.target.model_copy(
        update={"observation": event.target.observation.model_copy(update={"resource_id": child})}
    )
    with pytest.raises(core.ContractError, match="root facts"):
        core.step(state, event.model_copy(update={"target": target}))


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("field", ["scope", "admission_id"])
def test_observation_cannot_claim_another_owner_or_admission_episode(
    *, nested: bool, field: str
) -> None:
    state, event, _ = turn_fixture()
    assert event.target is not None
    wrong = {
        "scope": core.Scope(owner=state.run.run_id, generation=1),
        "admission_id": core.DecisionId(root="new-episode"),
    }[field]
    if nested:
        assert event.target is not None
        event = event.model_copy(
            update={
                "target": event.target.model_copy(
                    update={
                        "observation": event.target.observation.model_copy(update={field: wrong})
                    }
                )
            }
        )
    else:
        event = core.RequestObserved(
            observation=event.target.observation.model_copy(update={field: wrong})
        )
    with pytest.raises(core.ContractError, match=r"scope|episode"):
        core.step(state, event)


@pytest.mark.parametrize("manifest", ["intent", "job", "job_observation", "lease"])
def test_missing_root_identity_does_not_let_known_child_masquerade_as_root(manifest: str) -> None:
    _, state, event = registered_fixture()
    assert event.target is not None
    child = core.ResourceId(root="child")
    original, query = state.intents.intents
    scope = original.request.scope
    discovered = observation(scope, "original").model_copy(update={"children": (child,)})
    if manifest == "intent":
        original = original.model_copy(update={"observation": discovered})
    elif manifest in ("job", "job_observation"):
        job = core.RegisteredOwnedJob(
            operation_id=core.OperationId(root="alpha"),
            request_id=original.request_id,
            scope=scope,
            resource_pool=core.PoolId(root="jobs"),
            children=(child,) if manifest == "job" else (),
            observation=discovered if manifest == "job_observation" else None,
        )
        state = state.model_copy(
            update={"evaluation": core.EvaluationState(registered_jobs=(job,))}
        )
    else:
        state = state.model_copy(
            update={
                "intents": state.intents.model_copy(
                    update={
                        "children": (
                            core.ChildLease(
                                resource_id=child,
                                scope=scope,
                                source_requests=(original.request_id,),
                            ),
                        )
                    }
                )
            }
        )
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (original, query)})}
    )
    target = event.target.model_copy(
        update={"observation": event.target.observation.model_copy(update={"resource_id": child})}
    )
    with pytest.raises(core.ContractError, match="discriminator"):
        core.step(state, event.model_copy(update={"target": target}))


def test_inspection_job_resource_must_match_the_recorded_owner() -> None:
    _, state, event = registered_fixture()
    assert event.target is not None
    original, query = state.intents.intents
    resource = core.ResourceId(root="job")
    original = original.model_copy(
        update={"observation": observation(original.request.scope, "original", resource)}
    )
    query = query.model_copy(
        update={
            "request": core.InspectOwnedJob(
                request_id=query.request_id,
                scope=query.request.scope,
                deadline_at=100.0,
                resource_id=core.ResourceId(root="unrelated"),
            )
        }
    )
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (original, query)})}
    )
    event = event.model_copy(
        update={
            "target": event.target.model_copy(
                update={
                    "observation": event.target.observation.model_copy(
                        update={"resource_id": resource}
                    )
                }
            )
        }
    )
    with pytest.raises(core.ContractError, match="job owner"):
        core.step(state, event)


def test_query_episode_cannot_replace_an_original_targets_absent_episode() -> None:
    state, event, invocation = turn_fixture(None)
    assert event.target is not None
    signal = core.TurnObserved(invocation=invocation, observation=event.target.observation)
    request = core.InspectTurn(
        scope=signal.observation.scope, deadline_at=100.0, invocation=invocation
    )
    result = core.trace_step(
        state,
        event,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=event, change=core.IntentsChange(state=state.intents, signals=(signal,))
                ),
                core.TraceFrame(
                    signal=signal,
                    change=core.SessionsChange(state=state.sessions, requests=(request,)),
                ),
            )
        ),
    )
    assert result.requests[0].admission_id is None
