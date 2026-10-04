"""Property tests of the intent ledger through the public `step` only."""

from __future__ import annotations

from typing import Literal

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api import (
    Capabilities,
    ContractError,
    ContractValidationError,
    CoreState,
    DispatchAuthorized,
    EventId,
    ExecuteRegisteredOperation,
    IntentPhase,
    LifecycleClass,
    Observation,
    ObservationStatus,
    OperationDescriptor,
    OperationId,
    OperationSchemaRef,
    OperationWire,
    RequestId,
    RequestObserved,
    RequestPrepared,
    SchemaRef,
    Scope,
    step,
)

from .test_falsification import initial_state

type Action = tuple[Literal["dispatch", "reload", "observe"], int, str]

REQUEST = RequestId(root="write")
KINDS = ("unknown", "retry", "success")


def _ready() -> CoreState:
    state = initial_state()
    schema = OperationSchemaRef(
        kind="project.artifact.put",
        request_schema=SchemaRef(name="artifact-put", version=1),
        outcome_schema=SchemaRef(name="artifact-put-outcome", version=1),
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
    )
    descriptor = OperationDescriptor(**schema.model_dump(mode="python"), inspect=True)
    request = ExecuteRegisteredOperation(
        request_id=REQUEST,
        scope=Scope(owner=state.run.run_id, generation=0),
        deadline_at=100.0,
        operation_id=OperationId(root="operation"),
        operation=OperationWire(schema_ref=schema, payload_json='{"content":"x"}'),
        retry_limit=2,
    )
    state = state.model_copy(
        update={
            "registry": (descriptor,),
            "run": state.run.model_copy(
                update={"capabilities": Capabilities(operations=(descriptor,))}
            ),
        }
    )
    return step(
        state, RequestPrepared(request=request, lifecycle=LifecycleClass.IDEMPOTENT_WRITE)
    ).state


def _observation(state: CoreState, sequence: int, kind: str) -> Observation:
    status, terminal = {
        "unknown": (ObservationStatus.UNKNOWN, False),
        "retry": (ObservationStatus.FAILED, False),
        "success": (ObservationStatus.SUCCEEDED, True),
    }[kind]
    return Observation(
        event_id=EventId(root=f"{kind}-{sequence}"),
        request_id=REQUEST,
        scope=Scope(owner=state.run.run_id, generation=0),
        sequence=sequence,
        observed_at=float(sequence),
        status=status,
        accepted=kind == "success",
        terminal=terminal,
        released=terminal,
        children_complete=terminal,
    )


def _event(state: CoreState, action: Action) -> core.CoreEvent | None:
    name, sequence, kind = action
    if name == "dispatch":
        return DispatchAuthorized(request_id=REQUEST)
    if name == "observe":
        return RequestObserved(observation=_observation(state, sequence, kind))
    return None


def _try(state: CoreState, event: core.CoreEvent) -> core.Transition | None:
    try:
        return step(state, event)
    except (ContractError, ContractValidationError):
        return None


actions = st.lists(
    st.tuples(
        st.sampled_from(("dispatch", "reload", "observe", "observe")),
        st.integers(min_value=1, max_value=4),
        st.sampled_from(KINDS),
    ),
    max_size=14,
)


@settings(max_examples=150, deadline=None)
@given(actions)
def test_ledger_interleavings_are_safe_and_reload_transparent(trace: list[Action]) -> None:
    plain = reloaded = _ready()
    completions = 0
    best = 0
    for action in trace:
        if action[0] == "reload":
            reloaded = CoreState.model_validate_json(reloaded.model_dump_json())
            assert reloaded == plain
            continue
        event = _event(plain, action)
        assert event is not None
        left = _try(plain, event)
        right = _try(reloaded, event)
        assert (left is None) == (right is None)
        if left is None or right is None:
            continue
        assert left == right
        plain, reloaded = left.state, right.state
        completions += sum(
            isinstance(row, core.OperationResult | core.DecisionCompleted) for row in left.events
        )
        (intent,) = plain.intents.intents
        if intent.observation is not None:
            # An accepted observation never moves the sequence backward or repeats it.
            assert intent.observation.sequence >= best
            best = intent.observation.sequence
        if intent.phase == IntentPhase.COMPLETED:
            assert intent.observation is not None
            assert intent.observation.terminal
    assert completions <= 1
    assert plain == reloaded


