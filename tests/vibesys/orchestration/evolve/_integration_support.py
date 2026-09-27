"""Production-host fixture for explicit evolve persistence tests.

This is the only evolve-plugin test helper that imports ``RunContext``. Remove
it once ``create_session`` exposes injectable agent and compute factories.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.orchestration.evolve import PLUGIN
from vibesys.orchestration.evolve.models import EvolveState
from vibesys.orchestration.request import ResumeRef, RunRequest
from vibesys.orchestration.runtime import RunContext
from vibesys.plugin_catalog import built_in_orchestrations
from vibesys.profilers import ProfilerKind
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor, Project
from vs_sandbox.api.testing import FakeComputeBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from pydantic import BaseModel

    from vs_agent.api import AgentSpec
    from vs_agent.api.testing import FakeAgentClient
    from vs_runtime.api import RunStatus


class InterruptedTurnError(RuntimeError):
    """Deterministic simulated process interruption at an agent turn."""


def options(**changes: object) -> BaseModel:
    """Return a small deterministic evolve configuration."""
    return PLUGIN.options.model_validate(
        {
            "max_generations": 1,
            "children_per_generation": 1,
            "k_top_inspirations": 0,
            "k_random_inspirations": 0,
            "selection_temperature": 1.0,
            "seed": 7,
            "frontier_bias": 0.7,
            "bootstrap_max_attempts": 2,
            "keep_deployments": False,
            "max_parallelism": 1,
            **changes,
        }
    )


def write_input(root: Path) -> Path:
    """Create one self-contained input project that can be copied and resumed."""
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Increase queue throughput.\n", encoding="utf-8")
    (root / "queue.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        encoding="utf-8",
    )
    return root


def agent_client_factory(
    clients: Sequence[FakeAgentClient],
) -> Callable[..., FakeAgentClient]:
    """Provide one scripted client for each explicitly opened role session."""
    available = deque(clients)

    def create(*, spec: AgentSpec, **_kwargs: object) -> FakeAgentClient:
        del spec
        return available.popleft()

    return create


def execute(
    project_root: Path,
    clients: Sequence[FakeAgentClient],
    *,
    configured: BaseModel | None = None,
    resume_run_id: str | None = None,
) -> tuple[RunStatus, str, Path]:
    """Run the explicit evolve plugin through its production host adapter."""
    bundle = load_input_bundle(project_root)
    selected = configured or options()
    descriptor = OrchestrationDescriptor(
        id=PLUGIN.id,
        config_version=PLUGIN.config_version,
        options=selected.model_dump(mode="json"),
    )
    prepared = built_in_orchestrations().resolve(PLUGIN.id).prepare_plugin(descriptor)
    request = RunRequest(
        project_root=project_root,
        orchestration=descriptor,
        config=Config.model_validate({"model": {"name": "evolve-plugin-test"}}),
        input_bundle=bundle,
        objective=bundle.objective,
        exp_name=resume_run_id or "evolve-plugin-test",
        runs_dir=project_root.parent / f"{project_root.name}-runs",
        resume=ResumeRef(run_id=resume_run_id) if resume_run_id else None,
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )

    async def run() -> tuple[RunStatus, str, Path]:
        integration = LocalRunIntegration()
        try:
            async with RunContext.open(
                request,
                integration,
                setup=prepared.setup,
                agent_client_factory=agent_client_factory(clients),
                backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(),
                plugin=PLUGIN,
            ) as host:
                status = await PLUGIN.orchestrate(host, prepared.options)
                return status, host.run_id, host.workspaces.root.path
        finally:
            integration.close()

    return asyncio.run(run())


def load_state(project_root: Path, run_id: str) -> EvolveState | None:
    """Load the plugin-owned durable aggregate through the project API."""
    return (
        Project.open(project_root)
        .state.portable_namespace(run_id, PLUGIN.id)
        .slot("state.json", EvolveState)
        .load_optional()
    )


__all__ = ["InterruptedTurnError", "execute", "load_state", "options", "write_input"]
