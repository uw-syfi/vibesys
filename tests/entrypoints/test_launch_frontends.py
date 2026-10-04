"""Launch assembly reaches both frontends through injected run contracts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.plugin import EmptyOptions

from entrypoints.run import run_headless
from launch import LaunchSettings, default_runs
from server.runtime import ServerRuntime
from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationDescriptor,
    OrchestrationRegistry,
    ProfilerKind,
    RunRequest,
)
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle
from vs_agent.api.testing import FakeAgentClient
from vs_runtime.api import OrchestrationPlugin, Run, RunStatus
from vs_sandbox.api.testing import FakeComputeBackend

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vibesys.api import Runs


async def _finish(run: Run, _options: BaseModel) -> RunStatus:
    run.observations.note("launched through an injected run service")
    return RunStatus.SUCCEEDED


def _fake_agent(**_kwargs: object) -> FakeAgentClient:
    return FakeAgentClient()


def _request(root: Path) -> RunRequest:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )
    return RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(id="launch-test", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_input_bundle(root),
        objective="Improve the queue.",
        exp_name="launch-test",
        run_environment=RunEnvironmentSpec("local"),
        agent_backend="stub",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _runs(clients: list[FakeAgentClient] | None = None) -> Runs:
    registry = OrchestrationRegistry()
    registry.register_plugin(
        OrchestrationPlugin(id="launch-test", agents=(), options=EmptyOptions, orchestrate=_finish)
    )

    def agent_factory(**_kwargs: object) -> FakeAgentClient:
        client = FakeAgentClient(session_reuse=True).set_text(None, "retained chat answer")
        if clients is not None:
            clients.append(client)
        return client

    return default_runs(
        LaunchSettings(
            registry=registry,
            agent_client_factory=agent_factory,
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(),
        )
    )


def test_headless_entry_starts_and_renders_launch_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _runs()
    result = run_headless(_request(tmp_path / "project"), runs)

    assert result.succeeded
    assert result.run_id.endswith("launch-test")
    assert runs.attach(result.run_id).run_id == result.run_id
    assert runs.list_active() == ()
    assert "launched through an injected run service" in capsys.readouterr().out


def test_server_entry_starts_launch_run_and_projects_events(tmp_path: Path) -> None:
    runs = _runs()
    runtime = ServerRuntime(socket_path=tmp_path / "control.sock", runs=runs)
    try:
        result = runtime.drive(_request(tmp_path / "project"))
        assert result.succeeded
        assert any(event.type == "run_finished" for event in runtime.journal.read())
        assert runtime.session is None
        assert runs.attach(result.run_id).run_id == result.run_id
        assert runs.list_active() == ()
    finally:
        runtime.integration.close()


@pytest.mark.parametrize("settings", [None, LaunchSettings(agent_client_factory=_fake_agent)])
def test_launch_rejects_invalid_builtin_options_before_execution(
    settings: LaunchSettings | None, tmp_path: Path
) -> None:
    request = _request(tmp_path / "project").model_copy(
        update={
            "orchestration": OrchestrationDescriptor(
                id="single-agent", config_version=1, options={"unknown_launch_option": True}
            )
        }
    )

    async def execute() -> None:
        runs = default_runs(settings)
        with pytest.raises(ValueError, match="unknown_launch_option"):
            runs.start(request)
        assert runs.list_active() == ()

    asyncio.run(execute())


def test_server_retains_chat_after_run_result_and_releases_it_on_close(tmp_path: Path) -> None:
    clients: list[FakeAgentClient] = []
    runs = _runs(clients)
    runtime = ServerRuntime(socket_path=tmp_path / "control.sock", runs=runs)
    try:
        result = runtime.drive(_request(tmp_path / "project"))
        assert result.succeeded
        assert clients, [
            event.model_dump_json()
            for event in runtime.journal.read()
            if "unavailable" in event.model_dump_json()
        ]
        assert all(not client.closed for client in clients)
        assert runtime.chat.chat("What happened in this run?") == "retained chat answer"
    finally:
        runtime.integration.close()

    assert all(client.closed for client in clients)
