"""Per-`LoopKind` dispatch from a `RunRequest` to the underlying loop function.

Private to `vibesys.api`. Each `_dispatch_*` helper translates `RunRequest`
(plus the facts already carried by `RunRequest.input_bundle`) into the flat
keyword arguments `run_agent_loop`/`run_evolve_loop`/`run_plain_loop`
(`vibesys.loops.{agent,evolve,plain}.loop`) expect, and returns their bare
`bool` success result unchanged. The loop functions are imported lazily,
inside each helper, matching the lazy-import-for-startup-cost pattern already
used at their sole other call site, `entrypoints/headless.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from vibesys.api.contracts import LoopKind
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError

if TYPE_CHECKING:
    from vibesys.api.contracts import RunRequest
    from vibesys.run.integration import LocalRunIntegration


def _required_objective(request: RunRequest) -> str:
    """Return `request.objective`, which agent/evolve requests must set.

    `RunRequest.objective` is `str | None` only because `_dispatch_plain`
    never reads it (a plain-loop request has no reason to set it). Every
    agent/evolve construction site sets it from `InputBundle.objective`
    (itself non-optional): `vibesys.api.entry.default_request` always passes
    `bundle.objective`, and `entrypoints/headless.py`'s
    `_build_agent_request`/`_build_evolve_request` always pass
    `bundle.objective` (optionally wrapped by `_with_operator_constraints`).
    Nothing in `RunRequest`'s own type ties `objective` to `loop`, so this
    turns that cross-field invariant into an explicit, checked contract
    instead of letting a future construction gap surface as a confusing
    `TypeError` inside the loop function.
    """
    if request.objective is None:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="missing_objective",
                stage="dispatch",
                message=f"RunRequest for outer loop {request.loop.value!r} must set objective",
            )
        )
    return request.objective


def _agent_outer_loop(loop: LoopKind) -> Literal["agent", "profile-guided"]:
    """Narrow `loop` to the two values `dispatch_loop` routes to `_dispatch_agent`.

    `dispatch_loop` calls `_dispatch_agent` only for `LoopKind.AGENT`/
    `LoopKind.PROFILE_GUIDED` (see its routing below); this makes that
    invariant explicit here instead of letting `loop.value`'s plain `str`
    widen silently past `run_agent_loop`'s `outer_loop` literal.
    """
    if loop is LoopKind.PROFILE_GUIDED:
        return "profile-guided"
    assert loop is LoopKind.AGENT, (  # noqa: S101  # dispatch_loop only routes these two here
        f"_dispatch_agent called with unsupported loop kind: {loop!r}"
    )
    return "agent"


def resolved_run_id(request: RunRequest) -> str:
    """Return the run id the loop should use: the resume target, or `exp_name`."""
    if request.resume is not None:
        return request.resume.run_id
    if request.exp_name is None:
        message = "RunRequest.exp_name must be set for a fresh (non-resume) run"
        raise ValueError(message)
    return request.exp_name


def dispatch_loop(request: RunRequest, integration: LocalRunIntegration) -> bool:
    """Call the loop function selected by `request.loop` and return its success bool."""
    if request.loop in (LoopKind.AGENT, LoopKind.PROFILE_GUIDED):
        return _dispatch_agent(request, integration)
    if request.loop is LoopKind.PLAIN:
        return _dispatch_plain(request, integration)
    return _dispatch_evolve(request, integration)


def _dispatch_agent(request: RunRequest, integration: LocalRunIntegration) -> bool:
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


def _dispatch_evolve(request: RunRequest, integration: LocalRunIntegration) -> bool:
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


def _dispatch_plain(request: RunRequest, integration: LocalRunIntegration) -> bool:
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
