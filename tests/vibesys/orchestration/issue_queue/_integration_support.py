"""Production-host fixture for issue-queue persistence tests."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationDescriptor,
    OrchestrationRegistry,
    ResumeRef,
    RunRequest,
)
from vibesys.api.testing import create_session
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.orchestration.issue_queue import PLUGIN, IssueQueueState
from vibesys.profilers import ProfilerKind
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
    """Return one small deterministic issue-queue configuration."""
    return PLUGIN.options.model_validate(
        {
            "max_rounds": 1,
            "max_attempts_per_issue": 2,
            "max_issues_per_perf_eval": 2,
            **changes,
        }
    )


def write_input(root: Path) -> Path:
    """Create a self-contained input project that can be copied and resumed."""
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Build a correct inference service.\n", encoding="utf-8")
    (root / "server.py").write_text("READY = False\n", encoding="utf-8")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        encoding="utf-8",
    )
    return root


def agent_client_factory(
    clients: Sequence[FakeAgentClient],
) -> Callable[..., FakeAgentClient]:
    """Provide one scripted client for each declared role session."""
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
    """Run the issue-queue plugin through the production runtime adapter."""
    bundle = load_input_bundle(project_root)
    selected = configured or options()
    descriptor = OrchestrationDescriptor(
        id=PLUGIN.id,
        config_version=PLUGIN.config_version,
        options=selected.model_dump(mode="json"),
    )
    request = RunRequest(
        project_root=project_root,
        orchestration=descriptor,
        config=Config.model_validate({"model": {"name": "issue-queue-plugin-test"}}),
        input_bundle=bundle,
        objective=bundle.objective,
        exp_name=resume_run_id or "issue-queue-plugin-test",
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
        session.start()
        try:
            result = await session.await_result()
        finally:
            session.close()
        workspace = _workspace_for(project_root, resume_run_id=resume_run_id)
        status = RunStatus.SUCCEEDED if result.succeeded else RunStatus.FAILED
        return status, result.run_id, workspace

    return asyncio.run(run())


def interrupted_workspace(input_root: Path) -> tuple[Path, str]:
    """Resolve the sole durable workspace left by an interrupted run."""
    workspace = _workspace_for(input_root, resume_run_id=None)
    return workspace, Project.open(workspace).state.resolve_run().run_id


def load_state(project_root: Path, run_id: str) -> IssueQueueState | None:
    """Load plugin-owned state through the project API."""
    return (
        Project.open(project_root)
        .state.portable_namespace(run_id, PLUGIN.id)
        .slot("state.json", IssueQueueState)
        .load_optional()
    )


def _workspace_for(project_root: Path, *, resume_run_id: str | None) -> Path:
    if resume_run_id is not None:
        return project_root
    projects = list((project_root.parent / f"{project_root.name}-runs").iterdir())
    assert len(projects) == 1
    return projects[0]


__all__ = [
    "InterruptedTurnError",
    "execute",
    "interrupted_workspace",
    "load_state",
    "options",
    "write_input",
]
