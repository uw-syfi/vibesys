"""Executor and publication boundaries: leases, failed outcomes, owner events, routing."""

from __future__ import annotations

import typing
from enum import StrEnum
from types import UnionType
from typing import TYPE_CHECKING, Annotated, get_args, get_origin

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from tests.support.runtime_core_shell import CounterState, CounterStrategy, ShellTraceTransitions

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    ClockAdvanced,
    ContractError,
    CoreEvent,
    CoreState,
    DecisionId,
    EnsureSession,
    EventId,
    InputId,
    IntentPhase,
    Observation,
    ObservationStatus,
    Request,
    RequestId,
    RequestObserved,
    Scope,
    ScopeInputTarget,
    SessionId,
    SessionInput,
    SessionInputReceived,
    SessionObserved,
    Transition,
    initial_state,
)
from vs_project.api import FakeStateStore, StoredEnvelope
from vs_runtime.api.core import (
    REQUEST_DISPATCH,
    CoreRuntime,
    CoreRuntimeBindings,
    DispatchProgress,
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    ExecutorRefusal,
    ExecutorRole,
    ObservationRejectedError,
    OwnerEvent,
    OwnerEventRejectedError,
    Publication,
    PublicationAcknowledgement,
    PublicationContext,
    PublicationDelivery,
    RequestExecutors,
    RuntimeCommitError,
    RuntimeExecutionError,
)
from vs_runtime.api.testing import FakePublicationDelivery, FakeRequestExecution

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_runtime.api.core import ExecutionLease


class ScriptedExecution:
    """Fake translator: wraps the faithful fake, then lets a test script the outcome."""

    def __init__(
        self,
        script: Callable[[Request, ExecutionContext, ExecutionResult], Awaitable[ExecutionOutcome]]
        | None = None,
    ) -> None:
        self.inner = FakeRequestExecution(ExecutorRole.SESSIONS)
        self._script = script

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionOutcome:
        result = await self.inner.execute(request, context)
        return result if self._script is None else await self._script(request, context, result)


def make_shell(
    store: FakeStateStore, sessions: ScriptedExecution | None = None
) -> CoreRuntime[CounterState]:
    return CoreRuntime(
        store,
        CounterStrategy(),
        initial_state(),
        bindings=CoreRuntimeBindings(
            transitions=ShellTraceTransitions(with_requests=True),
            executors=RequestExecutors(
                sessions=sessions or ScriptedExecution(),
                operations=FakeRequestExecution(ExecutorRole.OPERATIONS),
            ),
        ),
    )


def occurrence(index: int) -> SessionInputReceived:
    return SessionInputReceived(
        input=SessionInput(
            input_id=InputId(root=f"owner-input-{index}"),
            target=ScopeInputTarget(scope=Scope(owner=initial_state().run.run_id, generation=0)),
            artifact=ArtifactRef(artifact_id=ArtifactId(root="content"), digest="digest"),
            received_at=index,
            sequence=index,
        )
    )


def with_owner_events(result: ExecutionResult, *events: OwnerEvent) -> ExecutionResult:
    return result.model_copy(update={"owner_events": events})


async def prepared(shell: CoreRuntime[CounterState], store: FakeStateStore) -> None:
    del store
    shell.start("first", now_at=0, lease_duration=100)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    assert shell.advance()


# P1-1 ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_is_renewable_while_an_executor_is_in_flight() -> None:
    store = FakeStateStore()
    observed: list[bool] = []

    async def long_request(
        request: Request, context: ExecutionContext, result: ExecutionResult
    ) -> ExecutionOutcome:
        del request
        lease: ExecutionLease | None = context.lease
        assert lease is not None
        lease.renew(now_at=90, lease_duration=100)
        # The original lease (100) has expired but the renewed one (190) has not.
        observed.append(store.acquire("rival", now=150, duration=10) is None)
        observed.append(lease.verify(now_at=150))
        return result

    shell = make_shell(store, ScriptedExecution(long_request))
    await prepared(shell, store)
    assert await shell.dispatch_one(now_at=1) == DispatchProgress.DISPATCHED
    assert observed == [True, True]
    assert shell.advance()
    assert shell.record.envelope.core.intents.intents[0].phase == IntentPhase.RECONCILING


@pytest.mark.asyncio
async def test_shell_renew_is_not_rejected_as_busy_during_dispatch() -> None:
    store = FakeStateStore()
    holder: list[CoreRuntime[CounterState]] = []

    async def renewing(
        request: Request, context: ExecutionContext, result: ExecutionResult
    ) -> ExecutionOutcome:
        del request, context
        holder[0].renew(now_at=50, lease_duration=100)
        return result

    shell = make_shell(store, ScriptedExecution(renewing))
    holder.append(shell)
    await prepared(shell, store)
    assert await shell.dispatch_one(now_at=1) == DispatchProgress.DISPATCHED


