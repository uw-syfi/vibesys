"""Shared public contracts for runtime-owned prepared conversations."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from tests.support.runtime_agent_sessions import (
    _client,
    _ClientFactory,
    _environment,
    _EnvironmentOpener,
    _resume_transport,
    _runtime,
    _RuntimeEffects,
)

from vibesys.orchestration.structured_turn import structured_turn
from vs_agent.api import (
    AgentInvocationRecord,
    AgentInvocationState,
    AgentSessionCheckpoint,
    AgentSessionKey,
    AgentTurnResult,
    Completed,
    InvalidResponse,
    InvocationConflictError,
    Pending,
    SessionPersistenceError,
    Unknown,
)
from vs_agent.api.testing import FakeAgentInvocationStore
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import (
    AgentCapability,
    AgentConversationOpenError,
    AgentConversationRequest,
    AgentRole,
    InvocationRelease,
    SessionClosedError,
    SessionResumeError,
)
from vs_runtime.api.testing import (
    FakeAgentExecutionLifecycleSink,
    FakeWorkspace,
    FakeWorkspaceAgentSessions,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from vs_agent.api import AgentClientProtocol, AgentSessions
    from vs_runtime.api import AgentSession, Workspace, WorkspaceAgentSessions


ROLE = AgentRole(
    id="worker",
    system_prompt="Work carefully.",
    required_capabilities=frozenset({AgentCapability.DURABLE_TURN_CONTINUATION}),
)


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_reconstructed_session_replays_recorded_reply_but_fences_new_turn_without_proof(
    implementation: str,
) -> None:
    async def scenario() -> None:
        store = FakeAgentInvocationStore()
        original = _harness(implementation, store)
        session = await original.owner.create_session(
            ROLE, workspace=original.workspace, member_id="member"
        )
        try:
            reply = await session.turn("work", invocation_id="initial")
            assert isinstance(session.inspect("initial"), Completed)
        finally:
            await original.close()
        reconstructed = _harness(implementation, store)
        reopened = await reconstructed.owner.create_session(
            ROLE, workspace=reconstructed.workspace, member_id="member"
        )
        try:
            assert await reopened.turn("work", invocation_id="initial") == reply
            with pytest.raises(
                SessionResumeError, match="acknowledged provider checkpoint is missing"
            ):
                await reopened.turn("more", invocation_id="later")
            state = store.load_optional()
            assert state is not None
            assert set(state.invocations) == {"initial"}
        finally:
            await reconstructed.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_ambiguous_legacy_checkpoints_fence_new_turn_before_dispatch(implementation: str) -> None:
    async def scenario() -> None:
        store = FakeAgentInvocationStore()
        harness = _harness(implementation, store)
        session = await harness.owner.create_session(
            ROLE, workspace=harness.workspace, member_id="member"
        )
        try:
            assert await session.turn("work", invocation_id="initial")
            state = store.load_optional()
            assert state is not None
            original = state.invocations["initial"]
            conflicting = Completed(
                session_key=str(session.session_key),
                invocation_id="conflicting",
                checkpoint=AgentSessionCheckpoint(
                    session_key=str(session.session_key), provider_session_id="different"
                ),
                result=AgentTurnResult("older reply", provider_session_id="different"),
            )
            store.save(
                AgentInvocationState(
                    invocations={
                        "initial": original.model_copy(update={"sequence": 0}),
                        "conflicting": AgentInvocationRecord(
                            payload_digest="legacy", outcome=conflicting
                        ),
                    }
                )
            )
            with pytest.raises(SessionResumeError, match="checkpoint identity changed"):
                await session.turn("more", invocation_id="later")
            current = store.load_optional()
            assert current is not None
            assert set(current.invocations) == {"initial", "conflicting"}
        finally:
            await harness.close()

    asyncio.run(scenario())


class _OpeningGate:
    """A deterministic construction barrier shared across async and threaded owners."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.loop = asyncio.get_running_loop()

    def wait_sync(self) -> None:
        self.loop.call_soon_threadsafe(self.entered.set)
        self.release.wait()

    async def wait(self) -> None:
        self.entered.set()
        waiting = asyncio.create_task(asyncio.to_thread(self.release.wait))
        cancelled = False
        while not waiting.done():
            try:
                await asyncio.shield(waiting)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError


