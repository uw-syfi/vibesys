"""A headless run, for a test to signal, whose orchestration awaits a Slurm job.

``python -m tests.headless._signalled_run PROJECT SLURM_CONFIG`` runs the
headless supervisor over the product session, rendering to stdout, with a
policy that awaits one benchmark on the configured (Fake) Slurm cluster. A
stop does not interrupt the benchmark, as a stop does not interrupt in-flight
evaluations of the dynamic policy, so only a cancellation ends the run early.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.plugin import EmptyOptions

from entrypoints.run import supervise
from headless import run as render_run
from launch import LaunchSettings, default_runs
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.plugin_catalog import OrchestrationRegistry
from vibesys.run.contracts import RunRequest
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import OrchestrationPlugin, RunStatus
from vs_runtime.api.infrastructure import RunEnvironmentSpec
from vs_sandbox.api import create_compute_backend

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_agent.api import AgentClientProtocol
    from vs_runtime.api import Run

PLUGIN_ID = "slurm-signal"


class _NoAgentsError(AssertionError):
    def __init__(self) -> None:
        super().__init__("the signalled run starts no agents")


async def _await_benchmark(run: Run, _options: BaseModel) -> RunStatus:
    await run.evaluation.benchmark(run.workspaces.root)
    return RunStatus.SUCCEEDED


def _no_agents(**_kwargs: object) -> AgentClientProtocol:
    raise _NoAgentsError


def main(project_root: Path, slurm_config: Path) -> None:
    """Run until the job ends or a signal ends the run."""
    plugin = OrchestrationPlugin(
        id=PLUGIN_ID, agents=(), options=EmptyOptions, orchestrate=_await_benchmark
    )
    registry = OrchestrationRegistry()
    registry.register_plugin(plugin)
    request = RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id=PLUGIN_ID, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": PLUGIN_ID}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name=PLUGIN_ID,
        agent_backend="cli",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
        run_environment=RunEnvironmentSpec("slurm", {"config_path": str(slurm_config)}),
    )
    runs = default_runs(
        LaunchSettings(
            registry=registry,
            agent_client_factory=_no_agents,
            backend_factory=create_compute_backend,
        )
    )

    async def execute() -> None:
        handle = runs.start(request)
        await supervise(handle, render_run(handle))

    asyncio.run(execute())


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
