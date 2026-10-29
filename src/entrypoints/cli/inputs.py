"""Task selection, standalone-input synthesis, and the ``validate`` command."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.cli.args import _build_validate_parser, _parse_command_flag
from entrypoints.cli.constants import _STANDALONE_INPUT_DESTS
from entrypoints.cli.environment import _validate_run_environment_profiler
from entrypoints.cli.errors import _cause_error, _configuration_error
from vibesys.api import (
    DomainName,
    ProfilerKind,
    TaskNeedsProvisioning,
    TaskNotFound,
    TaskUnusable,
)
from vibesys.api.request import (
    InputSynthesisError,
    SynthesizedInputSpec,
    generate_experiment_name,
    load_task,
    synthesize_input_bundle,
    validate_experiment_name,
)
from vs_project.api import Project, ProjectLayoutError, TaskNotFoundError
from vs_sim.api import SystemClock, utc_datetime

if TYPE_CHECKING:
    import argparse

    from vibesys.api.request import InputBundle


def _run_validate(argv: list[str]) -> None:
    """Validate one task contract, then report its resolved paths."""
    args = _build_validate_parser().parse_args(argv)
    input_path = (args.project or Path.cwd()).expanduser().resolve()

    try:
        bundle = load_task(input_path, args.task)
    except (FileNotFoundError, ProjectLayoutError, ValueError) as exc:
        _configuration_error(
            f"Validation failed for VibeSys project {input_path}: {exc}",
            code="validation_failed",
            stage="input_validation",
            exit_code=1,
        )

    summary = [
        "VibeSys validation passed: task is valid.",
        f"  project: {bundle.root}",
        f"  task: {bundle.task_name}",
    ]
    summary.extend(
        [
            f"  objective: {bundle.objective_path}",
            f"  accuracy command: {bundle.accuracy_command_display}",
            f"  benchmark command: {bundle.benchmark_command_display}",
        ]
    )
    summary.extend(
        f"  workspace source: {source.name} -> {source.dest} @ {source.commit}"
        for source in bundle.workspace_sources
    )
    if bundle.evaluator_path is not None:
        summary.append(f"  evaluator source: {bundle.evaluator_path}")
    if bundle.benchmark_result is not None:
        summary.append(f"  benchmark metric: {bundle.benchmark_result.metric}")
    if bundle.benchmark_result_protocol is not None:
        summary.append(f"  benchmark result protocol: {bundle.benchmark_result_protocol}")
    sys.stdout.write("\n".join(summary) + "\n")


def _standalone_input_dests_set(args: argparse.Namespace) -> list[str]:
    """Return the standalone-input flag dests that were provided (non-default)."""
    return [dest for dest in _STANDALONE_INPUT_DESTS if getattr(args, dest, None) is not None]


def _resolve_standalone_objective(args: argparse.Namespace) -> str:
    if args.input_objective is not None and args.input_objective_file is not None:
        _configuration_error(
            "Error: pass only one of --input-objective or --input-objective-file.",
            code="invalid_arguments",
            stage="argument_parsing",
        )
    if args.input_objective is not None:
        return args.input_objective
    if args.input_objective_file is None:
        _configuration_error(
            "Error: --input-objective or --input-objective-file is required.",
            code="missing_input",
            stage="input_loading",
        )
    objective_file = args.input_objective_file.expanduser()
    if not objective_file.is_file():
        _configuration_error(
            f"Error: --input-objective-file not found: {args.input_objective_file}",
            code="invalid_input",
            stage="input_loading",
        )
    return objective_file.read_text()


def _synthesize_standalone_input(args: argparse.Namespace) -> Path:
    """Materialize standalone-input flags into a bundle and return its path."""
    missing = [
        flag
        for flag, present in (
            (
                "--input-objective/--input-objective-file",
                args.input_objective is not None or args.input_objective_file is not None,
            ),
            ("--input-domain", args.input_domain is not None),
            ("--input-accuracy-command", args.input_accuracy_command is not None),
            ("--input-benchmark-command", args.input_benchmark_command is not None),
        )
        if not present
    ]
    if missing:
        _configuration_error(
            "Error: standalone input requires " + ", ".join(missing) + ".",
            code="missing_input",
            stage="input_loading",
        )

    objective = _resolve_standalone_objective(args)
    spec = SynthesizedInputSpec(
        objective=objective,
        domain=args.input_domain,
        accuracy_command=_parse_command_flag(
            args.input_accuracy_command, "--input-accuracy-command"
        ),
        benchmark_command=_parse_command_flag(
            args.input_benchmark_command, "--input-benchmark-command"
        ),
        accuracy_timeout_seconds=args.input_accuracy_timeout,
        benchmark_timeout_seconds=args.input_benchmark_timeout,
        benchmark_metric=args.input_benchmark_metric,
        benchmark_result_arg=args.input_benchmark_result_arg,
        reference_dir=args.input_reference,
        evaluator_dir=args.input_evaluator_dir,
        evaluator_source_dir=args.input_evaluator_source,
    )

    if args.exp_name is None:
        args.exp_name = generate_experiment_name(
            Path(str(args.input_domain)), now=utc_datetime(SystemClock())
        )
    destination = args.runs_dir / "_inputs" / args.exp_name
    try:
        return synthesize_input_bundle(spec, destination)
    except InputSynthesisError as exc:
        _configuration_error(str(exc), code="invalid_input", stage="input_loading")


def _validate_target_inputs(args: argparse.Namespace) -> None:
    """Resolve and validate the run's input bundle, applying its profiler default."""
    if args.resume is None and args.exp_name is not None:
        try:
            validate_experiment_name(args.exp_name)
        except ValueError as exc:
            _configuration_error(
                str(exc),
                code="invalid_exp_name",
                stage="argument_parsing",
            )

    input_arg = getattr(args, "input", None)
    task_name = getattr(args, "task", None)
    standalone = _standalone_input_dests_set(args)

    if input_arg is not None and standalone:
        _configuration_error(
            "Error: --input cannot be combined with standalone input flags "
            f"({', '.join('--' + dest.replace('_', '-') for dest in standalone)}).",
            code="invalid_arguments",
            stage="argument_parsing",
        )

    if input_arg is None:
        input_arg = _resolve_implicit_input(args, standalone)
        args.input = input_arg

    try:
        args.input_bundle = load_task(input_arg, task_name)
        args.task = args.input_bundle.task_name
    except TaskNotFoundError as exc:
        if exc.task_name is None:
            _cause_error(TaskUnusable(task=None, problem=str(exc)))
        _cause_error(TaskNotFound(task=exc.task_name, available=tuple(map(str, exc.available))))
    except (FileNotFoundError, ProjectLayoutError, ValueError) as exc:
        _cause_error(TaskUnusable(task=task_name, problem=str(exc)))

    _apply_bundle_profiler_default(args)

    if args.resume is None and args.runs_dir is None:
        _require_project_provisioned(args.input_bundle)


