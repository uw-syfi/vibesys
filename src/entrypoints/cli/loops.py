"""Resolve CLI inputs into one descriptor-backed orchestration request."""

from __future__ import annotations

import math
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.cli.config import _prepare_experiment_repository, load_config_and_skills
from entrypoints.cli.environment import (
    _validate_run_environment_profiler,
    run_environment_spec_from_args,
)
from entrypoints.cli.errors import _configuration_error
from entrypoints.cli.inputs import _standalone_input_dests_set, _validate_target_inputs
from entrypoints.cli.remote import _clone_project, _is_remote_project
from headless import run as headless_run
from vibesys.api import (
    DomainName,
    MetricSpace,
    Objective,
    OrchestrationDescriptor,
    ResumeRef,
    RunRequest,
    RunResult,
    boot_trace,
)
from vibesys.api.evolve import resolve_openevolve_options
from vibesys.api.request import (
    InputBundle,
    validate_descriptor,
    with_operator_constraints,
)
from vs_project.api import Project

if TYPE_CHECKING:
    import argparse


def _normalize_runs_dir(args: argparse.Namespace) -> None:
    raw = getattr(args, "runs_dir", None)
    if raw is None:
        if _standalone_input_dests_set(args):
            _configuration_error(
                "Standalone --input-* flags require --runs-dir because they synthesize "
                "a new input bundle. Use a self-contained project directory for a direct run.",
                code="missing_runs_dir",
                stage="argument_parsing",
            )
        args.runs_dir = None
        return
    runs_dir = raw.expanduser().resolve()
    prefix = Path(sys.prefix).resolve()
    if runs_dir.is_relative_to(prefix):
        _configuration_error(
            f"--runs-dir cannot be inside the Python installation prefix {prefix}: {runs_dir}",
            code="invalid_runs_dir",
            stage="argument_parsing",
        )
    if runs_dir.exists() and not runs_dir.is_dir():
        _configuration_error(
            f"--runs-dir is not a directory: {runs_dir}",
            code="invalid_runs_dir",
            stage="argument_parsing",
        )
    args.runs_dir = runs_dir


def _resolve_project_root(project_arg: str, runs_dir: Path) -> Path:
    """Resolve one canonical project from a local collection or remote URL."""
    if project_arg != "latest":
        explicit = Path(project_arg).expanduser()
        if explicit.is_dir():
            project_root = explicit.resolve()
            if Project.is_state_initialized(project_root):
                return project_root
            _configuration_error(
                f"Directory is not a VibeSys project: {project_root}",
                code="resume_not_found",
                stage="resume_resolution",
            )

        collection_path = runs_dir / project_arg
        if collection_path.is_dir():
            project_root = collection_path.resolve()
            if Project.is_state_initialized(project_root):
                return project_root
            _configuration_error(
                f"Directory is not a VibeSys project: {project_root}",
                code="resume_not_found",
                stage="resume_resolution",
            )

        if _is_remote_project(project_arg):
            return _clone_project(project_arg, runs_dir)
        _configuration_error(
            f"Project directory does not exist: {collection_path}",
            code="resume_not_found",
            stage="resume_resolution",
        )

    if not runs_dir.is_dir():
        _configuration_error(
            f"Runs directory does not exist: {runs_dir}",
            code="resume_not_found",
            stage="resume_resolution",
        )
    projects = Project.find_state_projects(runs_dir)
    if not projects:
        _configuration_error(
            f"No VibeSys projects found in {runs_dir}.",
            code="resume_not_found",
            stage="resume_resolution",
        )
    return projects[-1].resolve()


def _load_metric_space_toml(input_path: Path) -> MetricSpace:
    """Read the run's metric space from the task's ``objectives.toml``.

    The axes and the measurement tolerance are one fact about the workload and
    are read together, once. ``run_agent_loop`` persists the returned space
    into the run state, and every consumer that has to order two readings takes
    it from there.

    Exact comparison remains the default. Inputs with measured benchmark
    variation can opt into a relative margin under ``[pareto]`` without
    imposing one domain's noise level on every optimization workload.
    """
    path = input_path / "objectives.toml"
    if not path.exists():
        return MetricSpace()
    data = tomllib.loads(path.read_text())
    objectives = []
    for entry in data.get("objective") or []:
        name = entry.get("name")
        direction = entry.get("direction")
        if not name or direction not in ("max", "min"):
            _exception_message_2 = f"Malformed entry in {path}: {entry!r}. Each [[objective]] must set name and direction (max|min)."
            raise ValueError(_exception_message_2)
        objectives.append(Objective(name=name, direction=direction))
    raw_value = (data.get("pareto") or {}).get("relative_noise", 0.0)
    if isinstance(raw_value, bool):
        message = f"Malformed pareto.relative_noise in {path}: {raw_value!r}"
        raise ValueError(message)  # noqa: TRY004  # lint-waiver: LW-010200 [TRY004]; malformed objective files use the CLI's established ValueError diagnostic contract.
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        _exception_message_3 = f"Malformed pareto.relative_noise in {path}: {raw_value!r}"
        raise ValueError(_exception_message_3) from exc
    if not math.isfinite(value) or not 0 <= value < 1:
        _exception_message = f"Malformed pareto.relative_noise in {path}: expected a finite value in [0, 1), got {raw_value!r}"
        raise ValueError(_exception_message)
    return MetricSpace(objectives=tuple(objectives), relative_noise=value)


