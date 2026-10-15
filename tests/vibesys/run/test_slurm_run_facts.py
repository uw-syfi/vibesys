"""A Slurm run's facts give agents no evaluation command and keep the task's accuracy gate.

The Slurm environment gives no agent an accuracy or benchmark command, because every agent
requests trusted evaluation through the evaluation tool. A strategy still decides whether to
run correctness from ``accuracy_configured``, which must follow the task, not the agent-facing
paths.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from tests.support.core_host_world import open_core_host
from tests.support.core_plugins import capability_plugin
from tests.support.real_git import NO_GITHUB, REAL_GIT
from tests.support.task_project import write_task
from tests.vibesys.orchestration.dynamic.core_policy._inputs import (
    plan_of,
    resolved,
    write_project,
)

from vibesys.api.request import RunEnvironmentSpec
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_task
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import RunRequest
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api import reap_orphaned_agents
from vs_agent.api.testing import FakeConfinement, FakeDockerBuildRunner
from vs_core.api import AssessmentKind
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api.testing import idle_environment_system
from vs_sandbox.api.testing import FakeDockerEngine
from vs_sim.api import OsThreads
from vs_sim.api.testing import FakePipedProcessLauncher, ManualClock

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api import OrchestrationPlugin, RunFacts

_SLURM_LOCAL = """[slurm]
name = "test-cluster"
remote_workspace_root = "/shared/vibesys"

[slurm.transport]
kind = "local"
shell_command = ["bash", "-c"]
"""


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_PLUGIN = capability_plugin("slurm-facts", options=_Options)


def _write_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "queue.py").write_text("VALUE = 1\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Improve the queue.\n",
    )


def _request(root: Path, config: Path) -> RunRequest:
    return RunRequest(
        run_environment=RunEnvironmentSpec(
            "slurm",
            {
                "config_path": str(config),
                "build_runner": FakeDockerBuildRunner(),
                "docker": FakeDockerEngine(launcher=FakePipedProcessLauncher()),
                "orphan_reaper": partial(
                    reap_orphaned_agents, confinement_for=lambda _id: FakeConfinement()
                ),
            },
        ),
        project_root=root,
        orchestration=OrchestrationDescriptor(id=_PLUGIN.id, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "slurm-facts"}}),
        input_bundle=load_task(root),
        objective="Improve the queue.",
        exp_name="slurm-facts",
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _facts(tmp_path: Path, plugin: OrchestrationPlugin) -> RunFacts:
    root = tmp_path / "project"
    _write_project(root)
    config = tmp_path / "slurm.toml"
    config.write_text(_SLURM_LOCAL, encoding="utf-8")
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )

    async def exercise() -> RunFacts:
        async with open_core_host(_request(root, config), integration, plugin=plugin) as host:
            return host.facts

    try:
        return asyncio.run(exercise())
    finally:
        integration.close()


def test_the_dynamic_loop_still_requires_correctness_on_slurm(tmp_path: Path) -> None:
    facts = _facts(tmp_path, _PLUGIN)

    # The agent is handed no command for trusted evaluation: it uses the evaluation tool ...
    assert facts.accuracy_command is None
    assert facts.benchmark_command is None
    # ... and the task's gates are still configured, so the dynamic loop still requires them.
    assert facts.accuracy_configured
    dynamic_root = tmp_path / "dynamic"
    write_project(dynamic_root)
    plan = plan_of(replace(resolved(dynamic_root), facts=facts))
    assert AssessmentKind.CORRECTNESS in plan.requirements.required_assessments
