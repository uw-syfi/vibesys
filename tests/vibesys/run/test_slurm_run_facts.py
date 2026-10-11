"""A Slurm run's facts follow the loop's evaluation surface and keep the task's accuracy gate (#1649).

A loop whose agents have the evaluation tool (the dynamic loop) is given no accuracy or
benchmark command by the Slurm environment, because the agent requests trusted evaluation
through the tool. A loop without it keeps the gate commands its trusted evaluation runs. Either
way the dynamic loop decides whether to run correctness from ``accuracy_configured``, which
must follow the task, not the agent-facing paths.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from tests.support.docker_environment import host_container_backend
from tests.support.task_project import write_task
from tests.vibesys.orchestration.dynamic.core_policy._inputs import (
    bounds_of,
    options_of,
    resolved,
    run_request,
)

from vibesys.api.request import RunEnvironmentSpec
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.dynamic_core import resolve_core_policy
from vibesys.inputs import load_task
from vibesys.orchestration.dynamic import DynamicOptions
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import RunRequest
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api import reap_orphaned_agents
from vs_agent.api.testing import FakeConfinement, FakeDockerBuildRunner
from vs_core.api import AssessmentKind
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import AgentRole, AgentTool, OrchestrationPlugin, RunFacts, RunStatus
from vs_sandbox.api.testing import FakeDockerEngine

if TYPE_CHECKING:
    from pathlib import Path

_SLURM_LOCAL = """[slurm]
name = "test-cluster"
remote_workspace_root = "/shared/vibesys"

[slurm.transport]
kind = "local"
shell_command = ["bash", "-c"]
"""


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


async def _orchestrate(run: object, _options: BaseModel) -> RunStatus:
    del run
    return RunStatus.SUCCEEDED


# An older loop: no agent has the evaluation tool.
_GATE_LOOP = OrchestrationPlugin(
    id="slurm-facts", agents=(), options=_Options, orchestrate=_orchestrate
)
# A loop whose agent does.
_TOOL_LOOP = OrchestrationPlugin(
    id="slurm-facts",
    agents=(
        AgentRole(id="probe", system_prompt="probe", extra_tools=(AgentTool(id="evaluation"),)),
    ),
    options=_Options,
    orchestrate=_orchestrate,
)


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
                "docker": FakeDockerEngine(),
                "orphan_reaper": partial(
                    reap_orphaned_agents, confinement_for=lambda _id: FakeConfinement()
                ),
            },
        ),
        project_root=root,
        orchestration=OrchestrationDescriptor(id=_GATE_LOOP.id, config_version=1, options={}),
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
    integration = LocalRunIntegration()

    async def exercise() -> RunFacts:
        async with open_product_run_host(
            _request(root, config),
            integration,
            plugin=plugin,
            backend_factory=host_container_backend,
        ) as run:
            return run.facts

    try:
        return asyncio.run(exercise())
    finally:
        integration.close()


def test_a_loop_without_the_evaluation_tool_keeps_the_gate_commands_on_slurm(
    tmp_path: Path,
) -> None:
    facts = _facts(tmp_path, _GATE_LOOP)

    assert (facts.accuracy_command or "").endswith("vibesys-gate --gate accuracy")
    assert (facts.benchmark_command or "").endswith("vibesys-gate --gate benchmark")
    assert facts.accuracy_configured


def test_the_dynamic_loop_still_requires_correctness_on_slurm(tmp_path: Path) -> None:
    facts = _facts(tmp_path, _TOOL_LOOP)

    # The agent is handed no command for trusted evaluation ...
    assert facts.accuracy_command is None
    assert facts.benchmark_command is None
    # ... and the task's gates are still configured, so the dynamic loop still requires them.
    assert facts.accuracy_configured
    dynamic_root = tmp_path / "dynamic"
    dynamic = run_request(dynamic_root)
    policy = resolve_core_policy(
        DynamicOptions.model_validate(options_of(dynamic).model_dump()),
        bounds_of(dynamic),
        replace(resolved(dynamic_root), facts=facts),
    )
    required = policy.requirements.required_assessments
    assert AssessmentKind.CORRECTNESS in required