class _OpeningFake(FakeWorkspaceAgentSessions):
    def __init__(self, gate: _OpeningGate) -> None:
        super().__init__(
            (ROLE,),
            supported_agent_capabilities={
                AgentCapability.DURABLE_TURN_CONTINUATION,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        self.gate = gate

    async def create_session(
        self,
        role: AgentRole,
        *,
        workspace: Workspace,
        member_id: str | None = None,
        generation: int | None = None,
        writable_paths: tuple[str, ...] = (),
    ) -> AgentSession:
        await self.gate.wait()
        return await super().create_session(
            role,
            workspace=workspace,
            member_id=member_id,
            generation=generation,
            writable_paths=writable_paths,
        )


@dataclass
class _Opening:
    failure: BaseException | None = None
    gate: _OpeningGate | None = None
    turn_gate: _OpeningGate | None = None
    transport: AgentSessions | None = None
    reply: dict[str, object] | None = None


@dataclass
class _Harness:
    owner: WorkspaceAgentSessions
    workspace: Workspace
    close: Callable[[], Awaitable[None]]
    opened: Callable[[], int]
    resources_closed: Callable[[], bool]

    def request(
        self, invocation_id: str = "initial", generation: int | None = None
    ) -> AgentConversationRequest:
        return AgentConversationRequest(
            role=ROLE,
            workspace=self.workspace,
            member_id="member",
            generation=generation,
            invocation_id=invocation_id,
        )


def _fake_harness(store: FakeAgentInvocationStore, opening: _Opening) -> _Harness:

    async def respond(
        _role: AgentRole, _history: tuple[str, ...], message: str, _response: object
    ) -> object:
        if opening.turn_gate is not None:
            await opening.turn_gate.wait()
            raise asyncio.CancelledError
        return opening.reply if opening.reply is not None else message

    owner = (
        _OpeningFake(opening.gate)
        if opening.gate is not None
        else FakeWorkspaceAgentSessions(
            (ROLE,),
            responder=respond,
            supported_agent_capabilities={
                AgentCapability.DURABLE_TURN_CONTINUATION,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
    )
    owner.bind_invocation_store(store)
    if opening.transport is not None:
        owner.bind_session_transport(opening.transport)
    if opening.failure is not None:
        owner.script_creation(opening.failure)
    return _Harness(
        owner,
        FakeWorkspace(),
        owner.close,
        lambda: len(owner.sessions),
        lambda: all(session.closed for session in owner.sessions),
    )


def _harness(
    implementation: str, store: FakeAgentInvocationStore, opening: _Opening | None = None
) -> _Harness:
    opening = opening or _Opening()
    if implementation == "fake":
        return _fake_harness(store, opening)

    prepared_clients = [
        _client().enqueue("worker", opening.reply)
        if opening.reply is not None
        else _client(responses=("done",))
        for _ in range(4)
    ]

    def hold_turn(_request: object) -> None:
        if opening.turn_gate is not None:
            opening.turn_gate.wait_sync()
            raise asyncio.CancelledError

    for client in prepared_clients:
        client.on_invoke(hold_turn)
    clients = _ClientFactory(*prepared_clients)

    def open_client(**kwargs: object) -> AgentClientProtocol:
        if opening.gate is not None:
            opening.gate.wait_sync()
        if opening.failure is not None:
            raise opening.failure
        return clients(**kwargs)

    runtime = _runtime(
        ROLE,
        _RuntimeEffects(
            open_client,
            _EnvironmentOpener(*(_environment() for _ in range(4))),
            FakeAgentExecutionLifecycleSink(),
            session_transport=opening.transport,
            invocation_store=lambda _key: store,
        ),
    )
    return _Harness(
        runtime.agents,
        runtime.workspaces.root,
        runtime.workspaces.close,
        lambda: len(clients.calls),
        lambda: all(client.closed for client in prepared_clients[: len(clients.calls)]),
    )


def _pending(store: FakeAgentInvocationStore, invocation_id: str = "initial") -> Pending:
    pending = Pending(
        session_key=str(AgentSessionKey.for_member(ROLE.id, "member")), invocation_id=invocation_id
    )
    store.save(
        AgentInvocationState(
            invocations={
                invocation_id: AgentInvocationRecord(
                    payload_digest="unacknowledged", outcome=pending
                )
            }
        )
    )
    return pending


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_prepared_inspection_recovers_pending_without_opening(implementation: str) -> None:
    store = FakeAgentInvocationStore()
    pending = _pending(store)

    async def scenario() -> None:
        harness = _harness(implementation, store)
        conversation = harness.owner.prepare_conversation(harness.request())
        try:
            observed = conversation.inspect("initial")
            assert isinstance(observed, Unknown)
            assert observed.session_key == pending.session_key
            assert observed.invocation_id == pending.invocation_id
            assert isinstance(conversation.inspect("missing"), Unknown)
            assert harness.opened() == 0
            assert conversation.role == ROLE
            assert conversation.workspace is harness.workspace
            assert conversation.member_id == "member"
            persisted = store.load_optional()
            assert persisted is not None
            assert persisted.invocations["initial"].outcome == pending
        finally:
            await conversation.close()
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_prepared_close_before_open_is_idempotent(implementation: str) -> None:
    async def scenario() -> None:
        harness = _harness(implementation, FakeAgentInvocationStore())
        conversation = harness.owner.prepare_conversation(harness.request())
        try:
            await conversation.close()
            await conversation.close()
            assert conversation.closed
            assert harness.opened() == 0
            with pytest.raises(SessionClosedError):
                conversation.authorize_release(InvocationRelease(invocation_id="initial"))
            with pytest.raises(SessionClosedError):
                await conversation.turn("must not dispatch")
            assert harness.opened() == 0
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
@pytest.mark.parametrize("failure", [RuntimeError("opening failed"), asyncio.CancelledError()])
def test_failed_prepared_open_preserves_inspection_and_can_close(
    implementation: str, failure: BaseException
) -> None:
    store = FakeAgentInvocationStore()
    _pending(store)

    async def scenario() -> None:
        harness = _harness(implementation, store, _Opening(failure=failure))
        conversation = harness.owner.prepare_conversation(harness.request())
        try:
            expected = (
                asyncio.CancelledError
                if isinstance(failure, asyncio.CancelledError)
                else AgentConversationOpenError
            )
            with pytest.raises(expected):
                await conversation.turn("work")
            assert isinstance(conversation.inspect("initial"), Unknown)
            await conversation.close()
            await conversation.close()
            assert conversation.closed
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_cancelled_prepared_open_drains_before_closing(implementation: str) -> None:
    async def scenario() -> None:
        gate = _OpeningGate()
        harness = _harness(implementation, FakeAgentInvocationStore(), _Opening(gate=gate))
        conversation = harness.owner.prepare_conversation(harness.request())
        turn = asyncio.create_task(conversation.turn("work"))
        entering = asyncio.create_task(gate.entered.wait())
        try:
            done, _ = await asyncio.wait((turn, entering), return_when=asyncio.FIRST_COMPLETED)
            assert entering in done, "turn ended before opening barrier"
            turn.cancel()
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await turn
            await conversation.close()
            assert conversation.closed
            assert isinstance(conversation.inspect("initial"), Unknown)
        finally:
            gate.release.set()
            entering.cancel()
            await asyncio.gather(turn, entering, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_close_during_prepared_open_never_dispatches_a_turn(implementation: str) -> None:
    store = FakeAgentInvocationStore()

    async def scenario() -> None:
        gate = _OpeningGate()
        harness = _harness(implementation, store, _Opening(gate=gate))
        conversation = harness.owner.prepare_conversation(harness.request())
        turn = asyncio.create_task(conversation.turn("must not dispatch"))
        entering = asyncio.create_task(gate.entered.wait())
        closing: asyncio.Task[None] | None = None
        try:
            done, _ = await asyncio.wait((turn, entering), return_when=asyncio.FIRST_COMPLETED)
            assert entering in done, "turn ended before opening barrier"
            close_entered = asyncio.Event()

            async def close() -> None:
                close_entered.set()
                await conversation.close()

            closing = asyncio.create_task(close())
            await close_entered.wait()
            gate.release.set()
            with pytest.raises(SessionClosedError):
                await turn
            await closing
            assert conversation.closed
            assert store.load_optional() is None
        finally:
            gate.release.set()
            entering.cancel()
            await asyncio.gather(
                turn, entering, *(() if closing is None else (closing,)), return_exceptions=True
            )
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
@pytest.mark.parametrize("withdrawn", [False, True])
def test_prepared_recovered_unknown_remains_fenced_without_local_drain(
    implementation: str, *, withdrawn: bool
) -> None:
    store = FakeAgentInvocationStore()
    _pending(store)

    async def scenario() -> None:
        harness = _harness(implementation, store)
        old = harness.owner.prepare_conversation(harness.request())
        try:
            if withdrawn:
                old.authorize_release(InvocationRelease(invocation_id="initial"))
            await old.close()
            state = store.load_optional()
            assert state is not None
            assert not state.invocations["initial"].interrupted
            assert isinstance(old.inspect("initial"), Unknown)
            successor = harness.owner.prepare_conversation(harness.request("next"))
            try:
                with pytest.raises(SessionResumeError, match="unfinished dispatch"):
                    await successor.turn("next")
            finally:
                await successor.close()
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
@pytest.mark.parametrize("withdrawn", [False, True])
@pytest.mark.parametrize("correction", [False, True])
def test_cancelled_prepared_turn_drains_before_authorized_release(
    implementation: str, *, withdrawn: bool, correction: bool
) -> None:
    store = FakeAgentInvocationStore()
    active_identity = "initial/correction" if correction else "initial"

    async def scenario() -> None:
        gate = _OpeningGate()
        opening = _Opening()
        harness = _harness(implementation, store, opening)
        conversation = harness.owner.prepare_conversation(harness.request())
        if correction:
            assert await conversation.turn("first reply")
            assert isinstance(conversation.inspect("initial"), Completed)
        opening.turn_gate = gate
        turn = asyncio.create_task(conversation.turn("work", invocation_id=active_identity))
        entering = asyncio.create_task(gate.entered.wait())
        closing: asyncio.Task[None] | None = None
        try:
            done, _ = await asyncio.wait((turn, entering), return_when=asyncio.FIRST_COMPLETED)
            assert entering in done, "turn ended before provider barrier"
            assert isinstance(conversation.inspect(active_identity), Pending)
            if withdrawn:
                conversation.authorize_release(InvocationRelease(invocation_id="initial"))
            turn.cancel()
            close_entered = asyncio.Event()

            async def close() -> None:
                close_entered.set()
                await conversation.close()

            closing = asyncio.create_task(close())
            await close_entered.wait()
            state = store.load_optional()
            assert state is not None
            assert not state.invocations[active_identity].interrupted
            assert not turn.done()
            assert not closing.done()
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await turn
            await closing
            assert conversation.closed
            assert isinstance(conversation.inspect(active_identity), Unknown)
            state = store.load_optional()
            assert state is not None
            assert state.invocations["initial"].interrupted is withdrawn
            assert state.invocations[active_identity].interrupted is withdrawn
        finally:
            gate.release.set()
            entering.cancel()
            await asyncio.gather(
                turn, entering, *(() if closing is None else (closing,)), return_exceptions=True
            )
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
@pytest.mark.parametrize("generation", [1, 2, 17])
def test_prepared_generation_reconstruction_preserves_old_unknown(
    implementation: str, generation: int
) -> None:
    store = FakeAgentInvocationStore()
    pending = _pending(store)

    async def scenario() -> None:
        harness = _harness(implementation, store)
        request = harness.request("generated", generation)
        first = harness.owner.prepare_conversation(request)
        try:
            assert first.session_key == AgentSessionKey.for_member(
                ROLE.id, "member", generation=generation
            )
            assert await first.turn("fresh")
            expected = first.inspect("generated")
            assert isinstance(expected, Completed)
            await first.close()
            reconstructed = harness.owner.prepare_conversation(request)
            try:
                assert reconstructed.inspect("generated") == expected
                assert await reconstructed.turn("rebuilt prompt") == expected.result.text
                assert harness.opened() == 1
            finally:
                await reconstructed.close()
            old = harness.owner.prepare_conversation(harness.request())
            try:
                assert isinstance(old.inspect("initial"), Unknown)
                assert old.session_key == AgentSessionKey.parse(pending.session_key)
            finally:
                await old.close()
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_prepared_bound_turn_allows_only_its_bounded_correction(implementation: str) -> None:
    store = FakeAgentInvocationStore()

    async def scenario() -> None:
        harness = _harness(implementation, store)
        conversation = harness.owner.prepare_conversation(harness.request())
        try:
            assert await conversation.turn("first")
            assert await conversation.turn("corrected", invocation_id="initial/correction")
            assert isinstance(conversation.inspect("initial"), Completed)
            assert isinstance(conversation.inspect("initial/correction"), Completed)
            for unrelated in ("other", "initial/correction/correction"):
                with pytest.raises(InvocationConflictError):
                    await conversation.turn("unrelated", invocation_id=unrelated)
            conversation.authorize_release(InvocationRelease(invocation_id="initial"))
            await conversation.close()
            state = store.load_optional()
            assert state is not None
            assert all(not record.interrupted for record in state.invocations.values())
        finally:
            await conversation.close()
            await harness.close()

    asyncio.run(scenario())


class _ReleaseFailure(FakeAgentInvocationStore):
    def save(self, model: AgentInvocationState) -> None:
        if any(record.interrupted for record in model.invocations.values()):
            message = "release commit failed"
            raise OSError(message)
        super().save(model)


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_prepared_release_commit_failure_still_closes_resources(implementation: str) -> None:
    async def scenario() -> None:
        gate = _OpeningGate()
        store = _ReleaseFailure()
        harness = _harness(implementation, store, _Opening(turn_gate=gate))
        conversation = harness.owner.prepare_conversation(harness.request())
        turn = asyncio.create_task(conversation.turn("work"))
        entering = asyncio.create_task(gate.entered.wait())
        try:
            done, _ = await asyncio.wait((turn, entering), return_when=asyncio.FIRST_COMPLETED)
            assert entering in done, "turn ended before provider barrier"
            conversation.authorize_release(InvocationRelease(invocation_id="initial"))
            turn.cancel()
            gate.release.set()
            outcomes = await asyncio.gather(turn, return_exceptions=True)
            assert isinstance(outcomes[0], SessionPersistenceError)
            with pytest.raises(SessionPersistenceError, match="release commit failed"):
                await conversation.close()
            assert harness.resources_closed()
            state = store.load_optional()
            assert state is not None
            assert not state.invocations["initial"].interrupted
        finally:
            gate.release.set()
            entering.cancel()
            await asyncio.gather(turn, entering, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_prepared_resume_opens_and_preserves_the_bound_checkpoint(
    implementation: str, tmp_path: Path
) -> None:
    transport, client = _resume_transport(tmp_path, lambda _request: None)
    message = TemplateRenderer(tmp_path).render_string("trusted continuation")
    key = AgentSessionKey.for_member(ROLE.id, "member")
    expected = transport.checkpoint(key)

    async def scenario() -> None:
        harness = _harness(
            implementation, FakeAgentInvocationStore(), _Opening(transport=transport)
        )
        conversation = harness.owner.prepare_conversation(harness.request("resume"))
        try:
            assert harness.opened() == 0
            result = await conversation.resume(message, "resume")
            assert isinstance(result, Completed)
            assert result.checkpoint == expected
            assert conversation.inspect("resume") == result
            assert await conversation.resume(message, "resume") == result
            assert harness.opened() == 1
        finally:
            await conversation.close()
            await harness.close()

    try:
        asyncio.run(scenario())
    finally:
        client.close()


class _Reply(BaseModel):
    value: int


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_prepared_structured_correction_refuses_unavailable_provider_checkpoint(
    implementation: str,
) -> None:
    store = FakeAgentInvocationStore()
    key = str(AgentSessionKey.for_member(ROLE.id, "member"))
    rejected = InvalidResponse(
        session_key=key,
        invocation_id="initial",
        detail="value must be an integer",
        checkpoint=AgentSessionCheckpoint(session_key=key, provider_session_id="existing"),
    )
    store.save(
        AgentInvocationState(
            invocations={
                "initial": AgentInvocationRecord(payload_digest="rejected", outcome=rejected)
            }
        )
    )

    async def scenario() -> None:
        harness = _harness(implementation, store, _Opening(reply={"value": 2}))
        request = harness.request()
        conversation = harness.owner.prepare_conversation(request)
        try:
            assert conversation.invocation_id == "initial"
            # The ledger alone cannot reconstruct provider history. A correction
            # must refuse this unavailable identity rather than start a fresh turn.
            with pytest.raises(SessionResumeError) as failure:
                await structured_turn(conversation, "work", _Reply)
            assert "checkpoint" in str(failure.value)
            assert "unresolved" not in str(failure.value)
            corrected = conversation.inspect("initial/correction")
            assert isinstance(corrected, Unknown)
            assert conversation.inspect("initial") == rejected
        finally:
            await conversation.close()
            await harness.close()

    asyncio.run(scenario())
