"""Experiment-chat agent construction tests."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from server.chat.factory import build_chat_agent
from server.chat.prompts import experiment_chat_system_prompt
from vibesys.config import Config
from vibesys.domains.environment import EnvironmentBindMount
from vibesys.run.integration import AgentSelection, RunAttachment
from vs_sandbox import HostResourceAccess, ProjectPathPolicy

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    import pytest


class _Client:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class _EnvironmentRequest:
    environment_bind_mounts: tuple[EnvironmentBindMount, ...] = ()
    log: Callable[[str], None] | None = None


class _Environment:
    def __init__(self) -> None:
        self.request: _EnvironmentRequest | None = None

    @contextmanager
    def open(self, request: _EnvironmentRequest) -> Generator[Any]:
        self.request = request
        yield SimpleNamespace(
            sandbox=object(),
            view=SimpleNamespace(cli_sandboxed=True, isolated=True),
        )


def test_thread_prompt_uses_shared_evidence_and_private_transcript() -> None:
    prompt = experiment_chat_system_prompt(
        "/state/server/chat",
        "/state/server/chat/threads/thread-1",
    )

    assert "`/state/server/chat/trajectory/state/`" in prompt
    assert "`/state/server/chat/trajectory/logs/`" in prompt
    assert "`/state/server/chat/threads/thread-1/conversation.jsonl`" in prompt


def _attachment(
    tmp_path: Path,
    *,
    sandboxed: bool,
    environment: _Environment | None = None,
) -> RunAttachment:
    workspace = tmp_path / "workspace"
    log_dir = tmp_path / "state" / "runs" / "run-1" / "logs"
    workspace.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    runtime = SimpleNamespace(
        config=Config.model_validate(
            {
                "model": {"name": "gpt-test"},
                "agent": {"backend": "cli", "driver": "agentshim"},
            }
        ),
        compute_backend="cpu",
        model=None,
        skill_source_dirs=(),
        environment=environment,
        environment_request=_EnvironmentRequest(),
        run_environment_sandboxed=sandboxed,
        project_path_policy=ProjectPathPolicy(),
        host_resources=(),
    )
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
        agent_runtime=cast("Any", runtime),
    )


def test_host_chat_agent_receives_read_only_server_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = _attachment(tmp_path, sandboxed=False)
    shared_state_dir = attachment.log_dir.parent / "server" / "chat"
    shared_state_dir.mkdir(parents=True)
    captured: dict[str, Any] = {}
    client = _Client()

    def fake_build_agent_client(*_args: object, **kwargs: object) -> _Client:
        captured.update(kwargs)
        return client

    monkeypatch.setattr("server.chat.factory.build_agent_client", fake_build_agent_client)

    resources = build_chat_agent(
        attachment,
        attachment.agent_defaults,
        None,
        shared_state_dir,
    )

    assert resources.agent_shared_state_dir == str(shared_state_dir)
    chat_resource = captured["host_resources"][-1]
    assert chat_resource.path == shared_state_dir
    assert chat_resource.access is HostResourceAccess.READ_ONLY
    assert captured["use_docker"] is False
    resources.close()
    assert client.closed


def test_container_chat_agent_mounts_server_state_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _Environment()
    attachment = _attachment(tmp_path, sandboxed=True, environment=environment)
    shared_state_dir = attachment.log_dir.parent / "server" / "chat"
    shared_state_dir.mkdir(parents=True)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    existing_mount = EnvironmentBindMount(model_dir, "/model", read_only=True)
    cast("Any", attachment.agent_runtime).environment_request = _EnvironmentRequest(
        (existing_mount,)
    )
    captured: dict[str, Any] = {}
    client = _Client()

    def fake_build_agent_client(*_args: object, **kwargs: object) -> _Client:
        captured.update(kwargs)
        return client

    monkeypatch.setattr("server.chat.factory.build_agent_client", fake_build_agent_client)

    resources = build_chat_agent(
        attachment,
        attachment.agent_defaults,
        "thread-1",
        shared_state_dir,
    )

    assert resources.agent_shared_state_dir == "/opt/vibesys-chat"
    assert environment.request is not None
    assert environment.request.environment_bind_mounts == (
        existing_mount,
        EnvironmentBindMount(shared_state_dir, "/opt/vibesys-chat", read_only=True),
    )
    assert captured["use_docker"] is True
    assert set(captured["backends"]) == {"chat"}
    resources.close()
    assert client.closed
