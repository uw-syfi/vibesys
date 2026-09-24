"""Per-outer-loop request builders, runners, and run-environment resolution.

Also hosts the ``migrate-run-environment`` command's implementation.
"""

from __future__ import annotations

import math
import shlex
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.cli.args import _build_migrate_run_environment_parser
from entrypoints.cli.config import _prepare_experiment_repository, load_config_and_skills
from entrypoints.cli.constants import _MIGRATE_RUN_ENVIRONMENT_COMMAND
from entrypoints.cli.environment import (
    _validate_run_environment_profiler,
    run_environment_spec_from_args,
)
from entrypoints.cli.errors import _configuration_error
from entrypoints.cli.inputs import _standalone_input_dests_set, _validate_target_inputs
from entrypoints.cli.remote import _clone_project, _is_remote_project
from headless import run as headless_run
from vibesys.api import (
    LoopKind,
    MetricSpace,
    Objective,
    ResumeRef,
    RunRequest,
    RunResult,
    boot_trace,
)
from vibesys.api.request import (
    InputBundle,
    OpenEvolveSearchConfig,
    make_run_environment_spec,
    run_environment_record,
    with_operator_constraints,
)
from vs_project.api import Project, ProjectLayoutError, ProjectStateError

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


def _migrate_run_environment_command(project_root: Path, run_id: str) -> str:
    """Render the operator command that migrates one run's recorded metadata."""
    return shlex.join(
        [
            "vibesys",
            _MIGRATE_RUN_ENVIRONMENT_COMMAND,
            "--project",
            str(project_root),
            "--run",
            run_id,
            "--run-environment",
            "local|docker|modal",
        ]
    )


def _run_migrate_run_environment(argv: list[str]) -> None:
    """Migrate one version 1 or 2 run to the current execution schema."""
    args = _build_migrate_run_environment_parser().parse_args(argv)
    project_root = (args.project or Path.cwd()).expanduser().resolve()
    # Build the record through the same producer a fresh run uses so a migrated
    # recording matches what the CLI would have written for those flags.
    spec = make_run_environment_spec(
        use_docker=args.run_environment == "docker",
        use_modal=args.run_environment == "modal",
        docker_image=args.docker_image,
        modal_gpu=args.modal_gpu,
        modal_model_volume=args.modal_model_volume,
        modal_app=args.modal_app,
    )
    try:
        store = Project.open(project_root).state
        run_id = args.run or store.current_run_id()
        if run_id is None:
            _configuration_error(
                f"No current run in {project_root}; pass --run RUN_ID",
                code="migration_failed",
                stage="run_migration",
                exit_code=1,
            )
        manifest = store.migrate_run_environment(run_id, run_environment_record(spec))
    except (ProjectLayoutError, ProjectStateError, ValueError) as exc:
        _configuration_error(
            f"Migration failed for VibeSys run in {project_root}: {exc}",
            code="migration_failed",
            stage="run_migration",
            exit_code=1,
        )

    sys.stdout.write(
        f"Migrated run {manifest.run_id} to run schema version "
        f"{manifest.schema_version}: run environment "
        f"{manifest.configuration.run_environment.name}\n"
    )
    sys.stdout.write(f"  metadata: {project_root}\n")
    sys.stdout.write("  commit the updated run metadata to keep the run branch clean.\n")


