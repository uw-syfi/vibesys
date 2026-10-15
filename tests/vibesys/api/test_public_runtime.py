"""Public runtime capability tests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.support.core_host_world import open_core_host
from tests.support.docker_environment import fake_docker_environment
from tests.support.real_git import NO_GITHUB, REAL_GIT
from tests.support.task_project import write_task
from tests.vibesys.orchestration.plugin import EmptyOptions, capability_plugin

from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationDescriptor,
    ProfilerKind,
    RunRequest,
)
from vibesys.api.request import load_task
from vibesys.api.testing import FakeProfilerPreflight
from vibesys.run.core_services import CoreCompositionError
from vibesys.run.host import open_product_core_host
from vibesys.run.integration import LocalRunIntegration
from vs_runtime.api import AgentRole
from vs_runtime.api.core import RunTiming
from vs_runtime.api.testing import FakeStopTimer, idle_environment_system
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api import OsThreads, SystemClock, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

_PLUGIN = capability_plugin("three-agent-rounds")
_AGENT_PLUGIN = capability_plugin(
    "three-agent-rounds",
    agents=(AgentRole(id="implementer", system_prompt="Implement the requested change."),),
)


class _LifecycleMonitor:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _LifecycleBackend(FakeComputeBackend):
    def __init__(self) -> None:
        super().__init__(threads=SimThreads())
        self.monitor = _LifecycleMonitor()

    def make_monitor(self, log_dir: Path) -> _LifecycleMonitor:
        del log_dir
        return self.monitor


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "queue.py").write_text("VALUE = 1\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Improve the queue.\n",
    )


def _request(project_root: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(
            id="three-agent-rounds", config_version=1, options={"rounds": 2}
        ),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_task(project_root),
        objective="Improve the queue.",
        exp_name="team-demo",
        run_environment=fake_docker_environment(),
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def test_root_workspace_capabilities(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )

    async def exercise() -> None:
        async with open_core_host(_request(project_root), integration, plugin=_PLUGIN) as ctx:
            root = ctx.workspaces.root
            original = root.revision
            assert original is not None
            (root.path / "queue.py").write_text("VALUE = 2\n")
            assert "queue.py" in await root.pending_changes()
            changed = await root.snapshot("public runtime candidate")
            assert changed == root.revision
            await root.restore(original)
            assert (root.path / "queue.py").read_text() == "VALUE = 1\n"
            await root.restore(changed)
            assert (root.path / "queue.py").read_text() == "VALUE = 2\n"
            assert await root.retain(changed, label="public-probe") is None
            assert ctx.workspaces.supports_parallel_candidates

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


def test_product_host_closes_resources_when_composition_fails(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )
    backend = _LifecycleBackend()

    def backend_factory(*_args: object, **_kwargs: object) -> _LifecycleBackend:
        return backend

    async def exercise() -> None:
        with pytest.raises(CoreCompositionError, match="agent_client_factory"):
            async with open_product_core_host(
                _request(project_root),
                integration,
                plugin=_AGENT_PLUGIN,
                options=EmptyOptions(),
                timing=RunTiming(SystemClock(), 60.0, OsThreads()),
                backend_factory=backend_factory,
                stop_timer=FakeStopTimer(threads=OsThreads()),
                blocking=ThreadBlockingRunner(),
                network=UnixNetwork(),
                profiler_preflight=FakeProfilerPreflight(),
            ):
                pytest.fail("failed product assembly yielded a run")

    try:
        asyncio.run(exercise())
    finally:
        integration.close()

    assert backend.monitor.started
    assert backend.monitor.stopped
