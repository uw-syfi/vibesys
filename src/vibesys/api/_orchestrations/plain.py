"""Adapter from RunRequest to the existing plain loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api._orchestrations._common import built_in_description, resolved_run_id

if TYPE_CHECKING:
    from vibesys.api._orchestrations.contracts import RunDescription
    from vibesys.api.contracts import RunRequest
    from vibesys.orchestration import ResumeProjection
    from vibesys.run.integration import LocalRunIntegration
    from vs_project.api import OrchestrationRunManifest


class PlainOrchestration:
    """Preserve the existing plain loop call contract."""

    def history_namespaces(self) -> tuple[str, ...]:
        """Expose this policy's portable state to run history queries."""
        return ("plain",)

    def describe(self, request: RunRequest) -> RunDescription:
        return built_in_description(request, round_budget=True)

    def resume_projection(self, manifest: OrchestrationRunManifest) -> ResumeProjection:
        """Project the plain-owned descriptor without constructing v3 settings."""
        from vibesys.loops.plain.orchestration import resume_projection  # noqa: PLC0415

        return resume_projection(manifest)

    def execute(self, request: RunRequest, integration: LocalRunIntegration) -> bool:
        from vibesys.loops.plain.loop import run_plain_loop  # noqa: PLC0415  # tracked: #288

        bundle = request.input_bundle
        resuming = request.resume is not None
        return run_plain_loop(
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
            max_rounds=request.max_rounds if request.max_rounds is not None else 5,
            max_attempts_per_issue=request.max_attempts_per_issue,
            max_issues_per_perf_eval=request.max_issues_per_perf_eval,
            existing=resuming,
            debug=request.debug,
            profiler_kind=request.profiler_kind,
            skills_dirs=request.skills_dirs,
            run_environment=request.run_environment,
            agent_backend=request.agent_backend,
            cli_provider=request.cli_provider,
            backend=request.backend,
            domain=bundle.domain,
            remote_repo=request.remote_repo,
            repo_visibility=request.repo_visibility,
            integration=integration,
        )
