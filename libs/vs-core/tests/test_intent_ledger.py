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
