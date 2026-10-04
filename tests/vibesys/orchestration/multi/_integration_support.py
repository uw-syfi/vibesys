"""Public-session fixture for explicit multi-plugin scenario tests."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from launch.testing import create_session
from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationDescriptor,
    OrchestrationRegistry,
    ResumeRef,
    RunRequest,
)
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.multi import PLUGIN
from vibesys.orchestration.multi.models import MultiState
from vibesys.orchestration.profilers import ProfilerKind
from vs_project.api import Project
from vs_runtime.api import RunStatus
from vs_sandbox.api.testing import FakeComputeBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from pydantic import BaseModel

    from vibesys.events import CoreEvent
    from vs_agent.api import AgentSpec
    from vs_agent.api.testing import FakeAgentClient


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


def _discard_event(event: CoreEvent) -> None:
    del event


def execute(
    project_root: Path,
    clients: Sequence[FakeAgentClient],
    *,
    configured: BaseModel | None = None,
    resume_run_id: str | None = None,
) -> tuple[RunStatus, str, Path]:
    """Run the explicit multi plugin through the public product session."""
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
        registry = OrchestrationRegistry()
        registry.register_plugin(PLUGIN)
        session = create_session(
            request,
            sink=_discard_event,
            registry=registry,
            agent_client_factory=agent_client_factory(clients),
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(),
        )
        try:
            session.start()
            result = await session.await_result()
        finally:
            session.close()
        status = RunStatus.SUCCEEDED if result.succeeded else RunStatus.FAILED
        workspace = _workspace_for(
            project_root,
            run_id=result.run_id,
            resume_run_id=resume_run_id,
        )
        return status, result.run_id, workspace

    return asyncio.run(run())


def _workspace_for(
    project_root: Path,
    *,
    run_id: str,
    resume_run_id: str | None,
) -> Path:
    if resume_run_id is not None:
        return project_root
    return project_root.parent / f"{project_root.name}-runs" / run_id


def load_state(project_root: Path, run_id: str) -> MultiState | None:
    """Load the plugin-owned durable aggregate through the project API."""
    return (
        Project.open(project_root)
        .state.portable_namespace(run_id, PLUGIN.id)
        .slot("state.json", MultiState)
        .load_optional()
    )
