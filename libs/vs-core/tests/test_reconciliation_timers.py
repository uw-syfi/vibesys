"""New cleanup intents retain bounded future timers after the run expires."""

from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core


def cleanup_evaluation(
    state: core.EvaluationState, context: core.EvaluationContext, event: core.EvaluationEvent
) -> core.AreaChange[core.EvaluationState]:
    """A pure implementation of bounded cancellation for this kernel contract."""
    if not isinstance(event, core.JobTerminationRequested):
        raise core.ContractValidationError("event", "expected owned-job termination")
    owner = next(
        (
            job
            for job in (*state.jobs, *state.registered_jobs)
            if job.resource_id == event.resource_id
        ),
        None,
    )
    if owner is None:
        raise core.ContractValidationError("resource_id", "unknown owned job")
    request = core.CancelOwnedJob(
        scope=owner.scope,
        resource_id=event.resource_id,
        deadline_at=context.run.now_at + context.run.limits.cancellation_bound,
    )
    return core.AreaChange(state=state, requests=(request,))


@given(
    now=st.integers(min_value=0, max_value=10000),
    run_deadline=st.integers(min_value=0, max_value=10000),
    bound=st.integers(min_value=1, max_value=1000),
)
@example(now=100, run_deadline=99, bound=60)
def test_cleanup_registration_never_creates_past_timers(
    now: int, run_deadline: int, bound: int
) -> None:
    state = core.initial_state()
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "now_at": float(now),
                    "deadline_at": float(run_deadline),
                    "limits": state.run.limits.model_copy(
                        update={"cancellation_bound": float(bound)}
                    ),
                }
            )
        }
    )
    owner = core.RegisteredOwnedJob(
        operation_id=core.OperationId(root="operation:owned-job"),
        request_id=core.RequestId(root="submission"),
        scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
        resource_pool=core.PoolId(root="jobs"),
        resource_id=core.ResourceId(root="job"),
    )
    state = state.model_copy(update={"evaluation": core.EvaluationState(registered_jobs=(owner,))})
    before = state.model_dump_json()
    event = core.JobTerminationRequested(resource_id=core.ResourceId(root="job"), cause="deadline")
    result = core.step(state, event, reducers=core.CoreReducers(evaluation=cleanup_evaluation))
    assert len(result.requests) == 1
    request = result.requests[0]
    intent = result.state.intents.intents[0]
    assert request.deadline_at == now + bound
    assert intent.reconcile_deadline_at >= now
    if run_deadline <= now:
        assert intent.reconcile_deadline_at == request.deadline_at
    else:
        assert intent.reconcile_deadline_at == min(request.deadline_at, run_deadline)
    assert core.CoreState.model_validate_json(result.state.model_dump_json()) == result.state
    assert state.model_dump_json() == before
