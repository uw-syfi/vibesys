"""Public root-workspace behavior over the production run host."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.plugin import capability_plugin

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.request import RunRequest
from vibesys.profilers import ProfilerKind
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import WorkspaceRestoreError

if TYPE_CHECKING:
    from pathlib import Path


_PLUGIN = capability_plugin("workspace")


def _write_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _request(project_root: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id="workspace", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "workspace"}}),
        input_bundle=load_input_bundle(project_root),
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
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with open_product_run_host(
            _request(project_root),
            integration,
            plugin=_PLUGIN,
        ) as ctx:
            assert ctx.facts.objective == "Improve the queue."
            workspace = ctx.workspaces.root
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
