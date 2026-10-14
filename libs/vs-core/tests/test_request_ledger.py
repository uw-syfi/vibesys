"""Property tests of the record ledger through the public `step` only."""

from typing import ClassVar, Literal

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core
from vs_core.api import (
    Capabilities,
    ContractError,
    ContractValidationError,
    CoreState,
    DispatchAuthorized,
    EventId,
    ExecuteRegisteredOperation,
    LifecycleClass,
    Observation,
    ObservationStatus,
    OperationDescriptor,
    OperationId,
    OperationSchemaRef,
    OperationWire,
    RequestId,
    RequestObserved,
    RequestPhase,
    RequestPrepared,
    SchemaRef,
    Scope,
    Until,
    step,
)
from vs_sim.api.testing import examples

from .test_falsification import initial_state

type Action = tuple[Literal["dispatch", "reload", "observe"], int, str]

REQUEST = RequestId(root="write")
KINDS = ("unknown", "retry", "success")


class WriteOutcome(core.Value):
    status: Literal["succeeded"] = "succeeded"


class ArtifactPut(core.OperationRequest):
    kind: Literal["test.write"] = "test.write"
    lifecycle: Literal[core.LifecycleClass.IDEMPOTENT_WRITE] = core.LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = WriteOutcome
    content: str


def operation_state() -> tuple[core.OperationRegistry, core.CoreState]:
    descriptor = core.OperationDescriptor(
        kind="test.write",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        request_schema=core.SchemaRef(name="write", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
        inspect=True,
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=ArtifactPut, outcome_model=WriteOutcome
            ),
        )
    )
    state = core.initial_state()
    return codec, state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={"capabilities": core.Capabilities(operations=codec.descriptors)}
            ),
        }
    )


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
        state,
        RequestPrepared(
            until=Until(at=100.0), request=request, lifecycle=LifecycleClass.IDEMPOTENT_WRITE
        ),
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


@settings(max_examples=examples(pr=5, nightly=150), deadline=None)
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
        (record,) = plain.records.records
        if record.observation is not None:
            # An accepted observation never moves the sequence backward or repeats it.
            assert record.observation.sequence >= best
            best = record.observation.sequence
        if record.phase == RequestPhase.COMPLETED:
            assert record.observation is not None
            assert record.observation.terminal
    assert completions <= 1
    assert plain == reloaded


@settings(max_examples=examples(pr=5, nightly=60), deadline=None)
@given(st.integers(min_value=1, max_value=4))
def test_duplicate_and_stale_observations_are_inert(sequence: int) -> None:
    state = step(_ready(), DispatchAuthorized(request_id=REQUEST)).state
    first = step(state, RequestObserved(observation=_observation(state, sequence, "unknown")))
    again = step(first.state, RequestObserved(observation=_observation(state, sequence, "unknown")))
    assert again.state.records == first.state.records
    assert again.events == again.requests == ()
    if sequence > 1:
        stale = step(
            first.state,
            RequestObserved(observation=_observation(state, sequence - 1, "unknown")),
        )
        assert stale.state.records == first.state.records


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
    (record,) = result.state.records.records
    assert record.phase == RequestPhase.COMPLETED
    assert record.observation is not None
    assert record.observation.status == ObservationStatus.CANCELLED
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
    assert unknown.records.records[0].phase == RequestPhase.RECONCILING
    again = step(unknown, DispatchAuthorized(request_id=REQUEST)).state
    assert again.records.records[0].phase == RequestPhase.DISPATCHED
    done = _obs(2, ObservationStatus.SUCCEEDED, terminal=True)
    closed = step(again, RequestObserved(observation=done)).state
    assert closed.records.records[0].phase == RequestPhase.COMPLETED
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


def test_retries_exhaust_into_a_conclusive_failure() -> None:
    """Failures the executor calls retryable end as a terminal failure once the bound is used."""
    state = _dispatched()
    for sequence in (1, 2, 3):
        failed = _obs(sequence, ObservationStatus.FAILED)
        state = step(state, RequestObserved(observation=failed)).state
    (record,) = state.records.records
    assert record.phase == RequestPhase.COMPLETED
    assert record.observation is not None
    assert record.observation.terminal
    assert record.observation.status == ObservationStatus.FAILED
    assert not record.observation.accepted


def test_a_prepared_request_replays_inertly_and_conflicts_loudly() -> None:
    state = _ready()
    (record,) = state.records.records
    assert isinstance(record.request, ExecuteRegisteredOperation)
    replay = step(
        state,
        RequestPrepared(
            until=Until(at=100.0), request=record.request, lifecycle=LifecycleClass.IDEMPOTENT_WRITE
        ),
    )
    assert replay.state.records == state.records
    assert replay.requests == ()
    changed = record.request.model_copy(
        update={
            "operation": record.request.operation.model_copy(
                update={"payload_json": '{"content":"y"}'}
            )
        }
    )
    assert (
        _try(
            state,
            RequestPrepared(
                until=Until(at=100.0), request=changed, lifecycle=LifecycleClass.IDEMPOTENT_WRITE
            ),
        )
        is None
    )


