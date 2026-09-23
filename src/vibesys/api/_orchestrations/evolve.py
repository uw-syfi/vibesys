"""Adapter from RunRequest to the existing evolve loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api._orchestrations._common import (
    _required_objective,
    built_in_description,
    resolved_run_id,
)
from vibesys.loops.evolve.orchestration import resume_projection

if TYPE_CHECKING:
    from vibesys.api._orchestrations.contracts import RunDescription
    from vibesys.api.contracts import RunRequest
    from vibesys.orchestration import ResumeProjection
    from vibesys.run.integration import LocalRunIntegration
    from vs_project.api import OrchestrationRunManifest


class EvolveOrchestration:
    """Preserve the existing evolve loop call contract."""

    def describe(self, request: RunRequest) -> RunDescription:
        return built_in_description(request, round_budget=False)

    def resume_projection(self, manifest: OrchestrationRunManifest) -> ResumeProjection:
        """Project evolve-owned settings without constructing v3 configuration."""
        return resume_projection(manifest)

    def execute(self, request: RunRequest, integration: LocalRunIntegration) -> bool:
        from vibesys.loops.evolve.loop import run_evolve_loop  # noqa: PLC0415  # tracked: #288

        bundle = request.input_bundle
        resuming = request.resume is not None
        return run_evolve_loop(
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
            accuracy_timeout_seconds=bundle.manifest.accuracy.timeout_seconds,
            benchmark_result=bundle.benchmark_result,
            benchmark_result_protocol=bundle.benchmark_result_protocol,
            benchmark_timeout_seconds=bundle.manifest.benchmark.timeout_seconds,
            objective=_required_objective(request),
            max_generations=request.max_generations,
            children_per_generation=request.children_per_generation,
            k_top_inspirations=request.k_top_inspirations,
            k_random_inspirations=request.k_random_inspirations,
            selection_temperature=request.selection_temperature,
            seed=request.seed,
            existing=resuming,
            debug=request.debug,
            profiler_kind=request.profiler_kind,
            skills_dirs=request.skills_dirs,
            run_environment=request.run_environment,
            agent_backend=request.agent_backend,
            cli_provider=request.cli_provider,
            backend=request.backend,
            modality=request.modality,
            domain=bundle.domain,
            space=request.space,
            frontier_bias=request.frontier_bias,
            bootstrap_max_attempts=request.bootstrap_max_attempts,
            keep_deployments=request.keep_deployments,
            max_parallelism=request.max_parallelism,
            search_policy=request.search_policy,
            openevolve_config=request.openevolve_config,
            remote_repo=request.remote_repo,
            repo_visibility=request.repo_visibility,
            integration=integration,
        )
