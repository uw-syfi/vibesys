"""Public-session fixture for explicit single-plugin scenario tests."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationDescriptor,
    OrchestrationRegistry,
    ResumeRef,
    RunRequest,
)
from vibesys.api.testing import create_session
from vibesys.inputs import ProfileGuidedInput, load_input_bundle
from vibesys.orchestration.metrics import MetricSpace
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.orchestration.single import (
    PLUGIN,
    PROFILE_GUIDED_PLUGIN,
    PROFILE_GUIDED_REGISTRATION,
    REGISTRATION,
)
from vibesys.orchestration.single.models import SingleState
from vs_project.api import Project
from vs_runtime.api import RunStatus
from vs_sandbox.api import SandboxExecutionResult
from vs_sandbox.api.testing import FakeComputeBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path
    from typing import Any

    from pydantic import BaseModel

    from vibesys.events import CoreEvent
    from vs_agent.api import AgentSpec
    from vs_agent.api.testing import FakeAgentClient
    from vs_runtime.api import OrchestrationPlugin
    from vs_sandbox.api import SandboxKind
    from vs_sandbox.api.testing import FakeSandbox


class InterruptedTurnError(RuntimeError):
    """Deterministic simulated process interruption at an agent turn."""


@dataclass(frozen=True, slots=True)
class PluginRunOptions:
    """Pair one explicit plugin with its already-validated test options."""

    plugin: OrchestrationPlugin
    options: BaseModel


_ATTRIBUTION_OUTPUT = (
    '{"version":1,"cost_unit":"ms","components":['
    '{"name":"prefill","cost":0.72,"share":0.6,"evidence":["profile.json:12"]},'
    '{"name":"decode","cost":0.48,"share":0.4,"evidence":["profile.json:18"]}]}'
)


class _ProfileAttributionBackend(FakeComputeBackend):
    """Provide one deterministic captured profile result to the real adapter."""

    # This FakeComputeBackend override matches its real backend seam's setup inputs.
    def make_sandbox(
        self,
        kind: SandboxKind,
        **kwargs: Any,  # noqa: ANN401  # LW-040202 [ANN401]; the production factory's extensible keyword boundary is intentionally open.
    ) -> FakeSandbox:
        sandbox = cast("FakeSandbox", super().make_sandbox(kind, **kwargs))
        output_path = "profile-result.json"
        sandbox.script(
            "mktemp",
            SandboxExecutionResult(output=output_path, exit_code=0, stdout=output_path),
        )
        sandbox.script(
            f"profile-tool --vs-output {output_path}",
            SandboxExecutionResult(output="", exit_code=0),
        )
        sandbox.script(
            f"cat {output_path}",
            SandboxExecutionResult(
                output=_ATTRIBUTION_OUTPUT, exit_code=0, stdout=_ATTRIBUTION_OUTPUT
            ),
        )
        sandbox.script(
            f"rm -f {output_path}",
            SandboxExecutionResult(output="", exit_code=0),
        )
        return sandbox


def options(*, max_rounds: int = 1, interface: str = "service") -> BaseModel:
    """Return a small, deterministic single-plugin run configuration."""
    return PLUGIN.options.model_validate(
        {
            "interface": interface,
            "max_rounds": max_rounds,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 10,
        }
    )


def profile_options(*, interface: str = "inprocess") -> PluginRunOptions:
    """Build the fixed profile-single golden configuration."""
    return PluginRunOptions(
        plugin=PROFILE_GUIDED_PLUGIN,
        options=PROFILE_GUIDED_PLUGIN.options.model_validate(
            {
                "interface": interface,
                "max_rounds": 1,
                "max_retries_per_round": 2,
                "judge_every": 1,
                "official_eval_every": 1,
                "metric_space": MetricSpace(),
                "profile_guided": ProfileGuidedInput(command=("profile-tool",), timeout_seconds=73),
            }
        ),
    )


def write_input(
    root: Path,
    *,
    domain: str = "generic",
    objective: str = "Improve the queue.",
) -> Path:
    """Create a self-contained project that can be copied and resumed."""
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text(f"{objective}\n", encoding="utf-8")
    (root / "queue.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "vibesys.input.toml").write_text(
        f'version = 1\n[agent]\ndomain = "{domain}"\n'
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
    options_override: BaseModel | PluginRunOptions | None = None,
    observed_events: list[CoreEvent] | None = None,
) -> tuple[RunStatus, str, Path]:
    """Run the explicit plugin through the public product session."""
    bundle = load_input_bundle(project_root)
    registration = REGISTRATION
    if isinstance(options_override, PluginRunOptions):
        registration = PROFILE_GUIDED_REGISTRATION
        configured = options_override.options
    else:
        configured = options() if options_override is None else options_override
    plugin = registration.plugin
    descriptor = OrchestrationDescriptor(
        id=plugin.id,
        config_version=plugin.config_version,
        options=configured.model_dump(mode="json"),
    )
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
        def emit(event: CoreEvent) -> None:
            if observed_events is not None:
                observed_events.append(event)

        registry = OrchestrationRegistry()
        registry.register(registration)
        session = create_session(
            request,
            sink=emit,
            registry=registry,
            agent_client_factory=agent_client_factory(clients),
            backend_factory=lambda *_args, **_kwargs: _ProfileAttributionBackend(),
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


def load_state(project_root: Path, run_id: str) -> SingleState | None:
    """Load the plugin-owned durable state through the project API."""
    return (
        Project.open(project_root)
        .state.portable_namespace(run_id, PLUGIN.id)
        .slot("state.json", SingleState)
        .load_optional()
    )
