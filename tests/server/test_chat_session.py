"""Experiment-chat prompt selection over a reused provider conversation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from tests.server.support import build_server_parts

# The agentshim driver's own fake agent, reused rather than copied: these tests
# are about what the real driver reports to the real client, so a second fake
# that drifted from it would stop testing that.
from tests.vibesys.agents.drivers.test_agentshim_driver import _FakeAgent

from server.chat.prompts import (
    experiment_chat_continuation_prompt,
    experiment_chat_system_prompt,
)
from server.chat.session import ExperimentChatDependencies, ExperimentChatSession
from server.events import EventType
from vibesys.agents.client import AgentClient
from vibesys.agents.drivers import agentshim
from vibesys.agents.session_key import AgentSessionKey, SessionScope

if TYPE_CHECKING:
    from pathlib import Path

_SHARED_STATE_DIR = "/state/server/chat"
_FULL_PROMPT = experiment_chat_system_prompt(_SHARED_STATE_DIR, _SHARED_STATE_DIR)
_CONTINUATION_PROMPT = experiment_chat_continuation_prompt(_SHARED_STATE_DIR)


class _FakeClient:
    """Record chat invocations and answer both conversation questions."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        #: The conversation a next turn would continue.
        self.continues: str | None = None
        #: The conversation the last completed turn ran in.
        self.ran_in: str | None = None

    def invoke_text(self, **kwargs: Any) -> str:  # noqa: ANN401  # Mirrors agent clients.
        self.calls.append(kwargs)
        return "It improved in round 2."

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        del session_key
        return self.continues

    def last_turn_provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        del session_key
        return self.ran_in


def _chat(
    tmp_path: Path,
    client: Any,  # noqa: ANN401  # Any ChatAgentClient implementation.
    *,
    thread_id: str | None = None,
    executions: Any = None,  # noqa: ANN401  # Any ExecutionTracker stand-in.
    controller: Any = None,  # noqa: ANN401  # Any RunController stand-in.
) -> ExperimentChatSession:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    if controller is None:
        controller = MagicMock()
        controller.start_agent_execution.return_value = SimpleNamespace(execution_id="exec-1")
    return ExperimentChatSession(
        ExperimentChatDependencies(
            controller=controller,
            executions=MagicMock() if executions is None else executions,
            agent_client=client,
            session_key=AgentSessionKey(SessionScope.CHAT, thread_id or "default"),
            chat_thread_id=thread_id,
            workspace=workspace,
            state_dir=tmp_path / "state",
            agent_shared_state_dir=_SHARED_STATE_DIR,
            agent_state_dir=_SHARED_STATE_DIR,
            evidence=MagicMock(),
            log=lambda _message: None,
            environment=dict,
            progress=lambda: None,
            driver="agentshim",
            provider="codex",
            model="gpt-test",
            fallback=lambda _question: "fallback",
        )
    )


def test_chat_reuses_a_conversation_keyed_by_thread(tmp_path: Path) -> None:
    client = _FakeClient()
    chat = _chat(tmp_path, client, thread_id="thread-a")

    chat.ask("what happened?")

    kwargs = client.calls[0]
    assert kwargs["reuse_session"] is True
    assert kwargs["session_key"] == AgentSessionKey(SessionScope.CHAT, "thread-a")


def test_chat_sends_the_full_prompt_when_no_conversation_is_named(tmp_path: Path) -> None:
    client = _FakeClient()
    chat = _chat(tmp_path, client)

    chat.ask("what happened?")

    assert [call["system_prompt"] for call in client.calls] == [_FULL_PROMPT]


def test_chat_shortens_the_prompt_inside_a_named_conversation(tmp_path: Path) -> None:
    client = _FakeClient()
    client.continues = "session-1"
    client.ran_in = "session-1"
    chat = _chat(tmp_path, client)

    chat.ask("what happened?")

    assert [call["system_prompt"] for call in client.calls] == [_CONTINUATION_PROMPT]


def test_chat_reasks_when_the_turn_ran_in_a_different_conversation(tmp_path: Path) -> None:
    client = _FakeClient()
    client.continues = "session-1"
    # The driver replaced the conversation while serving the turn, so the
    # shortened prompt reached an agent that never saw the read-only rules.
    client.ran_in = "session-2"
    chat = _chat(tmp_path, client)

    answer = chat.ask("what happened?")

    assert [call["system_prompt"] for call in client.calls] == [
        _CONTINUATION_PROMPT,
        _FULL_PROMPT,
    ]
    assert answer == "It improved in round 2."