def _job_requests(scope: Scope) -> list[core.Request]:
    resource = core.ResourceId(root="job")
    return [
        core.ObserveOwnedJob(request_id=REQUEST, scope=scope, resource_id=resource),
        core.InspectOwnedJob(request_id=REQUEST, scope=scope, resource_id=resource),
        core.CancelOwnedJob(request_id=REQUEST, scope=scope, resource_id=resource),
        core.CollectEvidence(request_id=REQUEST, scope=scope, resource_id=resource),
        core.CloseSession(
            request_id=REQUEST,
            scope=scope,
            session_id=core.SessionId(root="session"),
        ),
        core.DiscardWorkspace(
            request_id=REQUEST,
            scope=scope,
            attempt=core.AttemptRef(attempt_id=core.AttemptId(root="a"), generation=0),
        ),
        core.CloseAttemptScope(
            request_id=REQUEST,
            scope=scope,
            attempt=core.AttemptRef(attempt_id=core.AttemptId(root="a"), generation=0),
        ),
        core.EnsureSession(
            request_id=REQUEST,
            scope=scope,
            spec=core.SessionSpec(
                session_id=core.SessionId(root="session"),
                role_id=core.RoleId(root="worker"),
                policy="reuse",
                lifetime="owner",
                access=core.Access.READ_ONLY,
            ),
        ),
    ]


@given(st.integers(min_value=0, max_value=7))
def test_every_request_class_round_trips_through_the_ledger(index: int) -> None:
    base = initial_state()
    request = _job_requests(Scope(owner=base.run.run_id, generation=0))[index]
    prepared = _try(
        base,
        RequestPrepared(
            until=Until(at=100.0),
            request=request,
            lifecycle=(
                LifecycleClass.QUERY
                if isinstance(
                    request, core.ObserveOwnedJob | core.InspectOwnedJob | core.CollectEvidence
                )
                else LifecycleClass.IDEMPOTENT_WRITE
            ),
        ),
    )
    assert prepared is not None
    sent = step(prepared.state, DispatchAuthorized(request_id=REQUEST)).state
    done = _obs(1, ObservationStatus.SUCCEEDED, terminal=True, resource="job")
    closed = step(sent, RequestObserved(observation=done)).state
    (record,) = closed.records.records
    assert record.phase == RequestPhase.COMPLETED
    assert record.observation == done


def test_events_naming_unknown_requests_or_wrong_classes_are_rejected() -> None:
    state = _ready()
    (record,) = state.records.records
    wrong = RequestPrepared(
        until=Until(at=100.0), request=record.request, lifecycle=LifecycleClass.QUERY
    )
    assert _try(state, wrong) is None
    assert _try(initial_state(), DispatchAuthorized(request_id=REQUEST)) is None
    ghost = _obs(1, ObservationStatus.PENDING)
    assert _try(initial_state(), RequestObserved(observation=ghost)) is None
    other = core.OperationRetireRequested(
        operation=core.OperationRef(operation_id=OperationId(root="missing"), generation=0),
        scope=Scope(owner=state.run.run_id, generation=0),
    )
    assert _try(state, other) is None


def test_a_registered_write_completes_its_decision_and_reports_once() -> None:
    codec, state = operation_state()
    scope = Scope(owner=state.run.run_id, generation=0)
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="write-decision"),
            scope=scope,
            request=ArtifactPut(content="payload"),
        )
    )
    accepted = step(
        state,
        core.DecisionSubmitted(
            decision=core.Wait(request=decision, until=core.Within(limit=100.0)),
            expected_revision=0,
        ),
    )
    (request,) = accepted.requests
    assert isinstance(request, ExecuteRegisteredOperation)
    assert request.request_id is not None
    sent = step(accepted.state, DispatchAuthorized(request_id=request.request_id)).state
    observation = Observation(
        event_id=EventId(root="done"),
        request_id=request.request_id,
        scope=scope,
        sequence=1,
        observed_at=1.0,
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
    )
    schema = request.operation.schema_ref
    event = RequestObserved.model_validate(
        {
            "observation": observation,
            "operation_schema": schema,
            "outcome_schema": schema.outcome_schema,
            "outcome_json": codec.encode_outcome(schema, WriteOutcome()),
        },
        context={"operation_registry": codec},
    )
    done = step(sent, event)
    receipts = [r for r in done.state.run.receipts if r.decision_id == decision.decision_id]
    results = [row for row in done.events if isinstance(row, core.OperationResult)]
    assert [r.completion for r in receipts] == [core.CompletionStatus.SUCCEEDED]
    assert len(results) == 1
    again = step(done.state, event)
    assert again.events == again.requests == ()


@given(st.lists(st.integers(min_value=1, max_value=6), min_size=1, max_size=6, unique=True))
def test_the_record_always_records_the_latest_accepted_observation(sequences: list[int]) -> None:
    """Owners compare against the record's current observation, so it must follow progress."""
    state = _dispatched()
    latest = 0
    for sequence in sequences:
        observation = _obs(sequence, ObservationStatus.PENDING, resource="job")
        state = step(state, RequestObserved(observation=observation)).state
        (record,) = state.records.records
        latest = max(latest, sequence)
        assert record.observation is not None
        assert record.observation.sequence == latest
