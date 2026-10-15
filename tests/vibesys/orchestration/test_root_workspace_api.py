"""Public root-workspace behavior over the production runtime."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.support.core_host_world import open_core_host
from tests.support.docker_environment import fake_docker_environment
from tests.support.real_git import NO_GITHUB, REAL_GIT
from tests.support.task_project import write_task

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_task
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import RunRequest
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import WorkspaceRestoreError
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api import OsThreads
from vs_sim.api.testing import ManualClock

if TYPE_CHECKING:
    from pathlib import Path


def _write_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "queue.py").write_text("VALUE = 1\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Improve the queue.\n",
    )


def _request(project_root: Path) -> RunRequest:
    return RunRequest(
        run_environment=fake_docker_environment(),
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id="workspace", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "workspace"}}),
        input_bundle=load_task(project_root),
        objective="Improve the queue.",
        exp_name="workspace",
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def test_root_workspace_revision_restore_and_retention_contract(tmp_path: Path) -> None:
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
        async with open_core_host(_request(project_root), integration) as host:
            assert host.services.state.run.facts.objective == "Improve the queue."
            workspace = host.workspaces.root
            baseline = workspace.trusted_input_baseline
            assert baseline is not None
            assert workspace.revision is not None

            candidate_file = workspace.path / "queue.py"
            candidate_file.write_text("VALUE = 2\n")
            candidate = await workspace.snapshot("implemented plan")
            assert workspace.revision == candidate
            assert await workspace.retain(candidate, label="selected-round-0004") is None

            candidate_file.write_text("VALUE = 3\n")
            await workspace.restore(baseline, clean=True)
            assert candidate_file.read_text() == "VALUE = 1\n"
            await workspace.restore(candidate, clean=True)
            assert candidate_file.read_text() == "VALUE = 2\n"
            assert await workspace.try_restore(baseline, clean=True)
            assert not await workspace.try_restore("missing-revision", clean=True)
            with pytest.raises(WorkspaceRestoreError, match="missing-revision"):
                await workspace.restore("missing-revision", clean=True)

    try:
        asyncio.run(exercise())
    finally:
        integration.close()
