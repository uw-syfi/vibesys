"""Experiment-chat agent construction tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from server.chat.factory import build_chat_agent
from server.chat.prompts import experiment_chat_system_prompt
from server.run_attachment import AgentSelection, RunAttachment
from vibesys.config import Config
from vibesys.skills import NULL_SKILL_SELECTION
from vs_agent.api import MCPServerSpec
from vs_agent.api.testing import FakeAgentClient
from vs_sandbox.api import HostResource, HostResourceAccess, ProjectPathPolicy

_FAKE_TOOL_SERVERS = (
    MCPServerSpec(
        name="vibesys-run",
        command="python",
        args=("-m", "vibesys.api.chat_tools_server", "--run-id", "run-1"),
    ),
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vibesys.skills import SkillSelection


@dataclass
class _FakeAgentEnvironment:
    """Stub `vibesys.api.AgentEnvironment` returned by `_FakeRunSession`."""

    config: Config
    skill_selection: SkillSelection = NULL_SKILL_SELECTION
    skill_source_dirs: tuple[Path, ...] = ()
    project_path_policy: ProjectPathPolicy = field(default_factory=ProjectPathPolicy)
    host_resources: tuple[HostResource, ...] = ()
    backends: dict[str, Any] | None = None
    use_docker: bool = False
    isolated: bool = False
    agent_path_value: str | None = None
    closed: bool = False
    tool_servers: tuple[MCPServerSpec, ...] = _FAKE_TOOL_SERVERS

    def agent_path(self, _host: Path) -> str:
        assert self.agent_path_value is not None, "agent_path() called with no configured value"
        return self.agent_path_value

    def investigation_tools(self) -> tuple[MCPServerSpec, ...]:
        return self.tool_servers

    def close(self) -> None:
        self.closed = True


class _FakeRunSession:
    """Stub `vibesys.api.RunSession` exposing only `open_agent_environment`."""

    def __init__(self, environment: _FakeAgentEnvironment) -> None:
        self._environment = environment
        self.mounts: tuple[HostResource, ...] | None = None

    def open_agent_environment(
        self, *, mounts: tuple[HostResource, ...] = ()
    ) -> _FakeAgentEnvironment:
        self.mounts = mounts
        return self._environment


def test_thread_prompt_uses_investigation_tools_and_private_transcript() -> None:
    prompt = experiment_chat_system_prompt("/state/server/chat/threads/thread-1")

    assert "`vibesys-run`" in prompt
    assert "run_summary" in prompt
    assert "list_hypotheses" in prompt
    assert "get_hypothesis" in prompt
    assert "list_rounds" in prompt
    assert "list_state_files" in prompt
    assert "read_state_file" in prompt
    assert "`/state/server/chat/threads/thread-1/conversation.jsonl`" in prompt


def _config() -> Config:
    return Config.model_validate(
        {
            "model": {"name": "gpt-test"},
            "agent": {"backend": "cli", "driver": "agentshim"},
        }
    )


def _attachment(tmp_path: Path) -> RunAttachment:
    workspace = tmp_path / "workspace"
    log_dir = tmp_path / "state" / "runs" / "run-1" / "logs"
    workspace.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    return RunAttachment(
        project=cast("Any", None),
        run_id="run-1",
        workspace=workspace,
        log_dir=log_dir,
        agent_backend="cli",
        agent_defaults=AgentSelection(
            driver="agentshim",
            provider="claude",
            model="claude-haiku-4-5",
        ),
    )


def test_host_chat_agent_receives_read_only_server_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = _attachment(tmp_path)
    shared_state_dir = attachment.log_dir.parent / "server" / "chat"
    shared_state_dir.mkdir(parents=True)
    captured: dict[str, Any] = {}
    client = FakeAgentClient()

    def fake_build_agent_client(*_args: object, **kwargs: object) -> FakeAgentClient:
        captured.update(kwargs)
        return client

    monkeypatch.setattr("server.chat.factory.build_agent_client", fake_build_agent_client)

    environment = _FakeAgentEnvironment(config=_config())
    session = _FakeRunSession(environment)

    resources = build_chat_agent(
        cast("Any", session),
        attachment,
        attachment.agent_defaults,
        None,
        shared_state_dir,
    )

    assert resources.agent_shared_state_dir == str(shared_state_dir)
    assert session.mounts is not None
    chat_mount = session.mounts[-1]
    assert chat_mount.path == shared_state_dir
    assert chat_mount.access is HostResourceAccess.READ_ONLY
    assert chat_mount.agent_path == "/opt/vibesys-chat"
    chat_resource = captured["host_resources"][-1]
    assert chat_resource.path == shared_state_dir
    assert chat_resource.access is HostResourceAccess.READ_ONLY
    assert captured["use_docker"] is False
    assert resources.mcp_servers == _FAKE_TOOL_SERVERS
    assert environment.closed is False
    resources.close()
    assert client.closed
    assert environment.closed


def test_container_chat_agent_mounts_server_state_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = _attachment(tmp_path)
    shared_state_dir = attachment.log_dir.parent / "server" / "chat"
    shared_state_dir.mkdir(parents=True)
    captured: dict[str, Any] = {}
    client = FakeAgentClient()

    def fake_build_agent_client(*_args: object, **kwargs: object) -> FakeAgentClient:
        captured.update(kwargs)
        return client

    monkeypatch.setattr("server.chat.factory.build_agent_client", fake_build_agent_client)

    environment = _FakeAgentEnvironment(
        config=_config(),
        backends={"chat": object()},
        use_docker=True,
        isolated=True,
        agent_path_value="/opt/vibesys-chat",
    )
    session = _FakeRunSession(environment)

    resources = build_chat_agent(
        cast("Any", session),
        attachment,
        attachment.agent_defaults,
        "thread-1",
        shared_state_dir,
    )

    assert resources.agent_shared_state_dir == "/opt/vibesys-chat"
    assert session.mounts is not None
    chat_mount = session.mounts[-1]
    assert chat_mount.path == shared_state_dir
    assert chat_mount.access is HostResourceAccess.READ_ONLY
    assert chat_mount.agent_path == "/opt/vibesys-chat"
    assert captured["use_docker"] is True
    assert set(captured["backends"]) == {"chat"}
    assert resources.mcp_servers == _FAKE_TOOL_SERVERS
    resources.close()
    assert client.closed
    assert environment.closed
