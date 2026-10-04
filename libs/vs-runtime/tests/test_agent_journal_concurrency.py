"""Public regression for initial and continuation journal mutation ownership."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from queue import SimpleQueue
from threading import Event, get_ident
from typing import TYPE_CHECKING, Literal

from vs_agent.api import (
    AgentCapabilities,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    Completed,
    Unknown,
)
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore, FakeAgentSessions
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import AgentCapability, AgentRole
from vs_runtime.api.testing import FakeWorkspace, FakeWorkspaceAgentSessions

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vs_agent.api import AgentInvocationState, AgentInvocationStore
    from vs_runtime.api import AgentSession


class _JournalSaveGate(FakeAgentInvocationStore):
    def __init__(self) -> None:
        super().__init__()
        self.saving_unknown = Event()
        self.release = Event()
        self.initial_thread: int | None = None
        self.initial_access: SimpleQueue[Literal["transaction", "read"]] = SimpleQueue()

    def load_optional(self) -> AgentInvocationState | None:
        if get_ident() == self.initial_thread:
            self.initial_access.put("read")
        return super().load_optional()

    def save(self, model: AgentInvocationState) -> None:
        record = model.invocations.get("resume")
        if (
            get_ident() != self.initial_thread
            and record is not None
            and isinstance(record.outcome, Unknown)
        ):
            self.saving_unknown.set()
            self.release.wait()
        super().save(model)


class _JournalAccessSessions(FakeAgentSessions):
    def __init__(self, client: FakeAgentClient, store: _JournalSaveGate) -> None:
        super().__init__(client, store)
        self.store = store

    @contextmanager
    def invocation_transaction(self) -> Iterator[AgentInvocationStore]:
        if get_ident() == self.store.initial_thread:
            self.store.initial_access.put("transaction")
        with super().invocation_transaction() as store:
            yield store


def _initial_turn(session: AgentSession, store: _JournalSaveGate) -> str:
    store.initial_thread = get_ident()
    return asyncio.run(session.turn("initial", invocation_id="initial"))


def test_concurrent_initial_and_unknown_resume_preserve_both_journal_entries(
    tmp_path: Path,
) -> None:
    store = _JournalSaveGate()
    client = FakeAgentClient(
        capabilities=AgentCapabilities(session_reuse=True, provider_session_resume=True)
    )
    transport = _JournalAccessSessions(client, store)
    spec = AgentSessionSpec(
        role="worker", provider="fake", workspace=tmp_path, policy=AgentExecutionPolicy()
    )
    for member in ("one", "two"):
        key = AgentSessionKey.for_member("worker", member)
        client.run(session_spec=spec, turn=AgentTurnRequest(message="seed"), session_key=key)
        transport.bind(key, spec, AgentTurnRequest(message="template"))
    detail = "lost acknowledgement"
    client.fail("worker", OSError(detail), times=1)
    message = TemplateRenderer(tmp_path).render_string("resume")

    async def scenario() -> None:
        role = AgentRole(id="worker", system_prompt="Work.")
        owner = FakeWorkspaceAgentSessions(
            (role,), supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME}
        )
        owner.bind_session_transport(transport)
        one = await owner.create_session(role, workspace=FakeWorkspace(), member_id="one")
        two = await owner.create_session(role, workspace=FakeWorkspace(), member_id="two")
        resume = asyncio.create_task(one.resume(message, "resume"))
        try:
            await asyncio.to_thread(store.saving_unknown.wait)
            with ThreadPoolExecutor(max_workers=1) as workers:
                initial = workers.submit(_initial_turn, two, store)
                access = await asyncio.to_thread(store.initial_access.get)
                if access == "transaction":
                    store.release.set()
                assert await asyncio.to_thread(initial.result) == "initial"
                store.release.set()
            assert isinstance(await resume, Unknown)
            assert isinstance(two.inspect("initial"), Completed)
            assert isinstance(one.inspect("resume"), Unknown)
            assert isinstance(await one.resume(message, "resume"), Unknown)
        finally:
            store.release.set()
            await resume
            await owner.close()
            client.close()

    asyncio.run(scenario())
