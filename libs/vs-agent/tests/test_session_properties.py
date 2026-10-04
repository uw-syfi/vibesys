"""Generated invocation traces share one contract across session implementations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from agentshim.testing import FakeExecutor, FakeRun, scripted_turn
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import (
    AgentClient,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    ClientAgentSessions,
    Completed,
    InvocationConflictError,
    SessionScope,
    Unknown,
)
from vs_agent.api.testing import (
    FakeAgentInvocationStore,
    FakeAgentSessions,
    FakeDriver,
    fake_agentshim_driver,
)
from vs_prompts.api import TemplateRenderer


@dataclass
class _Execution:
    calls: int = 0
    reject: bool = False

    def accept(self) -> None:
        self.calls += 1
        if self.reject:
            detail = "provider acceptance is ambiguous"
            raise LookupError(detail)

    def execute(self, _request: object) -> FakeRun:
        self.accept()
        return scripted_turn("codex", session_id="thread-1", text="done")


def _client(implementation: str, execution: _Execution) -> AgentClient:
    driver = (
        FakeDriver(answer="done", on_turn=lambda _: execution.accept())
        if implementation == "fake"
        else fake_agentshim_driver(provider="codex", executor=FakeExecutor(execution.execute))
    )
    return AgentClient(driver, provider="codex")


def _sessions(
    implementation: str,
    client: AgentClient,
    store: FakeAgentInvocationStore,
    root: Path,
) -> tuple[ClientAgentSessions, AgentSessionKey]:
    key = AgentSessionKey(SessionScope.MEMBER, "generated")
    spec = AgentSessionSpec(
        role="implementer",
        provider="codex",
        workspace=root,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    if client.provider_session_id(key) is None:
        client.run(session_spec=spec, turn=AgentTurnRequest(message="initial"), session_key=key)
    factory = FakeAgentSessions if implementation == "fake" else ClientAgentSessions
    sessions = factory(client, store)
    sessions.bind(
        key,
        spec,
        AgentTurnRequest(message="", expected_provider_session_id=client.provider_session_id(key)),
    )
    return sessions, key


@pytest.mark.parametrize("implementation", ["fake", "client"])
@given(
    st.lists(
        st.tuples(st.integers(0, 3), st.integers(0, 3), st.booleans()),
        min_size=1,
        max_size=20,
    )
)
def test_generated_duplicate_and_conflicting_dispatch_traces(
    implementation: str, operations: list[tuple[int, int, bool]]
) -> None:
    """Every identity has one immutable payload and one external execution."""
    with TemporaryDirectory(prefix="c2-session-properties-") as directory:
        root = Path(directory)
        renderer = TemplateRenderer(root)
        (root / "message.j2").write_text("{{ content }}", encoding="utf-8")
        execution = _Execution()
        client = _client(implementation, execution)
        store = FakeAgentInvocationStore()
        sessions, key = _sessions(implementation, client, store, root)
        observed: dict[str, tuple[int, Completed]] = {}
        try:
            for identity, payload, restart in operations:
                if restart:
                    sessions, key = _sessions(implementation, client, store, root)
                invocation_id = str(identity)
                message = renderer.render_template("message.j2", content=payload)
                previous = observed.get(invocation_id)
                if previous is not None and previous[0] != payload:
                    with pytest.raises(InvocationConflictError):
                        sessions.resume(key, message, invocation_id)
                else:
                    outcome = sessions.resume(key, message, invocation_id)
                    assert isinstance(outcome, Completed)
                    if previous is None:
                        observed[invocation_id] = payload, outcome
                    else:
                        assert outcome == previous[1]
                    assert sessions.inspect(key, invocation_id) == outcome
                assert execution.calls == 1 + len(observed)
        finally:
            client.close()


@pytest.mark.parametrize("implementation", ["fake", "client"])
@given(st.text(min_size=1, max_size=30), st.integers(1, 10))
def test_generated_unknown_dispatch_remains_fenced_after_reconstruction(
    implementation: str, invocation_id: str, repetitions: int
) -> None:
    """Ambiguous acceptance cannot authorize another dispatch of its identity."""
    with TemporaryDirectory(prefix="c2-session-properties-") as directory:
        root = Path(directory)
        renderer = TemplateRenderer(root)
        (root / "message.j2").write_text("{{ content }}", encoding="utf-8")
        execution = _Execution()
        client = _client(implementation, execution)
        store = FakeAgentInvocationStore()
        sessions, key = _sessions(implementation, client, store, root)
        message = renderer.render_template("message.j2", content="trusted results")
        execution.reject = True
        try:
            outcome = sessions.resume(key, message, invocation_id)
            assert isinstance(outcome, Unknown)
            assert outcome.checkpoint is not None
            factory = FakeAgentSessions if implementation == "fake" else ClientAgentSessions
            spec = AgentSessionSpec(
                role="implementer",
                provider="codex",
                workspace=root,
                policy=AgentExecutionPolicy(require_enforcement=False),
            )
            for _ in range(repetitions):
                sessions = factory(client, store)
                sessions.bind(
                    key,
                    spec,
                    AgentTurnRequest(
                        message="",
                        expected_provider_session_id=outcome.checkpoint.provider_session_id,
                    ),
                )
                assert sessions.inspect(key, invocation_id) == outcome
                assert sessions.resume(key, message, invocation_id) == outcome
                changed = renderer.render_template("message.j2", content="different results")
                with pytest.raises(InvocationConflictError):
                    sessions.resume(key, changed, invocation_id)
                assert execution.calls == 2
        finally:
            client.close()
