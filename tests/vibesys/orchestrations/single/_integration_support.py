"""Production-host fixture for explicit single-plugin persistence tests.

This fixture is intentionally the only test module that imports ``RunContext``.
Remove it once ``create_session`` exposes injectable agent/backend factories.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from vibesys.api.testing import FakeComputeBackend
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.orchestration.request import ResumeRef, RunRequest
from vibesys.orchestration.runtime import RunContext
from vibesys.orchestrations.single import PLUGIN
from vibesys.orchestrations.single.models import SingleState
from vibesys.plugin_catalog import built_in_orchestrations
from vibesys.profilers import ProfilerKind
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor, Project

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from pydantic import BaseModel

    from vibesys.events import CoreEvent
    from vs_agent.api import AgentSpec
    from vs_agent.api.testing import FakeAgentClient
    from vs_runtime.api import RunStatus


class InterruptedTurnError(RuntimeError):
    """Deterministic simulated process interruption at an agent turn."""


def options(*, max_rounds: int = 1) -> BaseModel:
    """Return a small, deterministic single-plugin run configuration."""
    return PLUGIN.options.model_validate(
        {
            "interface": "service",
            "max_rounds": max_rounds,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 10,
            "memory_layout": "files",
        }
    )


def write_input(root: Path) -> Path:
    """Create a self-contained project that can be copied and resumed."""
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
    resume_run_id: str | None = None,
    max_rounds: int = 1,
    observed_events: list[CoreEvent] | None = None,
) -> tuple[RunStatus, str, Path]:
    """Run the explicit plugin through its production host adapter."""
    bundle = load_input_bundle(project_root)
    configured = options(max_rounds=max_rounds)
    descriptor = OrchestrationDescriptor(
        id=PLUGIN.id,
        config_version=PLUGIN.config_version,
        options=configured.model_dump(mode="json"),
    )
    prepared = built_in_orchestrations().resolve(PLUGIN.id).prepare_plugin(descriptor)
    request = RunRequest(
        project_root=project_root,
        orchestration=descriptor,
        config=Config.model_validate({"model": {"name": "single-plugin-test"}}),
        input_bundle=bundle,
        objective=bundle.objective,
        exp_name=resume_run_id or "single-plugin-test",
        runs_dir=project_root.parent / f"{project_root.name}-runs",
        resume=ResumeRef(run_id=resume_run_id) if resume_run_id else None,
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )

    async def run() -> tuple[RunStatus, str, Path]:
        integration = LocalRunIntegration()
        unsubscribe = (
            integration.events.subscribe(observed_events.append)
            if observed_events is not None
            else None
        )
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
            if unsubscribe is not None:
                unsubscribe()
            integration.close()

    return asyncio.run(run())


def load_state(project_root: Path, run_id: str) -> SingleState | None:
    """Load the plugin-owned durable state through the project API."""
    return (
        Project.open(project_root)
        .state.portable_namespace(run_id, PLUGIN.id)
        .slot("state.json", SingleState)
        .load_optional()
    )