def _resolve_metric_space(args: argparse.Namespace) -> MetricSpace:
    """Build the run's metric space, letting ``--objective`` override its axes.

    The tolerance always comes from the task file: it describes the workload's
    measurement variation, which a command-line axis list does not change.
    """
    space = _load_metric_space_toml(args.input_bundle.task_root)
    if getattr(args, "objective", None):
        return MetricSpace(
            objectives=tuple(args.objective),
            relative_noise=space.relative_noise,
        )
    return space


def _validate_openevolve_options(args: argparse.Namespace) -> None:
    """Validate options specific to the OpenEvolve search policy."""
    if args.search_policy == "vibesys" and any(
        value is not None
        for value in (
            args.openevolve_population_size,
            args.openevolve_archive_size,
            args.openevolve_num_islands,
            args.openevolve_migration_interval,
            args.openevolve_migration_rate,
        )
    ):
        _configuration_error("--openevolve-* settings cannot be used with --search-policy vibesys.")
    if args.openevolve_population_size is not None and args.openevolve_population_size < 1:
        _configuration_error("--openevolve-population-size must be >= 1.")
    if args.openevolve_archive_size is not None and args.openevolve_archive_size < 1:
        _configuration_error("--openevolve-archive-size must be >= 1.")
    if args.openevolve_num_islands is not None and args.openevolve_num_islands < 1:
        _configuration_error("--openevolve-num-islands must be >= 1.")
    if args.openevolve_migration_interval is not None and args.openevolve_migration_interval < 1:
        _configuration_error("--openevolve-migration-interval must be >= 1.")
    if args.openevolve_migration_rate is not None and not (
        0.0 <= args.openevolve_migration_rate <= 1.0
    ):
        _configuration_error("--openevolve-migration-rate must be in [0, 1].")


def _validate_evolve(args: argparse.Namespace) -> None:
    _validate_target_inputs(args)
    _validate_run_environment_profiler(args)
    if args.children_per_generation < 1:
        _configuration_error("--children-per-generation must be >= 1.")
    if args.max_generations < 1:
        _configuration_error("--max-generations must be >= 1.")
    if args.selection_temperature <= 0:
        _configuration_error("--selection-temperature must be > 0.")
    _validate_openevolve_options(args)
    if not (0.0 <= args.frontier_bias <= 1.0):
        _configuration_error("--frontier-bias must be in [0, 1].")
    if args.bootstrap_max_attempts < 1:
        _configuration_error("--bootstrap-max-attempts must be >= 1.")
    if args.max_parallelism < 1:
        _configuration_error("--max-parallelism must be >= 1.")


def _resolve_openevolve_options(
    args: argparse.Namespace,
) -> tuple[str | None, dict[str, int | float | None]]:
    return resolve_openevolve_options(
        args.search_policy,
        {
            "openevolve_population_size": args.openevolve_population_size,
            "openevolve_archive_size": args.openevolve_archive_size,
            "openevolve_num_islands": args.openevolve_num_islands,
            "openevolve_migration_interval": args.openevolve_migration_interval,
            "openevolve_migration_rate": args.openevolve_migration_rate,
        },
    )


def _validate_plain(args: argparse.Namespace) -> None:
    _validate_target_inputs(args)
    _validate_run_environment_profiler(args)


