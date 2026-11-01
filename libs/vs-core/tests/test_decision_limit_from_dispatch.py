"""Any wait's limit counts from the dispatch of its work, not from the ask.

A turn's limit is covered by ``test_turn_limit_from_dispatch``. The other waits that go through
the shell's slot cap are the same: an operation (the agent's prompt render, a parent
verification, a patch export), a workspace request, and a measurement. Each is held only to the
run deadline while it is queued, and its limit starts when the shell dispatches it. A
measurement's later polls belong to the same wait: they end where its submission does and never
restart the limit.
"""

from typing import ClassVar, Literal

from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core
from vs_core.testing.limits import reach

from .test_measurements import observation, requested
from .wait_support import OPEN

_LIMIT = st.integers(min_value=1, max_value=90).map(float)
_QUEUED = st.integers(min_value=0, max_value=5000).map(float)


class Written(core.Value):
    done: bool


class Write(core.OperationRequest):
    kind: Literal["test.dispatch.write"] = "test.dispatch.write"
    lifecycle: Literal[core.LifecycleClass.IDEMPOTENT_WRITE] = core.LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = Written


def _operation(limit: float) -> tuple[core.CoreState, core.Request]:
    descriptor = core.OperationDescriptor(
        kind="test.dispatch.write",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        request_schema=core.SchemaRef(name="write", version=1),
        outcome_schema=core.SchemaRef(name="written", version=1),
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=Write, outcome_model=Written
            ),
        )
    )
    state = core.initial_state()
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "deadline_at": 100_000.0,
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                }
            ),
        }
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="write"),
            scope=core.Scope(owner=state.run.run_id, generation=0),
            request=Write(),
        )
    )
    result = core.step(
        state,
        core.DecisionSubmitted(
            decision=core.Wait(request=decision, until=core.Within(limit=limit)),
            expected_revision=state.revision,
        ),
    )
    [request] = [r for r in result.requests if isinstance(r, core.ExecuteRegisteredOperation)]
    return result.state, request


def _record(state: core.CoreState, request: core.Request) -> core.RequestRecord:
    return next(row for row in state.records.records if row.request_id == request.request_id)


def _blocked_at(state: core.CoreState, request: core.Request, at: float) -> bool:
    after = reach(state, at).state
    return _record(after, request).phase not in OPEN


@given(limit=_LIMIT, queued=_QUEUED)
def test_a_queued_operation_spends_none_of_its_limit(limit: float, queued: float) -> None:
    state, request = _operation(limit)
    assert request.request_id is not None

    state = reach(state, queued).state
    assert _record(state, request).phase == core.RequestPhase.PREPARED

    state = core.step(
        state, core.DispatchAuthorized(request_id=request.request_id, now_at=queued)
    ).state
    assert not _blocked_at(state, request, queued + limit - 1.0)
    assert _blocked_at(state, request, queued + limit)


@given(queued=st.integers(min_value=0, max_value=800).map(float))
def test_a_measurements_limit_starts_at_its_submission_and_its_polls_do_not_restart_it(
    queued: float,
) -> None:
    result = requested()
    submit = result.requests[0]
    assert isinstance(submit, core.SubmitMeasurement)
    assert submit.request_id is not None
    state = core.step(result.state, core.ClockAdvanced(now_at=queued)).state
    assert _record(state, submit).phase == core.RequestPhase.PREPARED

    state = core.step(
        state, core.DispatchAuthorized(request_id=submit.request_id, now_at=queued)
    ).state
    limit_at = _record(state, submit).until.at
    assert limit_at == queued + 100.0

    first = observation(submit, 1, observed_at=queued + 1.0)
    state = core.step(state, core.RequestObserved(observation=first)).state
    resource = core.ResourceId(root="job")
    state = core.step(state, core.JobObserved(resource_id=resource, observation=first)).state
    due = core.project(state).next_observe_at
    assert due is not None
    tick = core.step(state, core.ClockAdvanced(now_at=max(due, queued + 1.0)))
    polls = [r for r in tick.requests if isinstance(r, core.ObserveOwnedJob)]
    assert polls
    # A poll has a bound of its own and does not move the submission's limit.
    assert _record(tick.state, submit).until.at == limit_at
