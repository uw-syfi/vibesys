"""A headless run, for a test to signal, whose strategy awaits a Slurm job.

``python -m tests.e2e._signalled_run PROJECT SLURM_CONFIG`` runs the
headless supervisor over the product session, rendering to stdout, with a
strategy that measures the input baseline on the configured (Fake) Slurm cluster, so the
run is waiting on a job until a signal ends it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from tests.support.docker_environment import host_container_backend
from tests.support.real_git import REAL_PROCESSES
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.vibesys.orchestration.plugin import capability_plugin

from entrypoints.production import production_timing
from entrypoints.run import supervise
from headless import run as render_run
from launch import LaunchSettings, default_runs
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_task
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.plugin_catalog import OrchestrationRegistry
from vibesys.run.contracts import RunRequest
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient, FakeDockerBuildRunner
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api.infrastructure import RunEnvironmentSpec
from vs_sim.api import (
    LoopSignalSource,
    OsThreads,
    SubprocessPipedProcessLauncher,
    ThreadBlockingRunner,
    UnixNetwork,
)

PLUGIN_ID = "slurm-signal"


def _resumable_client(**_kwargs: object) -> FakeAgentClient:
    return FakeAgentClient(
        capabilities=AgentCapabilities(provider_session_resume=True), threads=OsThreads()
    )


def main(project_root: Path, slurm_config: Path) -> None:
    """Run until the job ends or a signal ends the run."""
    registry = OrchestrationRegistry()
    registry.register_plugin(
        capability_plugin(PLUGIN_ID, strategy=SkeletonStrategy.baseline_probe())
    )
    request = RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id=PLUGIN_ID, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "claude-sonnet-5"}}),
        input_bundle=load_task(project_root),
        objective="Improve the queue.",
        exp_name=PLUGIN_ID,
        agent_backend="cli",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
        run_environment=RunEnvironmentSpec(
            "slurm", {"config_path": str(slurm_config), "build_runner": FakeDockerBuildRunner()}
        ),
    )
    runs = default_runs(
        LaunchSettings(
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            timing=production_timing(),
            processes=REAL_PROCESSES,
            piped_processes=SubprocessPipedProcessLauncher(),
            registry=registry,
            agent_client_factory=_resumable_client,
            backend_factory=host_container_backend,
        )
    )

    async def execute() -> None:
        handle = runs.start(request)
        await supervise(handle, render_run(handle), signals=LoopSignalSource())

    asyncio.run(execute())


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