def _build_agent_request(args: argparse.Namespace) -> RunRequest:
    """Run the agent-loop preamble and build its ``RunRequest``.

    Split out of ``_run_agent`` so the server can build this request and own
    the resulting ``create_session`` call itself, instead of going through
    ``dispatch``. The enclosing span is this preamble's total; ``context.py``
    times assembly from there.
    """
    with boot_trace.span("agent_preamble"):
        bundle: InputBundle = args.input_bundle
        with boot_trace.span("load_config_and_skills"):
            config, skills, backend = load_config_and_skills(args, domain=bundle.domain)
        with boot_trace.span("prepare_experiment_repository"):
            _prepare_experiment_repository(args, config)
        with boot_trace.span("load_objective"):
            objective = with_operator_constraints(bundle.objective, args.constraint)

        if args.resume is not None:
            sys.stdout.write(f"Resuming VibeSys run {args.resume} in {bundle.root}/\n")

        with boot_trace.span("load_objectives_toml"):
            metrics = _load_metric_space_toml(bundle.task_root)

        with boot_trace.span("run_environment_spec"):
            run_environment = run_environment_spec_from_args(
                args,
                build_task_docker_image=True,
            )

        return RunRequest(
            project_root=bundle.root,
            loop=LoopKind(getattr(args, "outer_loop", "agent")),
            config=config,
            input_bundle=bundle,
            objective=objective,
            resume=ResumeRef(run_id=args.resume) if args.resume is not None else None,
            exp_name=args.exp_name,
            runs_dir=args.runs_dir,
            metrics=metrics,
            operator_constraints=tuple(
                constraint.strip() for constraint in args.constraint if constraint.strip()
            ),
            debug=args.debug,
            profiler_kind=args.profiler,
            skills_dirs=skills,
            run_environment=run_environment,
            agent_backend="stub" if args.stub_agent else args.agent_backend,
            cli_provider=args.cli_provider,
            backend=backend,
            modality=args.modality,
            interface=args.interface,
            inner_loop=args.inner_loop,
            remote_repo=args.repo,
            repo_visibility=args.repo_visibility,
            max_rounds=args.max_rounds,
            max_retries_per_round=args.max_retries_per_round,
            judge_every=args.judge_every,
            official_eval_every=args.official_eval_every,
            memory_layout=args.memory_layout,
        )


def _run_agent(args: argparse.Namespace) -> None:
    request = _build_agent_request(args)
    result = _execute_run_request(request)

    if result.succeeded:
        sys.stdout.write(f"\nAgent loop completed {args.max_rounds} rounds.\n")
    else:
        sys.stdout.write("\nAgent loop stopped early (exception or KeyboardInterrupt).\n")
        sys.exit(1)


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
    if args.objective:
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
) -> tuple[str | None, OpenEvolveSearchConfig | None]:
    openevolve_defaults = OpenEvolveSearchConfig()
    openevolve_values = (
        args.openevolve_population_size,
        args.openevolve_archive_size,
        args.openevolve_num_islands,
        args.openevolve_migration_interval,
        args.openevolve_migration_rate,
    )
    openevolve_config = (
        OpenEvolveSearchConfig(
            population_size=args.openevolve_population_size or openevolve_defaults.population_size,
            archive_size=args.openevolve_archive_size or openevolve_defaults.archive_size,
            num_islands=args.openevolve_num_islands or openevolve_defaults.num_islands,
            migration_interval=args.openevolve_migration_interval
            or openevolve_defaults.migration_interval,
            migration_rate=(
                args.openevolve_migration_rate
                if args.openevolve_migration_rate is not None
                else openevolve_defaults.migration_rate
            ),
        )
        if any(value is not None for value in openevolve_values)
        else None
    )
    search_policy = args.search_policy or ("openevolve" if openevolve_config is not None else None)
    return search_policy, openevolve_config