def test_chat_keeps_one_answer_when_the_conversation_retires_after_the_turn(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    client.continues = "session-1"
    # The turn ran where the shortened prompt assumed; the conversation was
    # retired only afterwards, so its answer stands.
    client.ran_in = "session-1"
    chat = _chat(tmp_path, client)
    chat.ask("what happened?")
    client.continues = None

    chat.ask("and then?")

    assert [call["system_prompt"] for call in client.calls] == [
        _CONTINUATION_PROMPT,
        _FULL_PROMPT,
    ]


def test_chat_never_reasks_a_cold_turn(tmp_path: Path) -> None:
    client = _FakeClient()
    client.ran_in = "session-1"
    chat = _chat(tmp_path, client)

    chat.ask("what happened?")

    # Nothing justified a shortened prompt, so a new conversation is expected.
    assert len(client.calls) == 1


@pytest.fixture
def fake_agents(monkeypatch: pytest.MonkeyPatch) -> list[_FakeAgent]:
    """Build every provider conversation from the driver's own fake agent."""
    built: list[_FakeAgent] = []

    def factory(
        model: str | None = None,
        event_handler: Any | None = None,  # noqa: ANN401  # Mirrors the agent constructor.
        *,
        executor: Any | None = None,  # noqa: ANN401  # Mirrors the agent constructor.
    ) -> _FakeAgent:
        agent = _FakeAgent(model, event_handler, executor=executor)
        built.append(agent)
        return agent

    for provider in ("codex", "claude"):
        monkeypatch.setitem(agentshim._PROVIDER_CLASSES, provider, factory)  # noqa: SLF001
    monkeypatch.setattr(agentshim, "declare_agent_host_resources", lambda *_a, **_k: ())
    monkeypatch.setattr(agentshim, "build_host_sandbox", lambda *_a, **_k: "sandbox")
    return built


def _prompts(agents: list[_FakeAgent]) -> list[str]:
    """Every prompt the provider was given, in the order the turns ran."""
    return [prompt for agent in agents for prompt, _cwd, _timeout in agent.generate_calls]


def test_codex_thread_renewal_puts_the_read_only_rules_back(
    fake_agents: list[_FakeAgent], tmp_path: Path
) -> None:
    # The Codex thread budget retires a thread after a fixed number of turns.
    # Before drivers reported that, chat kept shortening its prompt into the
    # replacement thread, so a question periodically ran with no read-only
    # rules at all. The driver now reports the restart, the client drops the
    # conversation, and the next question carries the full instructions again.
    client = AgentClient(agentshim.AgentShimDriver(provider="codex"), provider="codex")
    chat = _chat(tmp_path, client)

    for question in ("what happened?", "and then?", "why?"):
        chat.ask(question)

    prompts = _prompts(fake_agents)
    assert len(prompts) == 3, "one provider turn per question, with no re-asks"
    assert prompts[0].startswith(_FULL_PROMPT)
    assert prompts[1].startswith(_CONTINUATION_PROMPT)
    assert prompts[2].startswith(_FULL_PROMPT)
    assert agentshim._MAX_CODEX_SESSION_TURNS == 2  # noqa: SLF001  # the cadence above


def test_stale_claude_session_reasks_instead_of_failing_the_question(
    fake_agents: list[_FakeAgent], tmp_path: Path
) -> None:
    client = AgentClient(agentshim.AgentShimDriver(provider="claude"), provider="claude")
    chat = _chat(tmp_path, client)
    chat.ask("what happened?")

    resumed = fake_agents[0]
    failures = [RuntimeError("claude exited with code 1")]

    def refuse_the_resume(prompt: str, /, **kwargs: Any) -> str:  # noqa: ANN401
        del kwargs
        resumed.generate_calls.append((prompt, None, None))
        if failures:
            raise failures.pop()
        return "done"

    resumed.generate_override = refuse_the_resume

    answer = chat.ask("and then?")

    prompts = _prompts(fake_agents)
    # The shortened prompt, the driver's own retry of it from a fresh
    # conversation, then the question asked again with the rules restored.
    assert prompts[1].startswith(_CONTINUATION_PROMPT)
    assert prompts[2].startswith(_CONTINUATION_PROMPT)
    assert prompts[3].startswith(_FULL_PROMPT)
    assert answer == "done"


@pytest.mark.parametrize("thread_id", [None, "thread-a"])
def test_streamed_output_is_filed_under_the_thread_that_asked(
    tmp_path: Path, thread_id: str | None
) -> None:
    parts = build_server_parts(tmp_path)

    class _StreamingClient(_FakeClient):
        """Emit a token mid-turn the way a streaming driver does."""

        def invoke_text(self, **kwargs: Any) -> str:  # noqa: ANN401
            parts.executions.publish_agent_output("partial ")
            return super().invoke_text(**kwargs)

    chat = _chat(
        tmp_path,
        _StreamingClient(),
        thread_id=thread_id,
        executions=parts.executions,
        controller=parts.controller,
    )

    chat.ask("what happened?")

    assert [
        (event.agent_kind, event.chat_thread_id)
        for event in parts.journal.read()
        if event.type is EventType.AGENT_OUTPUT_CHUNK
    ] == [("chat", thread_id)]


def test_chat_normalizes_an_agent_failure(tmp_path: Path) -> None:
    class _FailingClient(_FakeClient):
        def invoke_text(self, **kwargs: Any) -> str:  # noqa: ANN401
            super().invoke_text(**kwargs)
            raise ValueError("no such workspace")  # noqa: TRY003  # test fixture

    client = _FailingClient()
    chat = _chat(tmp_path, client)

    with pytest.raises(RuntimeError, match="Chat agent failed: ValueError: no such workspace"):
        chat.ask("what happened?")

    assert len(client.calls) == 1


def test_chat_propagates_a_cancellation_unwrapped(tmp_path: Path) -> None:
    class _CancelledClient(_FakeClient):
        def invoke_text(self, **kwargs: Any) -> str:  # noqa: ANN401
            super().invoke_text(**kwargs)
            raise KeyboardInterrupt

    chat = _chat(tmp_path, _CancelledClient())

    # A cancellation is not an agent error and must not be reported as one.
    with pytest.raises(KeyboardInterrupt):
        chat.ask("what happened?")