@pytest.mark.asyncio
async def test_lease_is_renewable_while_a_publication_is_in_flight() -> None:
    store = FakeStateStore()
    shell = make_shell(store)
    await prepared(shell, store)
    rivals: list[bool] = []

    class Renewing:
        async def publish(
            self, publication: Publication, context: PublicationContext
        ) -> PublicationAcknowledgement:
            assert context.lease is not None
            context.lease.renew(now_at=90, lease_duration=100)
            rivals.append(store.acquire("rival", now=150, duration=10) is None)
            return PublicationAcknowledgement(
                publication_id=publication.publication_id, sequence=publication.sequence
            )

    delivery: PublicationDelivery = Renewing()
    assert await shell.publish_one(delivery, now_at=1)
    assert rivals == [True]


# P2-1 ---------------------------------------------------------------------------------


class Failure(StrEnum):
    RAISES = "raises"
    OTHER_REQUEST = "other_request"
    REFUSES_AFTER_AUTHORIZATION = "refuses_after_authorization"


def wrong_request_result(result: ExecutionResult) -> ExecutionResult:
    observation = result.observation.observation.model_copy(
        update={"request_id": RequestId(root="someone-else")}
    )
    return result.model_copy(
        update={"observation": result.observation.model_copy(update={"observation": observation})}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", list(Failure))
async def test_failed_execution_halts_and_restart_inspects_the_stranded_intent(
    failure: Failure,
) -> None:
    store = FakeStateStore()

    async def broken(
        request: Request, context: ExecutionContext, result: ExecutionResult
    ) -> ExecutionOutcome:
        del context
        match failure:
            case Failure.RAISES:
                message = "executor lost its connection"
                raise OSError(message)
            case Failure.OTHER_REQUEST:
                return wrong_request_result(result)
            case Failure.REFUSES_AFTER_AUTHORIZATION:
                assert request.request_id is not None
                return ExecutorRefusal(
                    request_id=request.request_id, role=ExecutorRole.SESSIONS, detail="cannot"
                )

    shell = make_shell(store, ScriptedExecution(broken))
    await prepared(shell, store)
    with pytest.raises((OSError, ContractError, RuntimeCommitError)):
        await shell.dispatch_one(now_at=1)
    # The intent is DISPATCHED and the shell must not report idle or keep dispatching.
    assert shell.record.envelope.core.intents.intents[0].phase == IntentPhase.DISPATCHED
    with pytest.raises(RuntimeCommitError):
        await shell.dispatch_one(now_at=2)
    with pytest.raises(RuntimeCommitError):
        await shell.run_until_idle(FakePublicationDelivery(store), now_at=2)

    restarted = make_shell(store)
    restarted.start("second", now_at=100, lease_duration=100)
    kinds = {row.request.kind: row.phase for row in restarted.record.envelope.core.intents.intents}
    assert kinds["inspect_request"] == IntentPhase.PREPARED


@pytest.mark.asyncio
async def test_unbound_role_is_refused_before_authorization_is_committed() -> None:
    store = FakeStateStore()
    shell = CoreRuntime(
        store,
        CounterStrategy(),
        initial_state(),
        bindings=CoreRuntimeBindings(transitions=ShellTraceTransitions(with_requests=True)),
    )
    shell.start("host", now_at=0, lease_duration=100)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    shell.advance()
    revision = shell.storage_revision
    for _ in range(2):
        refusal = await shell.dispatch_one(now_at=1)
        assert isinstance(refusal, ExecutorRefusal)
        assert refusal.role == ExecutorRole.SESSIONS
    assert shell.storage_revision == revision
    assert shell.record.envelope.core.intents.intents[0].phase == IntentPhase.PREPARED


# P2-2 ---------------------------------------------------------------------------------


@pytest.mark.asyncio
@given(
    count=st.integers(min_value=1, max_value=4), crash_after=st.integers(min_value=1, max_value=5)
)
async def test_observation_and_owner_events_survive_a_crash_between_their_commits(
    count: int, crash_after: int
) -> None:
    store = FakeStateStore()
    events = tuple(occurrence(index) for index in range(1, count + 1))

    async def with_events(
        request: Request, context: ExecutionContext, result: ExecutionResult
    ) -> ExecutionOutcome:
        del request, context
        return with_owner_events(result, *events)

    shell = make_shell(store, ScriptedExecution(with_events))
    await prepared(shell, store)
    assert await shell.dispatch_one(now_at=1) == DispatchProgress.DISPATCHED
    for _ in range(min(crash_after, count + 1)):
        assert shell.advance()
    restarted = make_shell(store)
    restarted.start("second", now_at=100, lease_duration=100)
    while restarted.advance():
        pass
    stored = restarted.record.envelope.core.sessions.inputs
    assert {row.input.input_id for row in stored} == {event.input.input_id for event in events}
    assert restarted.record.pending_inputs == ()
    persisted = store.load()
    assert isinstance(persisted, StoredEnvelope)


# P2-3 ---------------------------------------------------------------------------------


def foreign_event(request: Request, *, generation: int | None, admission: str | None) -> OwnerEvent:
    assert request.request_id is not None
    observation = Observation(
        event_id=EventId(root=request.request_id.root + ":foreign"),
        request_id=request.request_id,
        scope=request.scope
        if generation is None
        else Scope(owner=request.scope.owner, generation=generation),
        sequence=0,
        observed_at=1,
        status=ObservationStatus.UNKNOWN,
        admission_id=request.admission_id if admission is None else DecisionId(root=admission),
    )
    return SessionObserved(session_id=SessionId(root="session-1"), observation=observation)


@pytest.mark.asyncio
@given(
    generation=st.none() | st.integers(min_value=1, max_value=9),
    admission=st.sampled_from([None, "other"]),
)
async def test_owner_events_outside_the_executed_scope_or_admission_halt_the_shell(
    generation: int | None, admission: str | None
) -> None:
    assume(generation is not None or admission is not None)
    store = FakeStateStore()

    async def injecting(
        request: Request, context: ExecutionContext, result: ExecutionResult
    ) -> ExecutionOutcome:
        del context
        return with_owner_events(
            result, foreign_event(request, generation=generation, admission=admission)
        )

    shell = make_shell(store, ScriptedExecution(injecting))
    await prepared(shell, store)
    with pytest.raises(ContractError, match="owner event"):
        await shell.dispatch_one(now_at=1)
    # Nothing from the rejected result reached the queue or the durable record.
    with pytest.raises(RuntimeCommitError):
        shell.advance()
    assert shell.record.pending_inputs == ()
    assert shell.record.envelope.core.intents.intents[0].phase == IntentPhase.DISPATCHED


# P3-2 ---------------------------------------------------------------------------------


def variants(annotation: object) -> set[type]:
    if hasattr(annotation, "__value__"):
        return variants(annotation.__value__)
    origin = get_origin(annotation)
    if origin is Annotated:
        return variants(get_args(annotation)[0])
    if origin is UnionType:
        return set().union(*(variants(child) for child in get_args(annotation)))
    assert isinstance(annotation, type)
    return {annotation}


@pytest.mark.parametrize("role", list(ExecutorRole))
def test_each_role_protocol_accepts_exactly_the_requests_routed_to_it(role: ExecutorRole) -> None:
    protocol = typing.get_type_hints(RequestExecutors)[role.value]
    accepted = variants(typing.get_type_hints(protocol.execute)["request"])
    routed = {kind for kind, owner in REQUEST_DISPATCH.items() if owner == role}
    assert accepted == routed


def test_every_request_variant_is_routed_to_exactly_one_role() -> None:
    assert set(REQUEST_DISPATCH) == variants(Request)


@pytest.mark.asyncio
async def test_unrouted_request_type_is_a_typed_contract_error() -> None:
    store = FakeStateStore()
    shell = make_shell(store)
    await prepared(shell, store)
    request = shell.record.envelope.core.intents.intents[0].request
    assert isinstance(request, EnsureSession)

    class Unrouted(EnsureSession):
        pass

    clone = Unrouted(**{name: getattr(request, name) for name in type(request).model_fields})
    context = ExecutionContext(fence=shell.record.envelope.fence, now_at=1, payload_digest="digest")
    with pytest.raises(ContractError, match="no executor role"):
        await RequestExecutors().dispatch(clone, context)


# P3-4 ---------------------------------------------------------------------------------


class FailingDelivery:
    def __init__(self) -> None:
        self.attempts = 0

    async def publish(
        self, publication: Publication, context: PublicationContext
    ) -> PublicationAcknowledgement:
        del publication, context
        self.attempts += 1
        message = "delivery unavailable"
        raise OSError(message)


@pytest.mark.asyncio
async def test_failing_publication_does_not_block_dispatch_and_is_reported_when_idle() -> None:
    store = FakeStateStore()
    sessions = ScriptedExecution()
    shell = make_shell(store, sessions)
    await prepared(shell, store)
    delivery = FailingDelivery()
    with pytest.raises(OSError, match="delivery unavailable"):
        await shell.run_until_idle(delivery, now_at=1)
    assert len(sessions.inner.executions) == 1
    assert delivery.attempts == 1
    assert shell.record.pending_publications


@pytest.mark.asyncio
async def test_rejected_input_is_dropped_without_halting_the_shell() -> None:
    store = FakeStateStore()
    shell = make_shell(store)
    shell.start("host", now_at=0, lease_duration=100)
    shell.submit(occurrence(1), now_at=1)
    assert shell.advance()
    conflicting = occurrence(1).input.model_copy(update={"sequence": 99})
    shell.submit(SessionInputReceived(input=conflicting), now_at=2)
    revision = shell.storage_revision
    with pytest.raises(ContractError, match="identity"):
        shell.advance()
    assert not shell.advance()
    assert shell.storage_revision == revision
    shell.submit(ClockAdvanced(now_at=3), now_at=3)
    assert shell.advance()


class RejectingObservations(ShellTraceTransitions):
    """The production trace kernel, except core refuses every executor observation.

    This is what core's freshness proof does to an observation whose sequence is
    not newer than the request's history (``Mismatch(SEQUENCE)``).
    """

    def step(self, state: CoreState, event: CoreEvent) -> Transition:
        if isinstance(event, RequestObserved):
            raise ContractError(("observation", "sequence"), "Mismatch(SEQUENCE)")
        return super().step(state, event)


@pytest.mark.asyncio
async def test_an_observation_core_rejects_halts_the_shell_naming_the_request() -> None:
    store = FakeStateStore()
    sessions = ScriptedExecution()
    shell = CoreRuntime(
        store,
        CounterStrategy(),
        initial_state(),
        bindings=CoreRuntimeBindings(
            transitions=RejectingObservations(with_requests=True),
            executors=RequestExecutors(
                sessions=sessions, operations=FakeRequestExecution(ExecutorRole.OPERATIONS)
            ),
        ),
    )
    await prepared(shell, store)
    (intent,) = shell.record.envelope.core.intents.intents
    delivery = FakePublicationDelivery(store)
    with pytest.raises(RuntimeExecutionError) as raised:
        await shell.run_until_idle(delivery, now_at=1)
    assert isinstance(raised.value, ObservationRejectedError)
    assert raised.value.request_id == intent.request_id
    assert raised.value.path == ("observation", "sequence")
    assert "Mismatch(SEQUENCE)" in str(raised.value)
    # The effect ran once; the result is not dropped into an idle shell.
    assert len(sessions.inner.executions) == 1
    with pytest.raises(RuntimeCommitError):
        await shell.run_until_idle(delivery, now_at=2)
    assert shell.record.envelope.core.intents.intents[0].phase == IntentPhase.DISPATCHED


class RejectingOwnerInputs(ShellTraceTransitions):
    """The production trace kernel, except core refuses every owner session input."""

    def step(self, state: CoreState, event: CoreEvent) -> Transition:
        if isinstance(event, SessionInputReceived):
            raise ContractError(("input", "input_id"), "refused")
        return super().step(state, event)


@pytest.mark.asyncio
@given(count=st.integers(min_value=1, max_value=4))
async def test_an_owner_event_core_rejects_halts_the_shell_instead_of_being_dropped(
    count: int,
) -> None:
    store = FakeStateStore()
    events = tuple(occurrence(index) for index in range(1, count + 1))

    async def with_events(
        request: Request, context: ExecutionContext, result: ExecutionResult
    ) -> ExecutionOutcome:
        del request, context
        return with_owner_events(result, *events)

    shell = CoreRuntime(
        store,
        CounterStrategy(),
        initial_state(),
        bindings=CoreRuntimeBindings(
            transitions=RejectingOwnerInputs(with_requests=True),
            executors=RequestExecutors(
                sessions=ScriptedExecution(with_events),
                operations=FakeRequestExecution(ExecutorRole.OPERATIONS),
            ),
        ),
    )
    await prepared(shell, store)
    assert await shell.dispatch_one(now_at=1) == DispatchProgress.DISPATCHED
    assert shell.advance()
    with pytest.raises(OwnerEventRejectedError, match="refused"):
        shell.advance()
    with pytest.raises(RuntimeCommitError):
        shell.advance()
    # The rejected event is still durable: a restart cannot lose it silently either.
    assert len(shell.record.pending_inputs) == count
