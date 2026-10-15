"""Launch assembly reaches both frontends through injected run contracts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from tests.support.core_plugins import failing_plugin
from tests.support.real_git import REAL_PROCESSES
from tests.support.task_project import write_task

from entrypoints.headless import print_uncaught
from entrypoints.production import production_timing
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
from vibesys.api.request import RunEnvironmentSpec, load_task
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient, FakeDockerBuildRunner
from vs_project.api.testing import FakeGitRepositories
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api import (
    OsThreads,
    Platform,
    ThreadBlockingRunner,
    UnixNetwork,
)
from vs_sim.api.testing import FakePipedProcessLauncher, FakeSignalSource, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.api import Runs
    from vs_runtime.api import CorePlan, CoreRunContext


def _fake_agent(**_kwargs: object) -> FakeAgentClient:
    return FakeAgentClient(
        capabilities=AgentCapabilities(provider_session_resume=True), threads=OsThreads()
    )


def _request(root: Path) -> RunRequest:
    root.mkdir()
    (root / "queue.py").write_text("VALUE = 1\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Improve the queue.\n",
    )
    return RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(id="launch-test", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_task(root),
        objective="Improve the queue.",
        exp_name="launch-test",
        run_environment=RunEnvironmentSpec("docker", {"build_runner": FakeDockerBuildRunner()}),
        agent_backend="stub",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _runs(clients: list[FakeAgentClient] | None = None, *, real_git: bool = False) -> Runs:
    """The launch wiring over Fakes; ``real_git`` leaves the default Git CLI in place."""
    registry = OrchestrationRegistry()
    registry.register_plugin(failing_plugin("launch-test"))

    def agent_factory(**_kwargs: object) -> FakeAgentClient:
        client = FakeAgentClient(
            session_reuse=True,
            capabilities=AgentCapabilities(provider_session_resume=True),
            threads=OsThreads(),
        ).set_text(None, "retained chat answer")
        if clients is not None:
            clients.append(client)
        return client

    return default_runs(
        LaunchSettings(
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            timing=production_timing(),
            processes=REAL_PROCESSES,
            piped_processes=FakePipedProcessLauncher(),
            registry=registry,
            agent_client_factory=agent_factory,
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(threads=SimThreads()),
            git_repository=None if real_git else FakeGitRepositories().repository,
        )
    )


def test_headless_entry_starts_and_renders_launch_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The one launch run over the real Git CLI: the default wiring of the repository.
    runs = _runs(real_git=True)
    result = run_headless(_request(tmp_path / "project"), runs, FakeSignalSource())

    assert result.run_id.endswith("launch-test")
    assert not result.succeeded
    assert runs.attach(result.run_id).run_id == result.run_id
    assert runs.list_active() == ()
    assert "[git-tracking]" in capsys.readouterr().out


class _CoreRejectedError(Exception):
    """A typed failure standing in for a run-halting core error."""


# Every C0 and C1 control but tab and newline, which a terminal only lays out.
_CONTROLS = "".join(chr(code) for code in [*range(0x09), *range(0x0B, 0x20), *range(0x7F, 0xA0)])


def _rejecting_plan(_context: CoreRunContext) -> CorePlan:
    message = "core rejected\nthe observation"
    raise _CoreRejectedError(message)


def _plan_rejecting_with(message: str) -> Callable[[CoreRunContext], CorePlan]:
    def plan(_context: CoreRunContext) -> CorePlan:
        raise _CoreRejectedError(message)

    return plan


def _rejecting_runs(plan: Callable[[CoreRunContext], CorePlan]) -> Runs:
    registry = OrchestrationRegistry()
    plugin = failing_plugin("launch-test")
    registry.register_plugin(replace(plugin, core=replace(plugin.core, plan=plan)))
    return default_runs(
        LaunchSettings(
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            timing=production_timing(),
            processes=REAL_PROCESSES,
            piped_processes=FakePipedProcessLauncher(),
            registry=registry,
            agent_client_factory=_fake_agent,
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(threads=SimThreads()),
            git_repository=FakeGitRepositories().repository,
        )
    )


def test_the_run_failed_line_and_traceback_show_control_characters_instead_of_sending_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run errors carry agent and tool text; none of its control characters reaches stderr raw.

    The ``run failed`` line collapsed whitespace but passed an escape sequence,
    a bell, or a C1 control through, and so did the interpreter's traceback.
    """
    runs = _rejecting_runs(_plan_rejecting_with(f"core rejected {_CONTROLS} the observation"))
    with pytest.raises(_CoreRejectedError) as raised:
        run_headless(_request(tmp_path / "project"), runs, FakeSignalSource())
    print_uncaught(raised.type, raised.value, raised.tb)

    err = capsys.readouterr().err
    assert [char for char in err if char in _CONTROLS] == []
    failed = [line for line in err.splitlines() if line.startswith("run failed")]
    assert len(failed) == 1
    assert "\\x1b" in failed[0]
    assert err.count("\\x9b") == 2


def test_headless_entry_prints_one_run_failed_line_with_the_typed_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = _rejecting_runs(_rejecting_plan)
    with pytest.raises(Exception, match="core rejected"):
        run_headless(_request(tmp_path / "project"), runs, FakeSignalSource())

    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("run failed")]
    assert lines == ["run failed: _CoreRejectedError: core rejected the observation"]


def test_server_entry_starts_launch_run_and_projects_events(tmp_path: Path) -> None:
    runs = _runs()
    runtime = ServerRuntime(
        run_id="run-1",
        socket_path=tmp_path / "control.sock",
        runs=runs,
        platform=Platform.real(),
    )
    try:
        result = runtime.drive(_request(tmp_path / "project"))
        assert any(event.type == "run_failed" for event in runtime.journal.read())
        assert runtime.session is None
        assert runs.attach(result.run_id).run_id == result.run_id
        assert runs.list_active() == ()
    finally:
        runtime.integration.close()


def test_launch_rejects_invalid_builtin_options_before_execution(tmp_path: Path) -> None:
    request = _request(tmp_path / "project").model_copy(
        update={
            "orchestration": OrchestrationDescriptor(
                id="single-agent", config_version=1, options={"unknown_launch_option": True}
            )
        }
    )

    async def execute() -> None:
        runs = default_runs(
            LaunchSettings(
                timing=production_timing(),
                processes=REAL_PROCESSES,
                piped_processes=FakePipedProcessLauncher(),
                agent_client_factory=_fake_agent,
                blocking=ThreadBlockingRunner(),
                network=UnixNetwork(),
            )
        )
        with pytest.raises(ValueError, match="unknown_launch_option"):
            runs.start(request)
        assert runs.list_active() == ()

    asyncio.run(execute())


def test_server_retains_chat_after_run_result_and_releases_it_on_close(tmp_path: Path) -> None:
    clients: list[FakeAgentClient] = []
    runs = _runs(clients)
    runtime = ServerRuntime(
        run_id="run-1",
        socket_path=tmp_path / "control.sock",
        runs=runs,
        platform=Platform.real(),
    )
    try:
        runtime.drive(_request(tmp_path / "project"))
        assert clients, [
            event.model_dump_json()
            for event in runtime.journal.read()
            if "unavailable" in event.model_dump_json()
        ]
        # The chat's client is made when the run is ready, before the run's own; the run's
        # closed with its host, and the chat's outlives the run's result.
        retained, *owned = clients
        assert not retained.closed
        assert all(client.closed for client in owned)
        assert runtime.chat.chat("What happened in this run?") == "retained chat answer"
    finally:
        runtime.integration.close()

    assert all(client.closed for client in clients)
