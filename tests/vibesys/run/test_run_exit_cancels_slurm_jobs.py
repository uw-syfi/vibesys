"""A run that exits cancels the Slurm jobs its evaluations submitted."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.plugin import capability_plugin

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import RunRequest
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api.infrastructure import RunEnvironmentSpec
from vs_slurm.fake_connector import JOB_ID, SUBMITTED_FILE, recorded_commands

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api import Run

_PLUGIN = capability_plugin("slurm-exit")


class _OrchestrationFailedError(RuntimeError):
    """The orchestration's own failure, raised while a benchmark is pending."""


def _write_slurm_config(path: Path, state: Path) -> None:
    connector = json.dumps([sys.executable, "-m", "vs_slurm.fake_connector", str(state)])
    # A one-hour poll interval: the job leaves the queue only through scancel.
    path.write_text(
        "[slurm]\n"
        'name = "fake"\n'
        'remote_workspace_root = "/remote/runs"\n'
        "poll_interval_seconds = 3600.0\n"
        f'transport = {{ kind = "connector", command = {connector} }}\n',
        encoding="utf-8",
    )


def _write_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n'
        '[benchmark]\ncommand = ["run-benchmark"]\nresult_protocol = 2\n'
    )


def _request(project_root: Path, config_path: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id=_PLUGIN.id, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "slurm-exit"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name="slurm-exit",
        agent_backend="cli",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
        run_environment=RunEnvironmentSpec("slurm", {"config_path": str(config_path)}),
    )


def test_an_orchestration_failure_cancels_a_pending_benchmark_job(tmp_path: Path) -> None:
    state = tmp_path / "cluster"
    state.mkdir()
    os.mkfifo(state / SUBMITTED_FILE)
    config_path = tmp_path / "slurm.toml"
    _write_slurm_config(config_path, state)
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def orchestrate(run: Run) -> None:
        # A background benchmark the orchestration never awaits, like an
        # input-baseline measurement, still queued when the orchestration fails.
        pending = asyncio.create_task(run.evaluation.benchmark(run.workspaces.root))
        submitted = await asyncio.to_thread((state / SUBMITTED_FILE).read_text, encoding="utf-8")
        assert submitted == JOB_ID
        assert not pending.done()
        raise _OrchestrationFailedError

    async def exercise() -> None:
        async with open_product_run_host(
            _request(project_root, config_path), integration, plugin=_PLUGIN
        ) as run:
            await orchestrate(run)

    try:
        with pytest.raises(_OrchestrationFailedError):
            asyncio.run(exercise())
    finally:
        integration.close()

    assert f"scancel {JOB_ID}" in recorded_commands(state)
