"""Experiment-chat construction through the managed VibeSys API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tests.server.support import build_server_parts

from server.chat.factory import (
    DEFAULT_CHAT_THREAD,
    ChatAgentBuildRequest,
    ExperimentChatFactory,
    build_chat_agent,
)
from server.chat.prompts import (
    experiment_chat_continuation_prompt,
    experiment_chat_system_prompt,
)
from server.run_attachment import AgentSelection, RunAttachment
from vibesys.api import AuxiliaryAgentDriver

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import AuxiliaryAgentLaunch


@dataclass
class _FakeManagedAgent:
    """In-memory managed conversation returned by the fake run session."""

    answer: str = "answer"
    closed: bool = False

    def turn(self, message: str, *, invocation_id: str | None = None) -> str:
        del message
        del invocation_id
        if self.closed:
            error = "fake managed agent is closed"
            raise RuntimeError(error)
        return self.answer

    def close(self) -> None:
        self.closed = True


class _FakeRunSession:
    """Faithful auxiliary-agent slice of ``vibesys.api.RunSession``."""

    def __init__(self, agent: _FakeManagedAgent) -> None:
        self.agent = agent
        self.launches: list[AuxiliaryAgentLaunch] = []

    def create_auxiliary_agent(self, launch: AuxiliaryAgentLaunch) -> _FakeManagedAgent:
        for readable in launch.readable_inputs:
            if not readable.path.exists():
                raise FileNotFoundError(readable.path)
        self.launches.append(launch)
        return self.agent


def _selection() -> AgentSelection:
    return AgentSelection(driver="agentshim", provider="codex", model="gpt-test")


def test_default_chat_declares_one_fixed_managed_conversation(tmp_path: Path) -> None:
    shared_state_dir = tmp_path / "state" / "server" / "chat"
    shared_state_dir.mkdir(parents=True)
    agent = _FakeManagedAgent()
    session = _FakeRunSession(agent)

    result = build_chat_agent(
        ChatAgentBuildRequest(
            session=session,
            selection=_selection(),
            instance_id=None,
            shared_state_dir=shared_state_dir,
        )
    )

    assert result is agent
    assert len(session.launches) == 1
    launch = session.launches[0]
    assert launch.role == "chat"
    assert launch.member_id == DEFAULT_CHAT_THREAD
    assert (launch.driver, launch.provider, launch.model) == (
        "agentshim",
        "codex",
        "gpt-test",
    )
    assert launch.system_prompt == experiment_chat_system_prompt("$VIBESYS_CHAT_STATE_DIR")
    assert launch.continuation_prompt == experiment_chat_continuation_prompt(
        "$VIBESYS_CHAT_STATE_DIR"
    )
    assert len(launch.readable_inputs) == 1
    readable = launch.readable_inputs[0]
    assert readable.path == shared_state_dir.resolve()
    assert readable.environment_variable == "VIBESYS_CHAT_STATE_DIR"
    assert readable.purpose == "server chat transcript"


def test_each_thread_declares_its_own_context_identity(tmp_path: Path) -> None:
    shared_state_dir = tmp_path / "chat"
    shared_state_dir.mkdir()
    session = _FakeRunSession(_FakeManagedAgent())

    build_chat_agent(
        ChatAgentBuildRequest(
            session=session,
            selection=_selection(),
            instance_id="thread-7",
            shared_state_dir=shared_state_dir,
        )
    )

    assert session.launches[0].member_id == "thread-7"


def test_thread_prompt_names_investigation_tools_and_private_transcript(tmp_path: Path) -> None:
    prompt = experiment_chat_system_prompt(str(tmp_path / "threads" / "thread-1"))

    assert "`vibesys-run`" in prompt
    assert "run_summary" in prompt
    assert "list_hypotheses" in prompt
    assert "get_hypothesis" in prompt
    assert "list_rounds" in prompt
    assert "list_state_files" in prompt
    assert "read_state_file" in prompt
    assert "conversation.jsonl" in prompt


def test_factory_creates_transcript_directory_before_declaring_readable_input(
    tmp_path: Path,
) -> None:
    shared_state_dir = tmp_path / "server" / "chat"
    parts = build_server_parts(tmp_path / "logs")
    session = _FakeRunSession(_FakeManagedAgent())
    factory = ExperimentChatFactory(
        manager=parts.chat,
        controller=parts.controller,
        executions=parts.executions,
        session=session,
        attachment=RunAttachment(
            chat_state_dir=shared_state_dir,
            agent_defaults=_selection(),
            agent_drivers=(AuxiliaryAgentDriver(driver="agentshim", providers=("codex",)),),
        ),
        build_agent=build_chat_agent,
        fallback=lambda _question: "recorded summary",
    )
    try:
        factory.start()
        assert shared_state_dir.is_dir()
        assert session.launches[0].readable_inputs[0].path == shared_state_dir
    finally:
        factory.close()
        parts.close()