@settings(max_examples=60, deadline=None)
@given(st.integers(min_value=1, max_value=4))
def test_duplicate_and_stale_observations_are_inert(sequence: int) -> None:
    state = step(_ready(), DispatchAuthorized(request_id=REQUEST)).state
    first = step(state, RequestObserved(observation=_observation(state, sequence, "unknown")))
    again = step(first.state, RequestObserved(observation=_observation(state, sequence, "unknown")))
    assert again.state.intents == first.state.intents
    assert again.events == again.requests == ()
    if sequence > 1:
        stale = step(
            first.state,
            RequestObserved(observation=_observation(state, sequence - 1, "unknown")),
        )
        assert stale.state.intents == first.state.intents


def _obs(
    sequence: int,
    status: ObservationStatus,
    *,
    terminal: bool = False,
    resource: str | None = None,
    event: str | None = None,
) -> Observation:
    return Observation(
        event_id=EventId(root=event or f"e-{status.value}-{sequence}"),
        request_id=REQUEST,
        scope=Scope(owner=_ready().run.run_id, generation=0),
        sequence=sequence,
        observed_at=float(sequence),
        status=status,
        accepted=terminal,
        terminal=terminal,
        released=terminal,
        children_complete=terminal,
        resource_id=None if resource is None else core.ResourceId(root=resource),
    )


def _retire(state: CoreState) -> core.Transition:
    return step(
        state,
        core.OperationRetireRequested(
            operation=core.OperationRef(operation_id=OperationId(root="operation"), generation=0),
            scope=Scope(owner=state.run.run_id, generation=0),
        ),
    )


def _dispatched() -> CoreState:
    return step(_ready(), DispatchAuthorized(request_id=REQUEST)).state


def test_retiring_unsent_work_closes_it_without_a_command() -> None:
    result = _retire(_ready())
    (intent,) = result.state.intents.intents
    assert intent.phase == IntentPhase.COMPLETED
    assert intent.observation is not None
    assert intent.observation.status == ObservationStatus.CANCELLED
    assert result.requests == ()


def test_retiring_live_work_commands_the_known_resource_else_inspects() -> None:
    blind = _retire(_dispatched())
    (inspect,) = blind.requests
    assert isinstance(inspect, core.InspectRequest)
    assert inspect.target == REQUEST

    state = _dispatched()
    pending = _obs(1, ObservationStatus.PENDING, resource="job")
    seen = step(state, RequestObserved(observation=pending)).state
    (cancel,) = _retire(seen).requests
    assert isinstance(cancel, core.CancelOwnedResource)
    assert cancel.resource_id == core.ResourceId(root="job")


def test_retiring_released_terminal_work_is_quiet() -> None:
    state = _dispatched()
    done = _obs(1, ObservationStatus.SUCCEEDED, terminal=True)
    closed = step(state, RequestObserved(observation=done)).state
    assert _retire(closed).requests == ()


def test_unknown_work_can_be_dispatched_again_and_terminal_work_cannot() -> None:
    state = _dispatched()
    unknown = step(state, RequestObserved(observation=_obs(1, ObservationStatus.UNKNOWN))).state
    assert unknown.intents.intents[0].phase == IntentPhase.RECONCILING
    again = step(unknown, DispatchAuthorized(request_id=REQUEST)).state
    assert again.intents.intents[0].phase == IntentPhase.DISPATCHED
    done = _obs(2, ObservationStatus.SUCCEEDED, terminal=True)
    closed = step(again, RequestObserved(observation=done)).state
    assert closed.intents.intents[0].phase == IntentPhase.COMPLETED
    assert _try(closed, DispatchAuthorized(request_id=REQUEST)) is None


def test_conflicting_or_premature_observations_are_typed_rejections() -> None:
    fresh = _ready()
    # A request that was never sent cannot have been observed.
    assert _try(fresh, RequestObserved(observation=_obs(1, ObservationStatus.PENDING))) is None
    state = _dispatched()
    first = step(
        state,
        RequestObserved(observation=_obs(2, ObservationStatus.PENDING, event="a")),
    ).state
    clash = _obs(2, ObservationStatus.PENDING, event="b")
    assert _try(first, RequestObserved(observation=clash)) is None