def _build_evolve_request(args: argparse.Namespace) -> RunRequest:
    """Run the evolve-loop preamble and build its ``RunRequest``.

    Split out of ``_run_evolve`` so the server can build this request and own
    the resulting ``create_session`` call itself, instead of going through
    ``dispatch``.
    """
    bundle: InputBundle = args.input_bundle
    config, skills, backend = load_config_and_skills(args, domain=bundle.domain)
    _prepare_experiment_repository(args, config)

    space = _resolve_metric_space(args)

    if args.resume is not None:
        sys.stdout.write(f"Resuming evolve run {args.resume} in {bundle.root}/\n")
    if space.objectives:
        spec = ", ".join(f"{o.name}({o.direction})" for o in space.objectives)
        sys.stdout.write(
            f"Pareto mode active: [{spec}]; frontier_bias={args.frontier_bias}; "
            f"tolerance={space.relative_noise:.0%}\n"
        )

    search_policy, openevolve_config = _resolve_openevolve_options(args)

    return RunRequest(
        project_root=bundle.root,
        loop=LoopKind.EVOLVE,
        config=config,
        input_bundle=bundle,
        objective=bundle.objective,
        resume=ResumeRef(run_id=args.resume) if args.resume is not None else None,
        exp_name=args.exp_name,
        runs_dir=args.runs_dir,
        space=space,
        debug=args.debug,
        profiler_kind=args.profiler,
        skills_dirs=skills,
        run_environment=run_environment_spec_from_args(args, build_task_docker_image=True),
        agent_backend=args.agent_backend,
        cli_provider=args.cli_provider,
        backend=backend,
        modality=args.modality,
        remote_repo=args.repo,
        repo_visibility=args.repo_visibility,
        max_generations=args.max_generations,
        children_per_generation=args.children_per_generation,
        k_top_inspirations=args.k_top_inspirations,
        k_random_inspirations=args.k_random_inspirations,
        selection_temperature=args.selection_temperature,
        seed=args.seed,
        frontier_bias=args.frontier_bias,
        bootstrap_max_attempts=args.bootstrap_max_attempts,
        keep_deployments=args.keep_deployments,
        max_parallelism=args.max_parallelism,
        search_policy=search_policy,
        openevolve_config=openevolve_config,
    )


def _run_evolve(args: argparse.Namespace) -> None:
    request = _build_evolve_request(args)
    result = _execute_run_request(request)

    if result.succeeded:
        sys.stdout.write(
            f"\nEvolve loop completed {args.max_generations} generations "
            f"with {args.children_per_generation} candidates each.\n"
        )
    else:
        sys.stdout.write("\nEvolve loop stopped early (exception or KeyboardInterrupt).\n")
        sys.exit(1)


def _validate_plain(args: argparse.Namespace) -> None:
    _validate_target_inputs(args)
    _validate_run_environment_profiler(args)


def _build_plain_request(args: argparse.Namespace) -> RunRequest:
    """Run the plain-loop preamble and build its ``RunRequest``.

    Split out of ``_run_plain`` so the server can build this request and own
    the resulting ``create_session`` call itself, instead of going through
    ``dispatch``.
    """
    bundle: InputBundle = args.input_bundle
    config, skills, backend = load_config_and_skills(args, domain=bundle.domain)
    _prepare_experiment_repository(args, config)

    if args.resume is not None:
        sys.stdout.write(f"Resuming plain run {args.resume} in {bundle.root}/\n")

    return RunRequest(
        project_root=bundle.root,
        loop=LoopKind.PLAIN,
        config=config,
        input_bundle=bundle,
        resume=ResumeRef(run_id=args.resume) if args.resume is not None else None,
        exp_name=args.exp_name,
        runs_dir=args.runs_dir,
        debug=args.debug,
        profiler_kind=args.profiler,
        skills_dirs=skills,
        run_environment=run_environment_spec_from_args(args, build_task_docker_image=True),
        agent_backend=args.agent_backend,
        cli_provider=args.cli_provider,
        backend=backend,
        remote_repo=args.repo,
        repo_visibility=args.repo_visibility,
        max_rounds=args.max_rounds,
        max_attempts_per_issue=args.max_attempts_per_issue,
        max_issues_per_perf_eval=args.max_issues_per_perf_eval,
    )


def _run_plain(args: argparse.Namespace) -> None:
    request = _build_plain_request(args)
    result = _execute_run_request(request)

    if result.succeeded:
        sys.stdout.write("\nPlain loop completed: no remaining open issues.\n")
    else:
        sys.stdout.write(f"\nPlain loop did not complete after {args.max_rounds} rounds.\n")
        sys.exit(1)


def _execute_run_request(request: RunRequest) -> RunResult:
    """Run *request* to completion via `headless.run`."""
    return headless_run(request)