def _require_project_provisioned(bundle: InputBundle) -> None:
    """Refuse a task whose candidate source is provisioned into a copy of the project.

    A run without ``--runs-dir`` starts in the project as it is, so source declared under
    ``[workspace]`` has nowhere to be placed.
    """
    workspace = bundle.manifest.workspace
    if workspace is not None and workspace.sources:
        _cause_error(TaskNeedsProvisioning(task=bundle.task_name))


def _apply_bundle_profiler_default(args: argparse.Namespace) -> None:
    """Upgrade ``--profiler auto`` to OTel when the task provisions tracing.

    ``resolve_profiler_kind`` keeps bare microservice ``auto`` on ``none``
    because OTel needs instrumentation and a collector that only the task can
    provide. A benchmark command that emits a normalized telemetry report and a
    trace graph has provided exactly that, so honoring it here is what makes an
    instrumented task profile out of the box. An explicit ``--profiler`` always
    wins: this only ever replaces ``auto``.
    """
    bundle = getattr(args, "input_bundle", None)
    if bundle is None or getattr(args, "profiler", None) is not ProfilerKind.AUTO:
        return
    if bundle.domain is not DomainName.MICROSERVICES:
        return
    if not bundle.provisions_trace_telemetry:
        return
    args.profiler = ProfilerKind.OTEL


def _resolve_implicit_input(args: argparse.Namespace, standalone: list[str]) -> Path:
    """Resolve the current directory as the project, or synthesize one from flags."""
    if standalone:
        return _synthesize_standalone_input(args)
    current = Path.cwd().resolve()
    if Project.open(current).is_initialized():
        return current
    _configuration_error(
        f"Current directory is not a VibeSys project ({current}; missing .vibesys/tasks). "
        "Launch VibeSys from the project or pass --project PATH.",
        code="missing_input",
        stage="input_loading",
    )
    return None


def _validate_agent(args: argparse.Namespace) -> None:
    """Validate a resolved hypothesis-driven orchestration invocation."""
    _validate_target_inputs(args)
    _validate_run_environment_profiler(args)
    if args.max_retries_per_round < 1:
        _configuration_error("Error: --max-retries-per-round must be >= 1.")
    if args.judge_every < 1:
        _configuration_error("Error: --judge-every must be >= 1.")
    if getattr(args, "outer_loop", "agent") == "dynamic" and args.max_in_flight < 1:
        _configuration_error("Error: --max-in-flight must be >= 1.")
    if (
        getattr(args, "outer_loop", "agent") == "profile-guided"
        and args.input_bundle.manifest.profile_guided is None
    ):
        _configuration_error(
            "The profile-guided outer loop requires a [profile_guided] input section.",
            code="missing_profile_guided_input",
            stage="input_validation",
        )