def test_retries_exhaust_into_a_blocked_intent() -> None:
    state = _dispatched()
    for sequence in (1, 2, 3):
        failed = _obs(sequence, ObservationStatus.FAILED)
        state = step(state, RequestObserved(observation=failed)).state
    assert state.intents.intents[0].phase == IntentPhase.BLOCKED


def test_a_prepared_request_replays_inertly_and_conflicts_loudly() -> None:
    state = _ready()
    (intent,) = state.intents.intents
    assert isinstance(intent.request, ExecuteRegisteredOperation)
    replay = step(
        state,
        RequestPrepared(request=intent.request, lifecycle=LifecycleClass.IDEMPOTENT_WRITE),
    )
    assert replay.state.intents == state.intents
    assert replay.requests == ()
    changed = intent.request.model_copy(
        update={
            "operation": intent.request.operation.model_copy(
                update={"payload_json": '{"content":"y"}'}
            )
        }
    )
    assert (
        _try(state, RequestPrepared(request=changed, lifecycle=LifecycleClass.IDEMPOTENT_WRITE))
        is None
    )


def _job_requests(scope: Scope) -> list[core.Request]:
    resource = core.ResourceId(root="job")
    return [
        core.ObserveOwnedJob(
            request_id=REQUEST, scope=scope, deadline_at=100.0, resource_id=resource
        ),
        core.InspectOwnedJob(
            request_id=REQUEST, scope=scope, deadline_at=100.0, resource_id=resource
        ),
        core.CancelOwnedJob(
            request_id=REQUEST, scope=scope, deadline_at=100.0, resource_id=resource
        ),
        core.CollectEvidence(
            request_id=REQUEST, scope=scope, deadline_at=100.0, resource_id=resource
        ),
        core.CloseSession(
            request_id=REQUEST,
            scope=scope,
            deadline_at=100.0,
            session_id=core.SessionId(root="session"),
        ),
    ]


@given(st.integers(min_value=0, max_value=4))
def test_every_request_class_round_trips_through_the_ledger(index: int) -> None:
    base = initial_state()
    request = _job_requests(Scope(owner=base.run.run_id, generation=0))[index]
    prepared = _try(
        base,
        RequestPrepared(
            request=request,
            lifecycle=(
                LifecycleClass.IDEMPOTENT_WRITE
                if isinstance(request, core.CloseSession | core.CancelOwnedJob)
                else LifecycleClass.QUERY
            ),
        ),
    )
    assert prepared is not None
    sent = step(prepared.state, DispatchAuthorized(request_id=REQUEST)).state
    done = _obs(1, ObservationStatus.SUCCEEDED, terminal=True, resource="job")
    closed = step(sent, RequestObserved(observation=done)).state
    (intent,) = closed.intents.intents
    assert intent.phase == IntentPhase.COMPLETED
    assert intent.observation == done


def _dependent() -> CoreState:
    state = _ready()
    (intent,) = state.intents.intents
    request = intent.request.model_copy(
        update={
            "request_id": RequestId(root="dependent"),
            "decision_dependencies": (core.DecisionId(root="upstream"),),
        }
    )
    return step(
        state, RequestPrepared(request=request, lifecycle=LifecycleClass.IDEMPOTENT_WRITE)
    ).state


@given(st.sampled_from(list(core.CompletionStatus)))
def test_only_an_unsuccessful_upstream_decision_cancels_prepared_dependents(
    status: core.CompletionStatus,
) -> None:
    state = _dependent()
    resolved = step(
        state,
        core.DecisionDependencyResolved(
            decision_id=core.DecisionId(root="upstream"), status=status
        ),
    ).state
    dependent = next(
        row for row in resolved.intents.intents if row.request_id == RequestId(root="dependent")
    )
    expected = (
        IntentPhase.PREPARED if status == core.CompletionStatus.SUCCEEDED else IntentPhase.COMPLETED
    )
    assert dependent.phase == expected
