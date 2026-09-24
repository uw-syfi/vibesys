"""Shared compatibility mapping for the built-in agent loop policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vibesys.loops.agent.model import AgentRunState
from vibesys.loops.agent.policy_rounds import validate_inner_policy
from vibesys.loops.agent.read_state import load_agent_run_state
from vibesys.loops.agent.readmodel import (
    project_committed_run_view,
    project_run_view,
)
from vibesys.loops.legacy_bridge import (
    LegacyBuiltinDefaults,
    built_in_description,
    legacy_integration,
    legacy_request,
    required_objective,
)
from vibesys.orchestration._common import resolved_run_id

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.api.contracts import RunStatus, RunView
    from vibesys.api.run_request import RunRequestLike
    from vibesys.loops.legacy_request import RunRequest
    from vibesys.orchestration import ResumeProjection
    from vibesys.orchestration.contracts import RunDescription
    from vibesys.runtime import VibeSysRuntime
    from vs_project.api import OrchestrationRunManifest, Project


class AgentBuiltinDefaults(LegacyBuiltinDefaults):
    """Read model and resume behavior shared by agent and profile guidance."""

    namespace = "agent"
    include_agent_state: bool

    def describe(self, request: RunRequestLike) -> RunDescription:
        """Describe the agent loop's round budget and expected roles."""
        return built_in_description(legacy_request(request), round_budget=True)

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Project persisted agent state for the selected outer policy."""
        # The existing public read model exposes hypotheses only for `agent`.
        state = (
            load_agent_run_state(project, run_id) or AgentRunState()
            if self.include_agent_state
            else AgentRunState()
        )
        return project_run_view(
            state,
            run_id=run_id,
            status=status,
            experiment_revision=state.experiment_revision,
            loop=loop,
        )

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project live agent commits from the agent namespace."""
        if namespace != "agent":
            return None
        return project_committed_run_view(state, run_id=run_id)

    def resume_projection(self, manifest: OrchestrationRunManifest) -> ResumeProjection:
        """Restore agent-owned settings from a run manifest."""
        from vibesys.loops.agent.orchestration import resume_projection  # noqa: PLC0415

        return resume_projection(manifest)


def run_agent_policy(
    request: RunRequestLike,
    runtime: VibeSysRuntime,
    *,
    outer_loop: Literal["agent", "profile-guided"],
    inner_loop: Literal["multi-agent", "single-agent"],
) -> bool:
    """Map a selected agent policy to the existing resumable loop entrypoint."""
    from vibesys.loops.agent.loop import run_agent_loop  # noqa: PLC0415  # tracked: #288

    legacy = legacy_request(request)
    bundle = legacy.input_bundle
    resuming = legacy.resume is not None
    return run_agent_loop(
        config=legacy.config,
        exp_name=resolved_run_id(legacy),
        runs_dir=legacy.runs_dir,
        input_path=str(bundle.root),
        task_name=bundle.task_name,
        task_root=bundle.task_root,
        accuracy_command=bundle.accuracy_command_display,
        benchmark_command=bundle.benchmark_command_display,
        workspace_sources=bundle.workspace_sources,
        evaluator_path=bundle.evaluator_path,
        evaluator_package_root=bundle.evaluator_package_root,
        benchmark_result=bundle.benchmark_result,
        benchmark_result_protocol=bundle.benchmark_result_protocol,
        accuracy_timeout_seconds=bundle.manifest.accuracy.timeout_seconds,
        benchmark_timeout_seconds=bundle.manifest.benchmark.timeout_seconds,
        objective=required_objective(legacy),
        metrics=legacy.metrics,
        max_rounds=legacy.max_rounds if legacy.max_rounds is not None else 24,
        max_retries_per_round=legacy.max_retries_per_round,
        judge_every=legacy.judge_every,
        official_eval_every=legacy.official_eval_every,
        memory_layout=legacy.memory_layout,
        start_round=None if resuming else 1,
        existing=resuming,
        operator_constraints=legacy.operator_constraints,
        debug=legacy.debug,
        profiler_kind=legacy.profiler_kind,
        skills_dirs=legacy.skills_dirs,
        run_environment=legacy.run_environment,
        agent_backend=legacy.agent_backend,
        cli_provider=legacy.cli_provider,
        backend=legacy.backend,
        modality=legacy.modality,
        domain=bundle.domain,
        interface=legacy.interface,
        inner_loop=inner_loop,
        remote_repo=legacy.remote_repo,
        repo_visibility=legacy.repo_visibility,
        integration=legacy_integration(runtime),
        outer_loop=outer_loop,
        profile_guided=bundle.manifest.profile_guided,
    )


@dataclass(frozen=True, slots=True)
class MultiAgentExecution:
    """Execute the built-in multi-agent round policy."""

    outer_loop: Literal["agent", "profile-guided"]

    def execute(self, request: RunRequestLike, runtime: VibeSysRuntime) -> bool:
        """Execute the multi-agent policy under the configured outer loop."""
        return run_agent_policy(
            request, runtime, outer_loop=self.outer_loop, inner_loop="multi-agent"
        )


@dataclass(frozen=True, slots=True)
class SingleAgentExecution:
    """Execute the built-in single-agent round policy."""

    outer_loop: Literal["agent", "profile-guided"]

    def execute(self, request: RunRequestLike, runtime: VibeSysRuntime) -> bool:
        """Execute the single-agent policy under the configured outer loop."""
        return run_agent_policy(
            request, runtime, outer_loop=self.outer_loop, inner_loop="single-agent"
        )


def agent_execution_for(
    request: RunRequest, *, outer_loop: Literal["agent", "profile-guided"]
) -> MultiAgentExecution | SingleAgentExecution:
    """Select the inner agent policy using the legacy request's inner-loop option."""
    implementations = {
        "multi-agent": MultiAgentExecution,
        "single-agent": SingleAgentExecution,
    }
    validate_inner_policy(request.inner_loop)
    return implementations[request.inner_loop](outer_loop)
