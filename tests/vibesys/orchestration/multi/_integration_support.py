"""Production-host fixture for explicit multi-plugin persistence and isolation tests.

This is the only multi-plugin test helper that imports ``RunHost``. The
behavior tests stay on the public plugin/runtime contracts and use this seam
only when process-persisted state or real workspace restoration matters.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.orchestration.multi import PLUGIN
from vibesys.orchestration.multi.models import MultiState
from vibesys.orchestration.request import ResumeRef, RunRequest
from vibesys.profilers import ProfilerKind
from vibesys.run.host import open_product_run_host
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
    """Return a small deterministic multi-plugin configuration."""
    return PLUGIN.options.model_validate(
        {
            "interface": "service",
            "max_rounds": 1,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 10,
            "memory_layout": "directories",
            **changes,
        }
    )


def write_input(root: Path) -> Path:
    """Create a self-contained project that the production host can resume."""
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n", encoding="utf-8")
    (root / "queue.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        encoding="utf-8",
    )
    return root


def agent_client_factory(clients: Sequence[FakeAgentClient]) -> Callable[..., FakeAgentClient]:
    """Provide one scripted client for each lazily opened agent handle."""
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
    """Run the explicit multi plugin through its production host adapter."""
    bundle = load_input_bundle(project_root)
    selected = configured or options()
    request = RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(
            id=PLUGIN.id,
            config_version=PLUGIN.config_version,
            options=selected.model_dump(mode="json"),
        ),
        config=Config.model_validate({"model": {"name": "multi-plugin-test"}}),
        input_bundle=bundle,
        objective=bundle.objective,
        exp_name=resume_run_id or "multi-plugin-test",
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
            async with open_product_run_host(
                request,
                integration,
                agent_client_factory=agent_client_factory(clients),
                backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(),
                plugin=PLUGIN,
            ) as host:
                status = await PLUGIN.orchestrate(host, selected)
                return status, host.run_id, host.workspaces.root.path
        finally:
            integration.close()

    return asyncio.run(run())


def load_state(project_root: Path, run_id: str) -> MultiState | None:
    """Load the plugin-owned durable aggregate through the project API."""
    return (
        Project.open(project_root)
        .state.portable_namespace(run_id, PLUGIN.id)
        .slot("state.json", MultiState)
        .load_optional()
    )