def _agent_policy_descriptor(
    args: argparse.Namespace, bundle: InputBundle
) -> OrchestrationDescriptor:
    orchestration_id = (
        args.inner_loop if args.outer_loop == "agent" else f"profile-guided-{args.inner_loop}"
    )
    metrics = _resolve_metric_space(args)
    benchmark = bundle.benchmark_result
    if benchmark is not None and metrics.axis(benchmark.metric) is None:
        metrics = metrics.model_copy(
            update={
                "objectives": (*metrics.objectives, Objective(benchmark.metric, "max")),
            }
        )
    options = {
        "interface": args.interface,
        "modality": args.modality
        or ("text_generation" if bundle.domain is DomainName.LLM_SERVING else None),
        "max_rounds": args.max_rounds,
        "max_retries_per_round": args.max_retries_per_round,
        "judge_every": args.judge_every,
        "official_eval_every": args.official_eval_every,
        "memory_layout": args.memory_layout,
        "operator_constraints": [item.strip() for item in args.constraint if item.strip()],
        "metric_space": metrics.model_dump(mode="json"),
        "profile_guided": bundle.manifest.profile_guided.model_dump(mode="json")
        if args.outer_loop == "profile-guided" and bundle.manifest.profile_guided is not None
        else None,
    }
    return OrchestrationDescriptor(id=orchestration_id, config_version=1, options=options)


def _plain_policy_descriptor(args: argparse.Namespace) -> OrchestrationDescriptor:
    options = {
        "modality": None,
        "max_rounds": args.max_rounds,
        "max_attempts_per_issue": args.max_attempts_per_issue,
        "max_issues_per_perf_eval": args.max_issues_per_perf_eval,
    }
    return OrchestrationDescriptor(id="plain", config_version=1, options=options)


def _evolve_policy_descriptor(
    args: argparse.Namespace, bundle: InputBundle
) -> OrchestrationDescriptor:
    search_policy, openevolve = _resolve_openevolve_options(args)
    space = _resolve_metric_space(args)
    modality = args.modality
    if modality is None and bundle.domain is DomainName.LLM_SERVING:
        modality = "text_generation"
    options = {
        "modality": modality,
        "max_generations": args.max_generations,
        "children_per_generation": args.children_per_generation,
        "k_top_inspirations": args.k_top_inspirations,
        "k_random_inspirations": args.k_random_inspirations,
        "selection_temperature": args.selection_temperature,
        "seed": args.seed,
        "search_policy": search_policy,
        **openevolve,
        "frontier_bias": args.frontier_bias,
        "bootstrap_max_attempts": args.bootstrap_max_attempts,
        "keep_deployments": args.keep_deployments,
        "max_parallelism": args.max_parallelism,
        "metric_space": space.model_dump(mode="json"),
    }
    return OrchestrationDescriptor(id="evolve", config_version=1, options=options)


def _build_run_request(args: argparse.Namespace) -> RunRequest:
    """Build the one request format consumed by every registered policy."""
    with boot_trace.span("run_preamble"):
        bundle: InputBundle = args.input_bundle
        config, skills, backend = load_config_and_skills(args, domain=bundle.domain)
        if args.outer_loop in {"agent", "profile-guided"}:
            descriptor = _agent_policy_descriptor(args, bundle)
            objective = with_operator_constraints(bundle.objective, args.constraint)
        elif args.outer_loop == "plain":
            descriptor = _plain_policy_descriptor(args)
            objective = bundle.objective
        else:
            descriptor = _evolve_policy_descriptor(args, bundle)
            objective = bundle.objective
        validate_descriptor(descriptor)
        _prepare_experiment_repository(args, config)
        run_environment = run_environment_spec_from_args(args, build_task_docker_image=True)
        if args.resume is not None:
            sys.stdout.write(f"Resuming VibeSys run {args.resume} in {bundle.root}/\n")
        return RunRequest(
            project_root=bundle.root,
            orchestration=descriptor,
            config=config,
            input_bundle=bundle,
            objective=objective,
            resume=ResumeRef(run_id=args.resume) if args.resume is not None else None,
            exp_name=args.exp_name,
            runs_dir=args.runs_dir,
            debug=args.debug,
            profiler_kind=args.profiler,
            skills_dirs=skills,
            run_environment=run_environment,
            agent_backend="stub" if getattr(args, "stub_agent", False) else args.agent_backend,
            cli_provider=args.cli_provider,
            backend=backend,
            remote_repo=args.repo,
            repo_visibility=args.repo_visibility,
        )


def _run_request(args: argparse.Namespace) -> None:
    request = _build_run_request(args)
    result = _execute_run_request(request)
    if result.succeeded:
        sys.stdout.write(f"\n{request.orchestration_id} run completed.\n")
    else:
        sys.stdout.write(f"\n{request.orchestration_id} run stopped early.\n")
        sys.exit(1)


def _execute_run_request(request: RunRequest) -> RunResult:
    """Run *request* to completion via `headless.run`."""
    return headless_run(request)
