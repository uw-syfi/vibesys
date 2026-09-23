"""Adapter from RunRequest to the existing agent loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api._orchestrations._common import (
    _agent_outer_loop,
    _required_objective,
    built_in_description,
    resolved_run_id,
)
from vibesys.api._orchestrations.agent_readmodel import (
    project_committed_run_view,
    project_run_view,
)
from vibesys.api._orchestrations.agent_state import load_agent_run_state
from vibesys.api.contracts import LoopKind, RunStatus
from vibesys.loops.agent.model import AgentRunState

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.api._orchestrations.contracts import RunDescription
    from vibesys.api.contracts import RunRequest, RunView
    from vibesys.orchestration import ResumeProjection
    from vibesys.run.integration import LocalRunIntegration
    from vs_project.api import OrchestrationRunManifest, Project


class AgentOrchestration:
    """Preserve the existing agent loop call contract."""

    def history_namespaces(self) -> tuple[str, ...]:
        """Expose this policy's portable state to run history queries."""
        return ("agent",)

    def describe(self, request: RunRequest) -> RunDescription:
        return built_in_description(request, round_budget=True)

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        # The existing public read model exposes hypotheses only for `agent`.
        state = (
            load_agent_run_state(project, run_id) or AgentRunState()
            if loop == LoopKind.AGENT
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
        if namespace != "agent":
            return None
        return project_committed_run_view(state, run_id=run_id)

    def resume_projection(self, manifest: OrchestrationRunManifest) -> ResumeProjection:
        """Project agent-owned settings without constructing v3 configuration."""
        from vibesys.loops.agent.orchestration import (  # noqa: PLC0415  # tracked: #288
            resume_projection,
        )

        return resume_projection(manifest)

    def execute(self, request: RunRequest, integration: LocalRunIntegration) -> bool:
        from vibesys.loops.agent.loop import run_agent_loop  # noqa: PLC0415  # tracked: #288

        bundle = request.input_bundle
        resuming = request.resume is not None
        return run_agent_loop(
            config=request.config,
            exp_name=resolved_run_id(request),
            runs_dir=request.runs_dir,
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
            objective=_required_objective(request),
            metrics=request.metrics,
            max_rounds=request.max_rounds if request.max_rounds is not None else 24,
            max_retries_per_round=request.max_retries_per_round,
            judge_every=request.judge_every,
            official_eval_every=request.official_eval_every,
            memory_layout=request.memory_layout,
            start_round=None if resuming else 1,
            existing=resuming,
            operator_constraints=request.operator_constraints,
            debug=request.debug,
            profiler_kind=request.profiler_kind,
            skills_dirs=request.skills_dirs,
            run_environment=request.run_environment,
            agent_backend=request.agent_backend,
            cli_provider=request.cli_provider,
            backend=request.backend,
            modality=request.modality,
            domain=bundle.domain,
            interface=request.interface,
            inner_loop=request.inner_loop,
            remote_repo=request.remote_repo,
            repo_visibility=request.repo_visibility,
            integration=integration,
            outer_loop=_agent_outer_loop(request.loop),
            profile_guided=bundle.manifest.profile_guided,
        )
