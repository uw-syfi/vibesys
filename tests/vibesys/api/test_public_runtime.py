"""Public runtime capability tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from tests.vibesys.orchestration.plugin import capability_plugin

from vibesys.api import (
    ComputeBackend,
    Config,
    ConfigurationError,
    OrchestrationDescriptor,
    ProfilerKind,
    RunRequest,
)
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle
from vibesys.context import borrow_run_agent_environment
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api import ToolServerDescriptor
from vs_runtime.api import Workspace
from vs_runtime.api.infrastructure import SkyPilotEnvironment
from vs_sandbox.api.testing import FakeComputeBackend

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.context import _RunResources


_PLUGIN = capability_plugin("three-agent-rounds")


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
        super().__init__()
        self.monitor = _LifecycleMonitor()

    def make_monitor(self, log_dir: Path) -> _LifecycleMonitor:
        del log_dir
        return self.monitor


class _ToolBindingAssemblyError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("tool binding assembly failed")


class _FailingToolBindings(
    Mapping[str, Callable[[object, Workspace], tuple[ToolServerDescriptor, ...]]]
):
    def __len__(self) -> int:
        return 1

    def __iter__(self) -> Iterator[str]:
        raise _ToolBindingAssemblyError

    def __getitem__(
        self,
        key: str,
    ) -> Callable[[object, Workspace], tuple[ToolServerDescriptor, ...]]:
        raise KeyError(key)


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _request(project_root: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(
            id="three-agent-rounds", config_version=1, options={"rounds": 2}
        ),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name="team-demo",
        run_environment=RunEnvironmentSpec("local"),
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def test_skypilot_agents_borrow_the_workspace_session() -> None:
    environment = SkyPilotEnvironment.from_options({"profile": "test-cluster"})
    assert environment.config.profile == "test-cluster"
    closed: list[bool] = []
    session = SimpleNamespace(
        view=SimpleNamespace(cli_sandboxed=False),
        close=lambda: closed.append(True),
    )
    context = cast(
        "_RunResources",
        SimpleNamespace(
            environment_request=SimpleNamespace(
                agent_backend="stub", cli_provider="claude", project_path_policy=None
            ),
            run_environment_session=session,
            skill_source_paths=(),
            backend=ComputeBackend.CPU,
            agent_host_resources=(),
        ),
    )
    borrowed = borrow_run_agent_environment(context, agent_backend="stub", cli_provider="claude")
    assert borrowed.session is session
    borrowed.close()
    assert not closed
    with pytest.raises(ConfigurationError, match="must use its configured backend"):
        borrow_run_agent_environment(context, cli_provider="codex")


def test_root_workspace_capabilities(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with open_product_run_host(
            _request(project_root), integration, plugin=_PLUGIN
        ) as ctx:
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
            assert not ctx.workspaces.supports_parallel_candidates
            with pytest.raises(RuntimeError, match="cannot open isolated candidate sandboxes"):
                await ctx.workspaces.create_candidate()

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


def test_product_host_closes_resources_when_capability_assembly_fails(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()
    backend = _LifecycleBackend()

    def backend_factory(*_args: object, **_kwargs: object) -> _LifecycleBackend:
        return backend

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="tool binding assembly failed"):
            async with open_product_run_host(
                _request(project_root),
                integration,
                plugin=_PLUGIN,
                backend_factory=backend_factory,
                agent_tool_bindings=_FailingToolBindings(),
            ):
                pytest.fail("failed product assembly yielded a host")

    try:
        asyncio.run(exercise())
    finally:
        integration.close()

    assert backend.monitor.started
    assert backend.monitor.stopped
