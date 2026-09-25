"""Restore one v4 descriptor-backed run before building its request."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from entrypoints.cli.args import _parse_cli_objective
from entrypoints.cli.constants import _RUN_ENVIRONMENT_OPTION_CLI_FIELDS
from entrypoints.cli.errors import _configuration_error, _project_resume_mismatch
from entrypoints.cli.loops import _resolve_project_root
from vibesys.api import ComputeBackend, Objective, ProfilerKind
from vibesys.api.request import coerce_profiler_kind, validate_descriptor
from vs_project.api import (
    GitTracker,
    NullGitTrackerEvents,
    OrchestrationRunManifest,
    Project,
    ProjectStateError,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Mapping

    from vs_project.api import RunEnvironmentRecord


_POLICY_CLI_SELECTION = {
    "multi-agent": ("agent", "multi-agent"),
    "single-agent": ("agent", "single-agent"),
    "profile-guided-multi-agent": ("profile-guided", "multi-agent"),
    "profile-guided-single-agent": ("profile-guided", "single-agent"),
    "plain": ("plain", None),
    "evolve": ("evolve", None),
}
_OPTION_TO_CLI = {
    "compute_backend": "backend",
    "operator_constraints": "constraint",
}
_CONFIG_ONLY_OPTIONS = frozenset(
    {
        "model",
        "agent_driver",
        "cli_timeout",
        "default_reasoning_effort",
        "outer_model",
        "outer_reasoning_effort",
        "inner_model",
        "inner_reasoning_effort",
        "metric_space",
        "profile_guided",
    }
)


def _restore_resume_budget(
    args: argparse.Namespace,
    *,
    destination: str,
    recorded_value: int,
    explicit: frozenset[str],
) -> None:
    if destination not in explicit:
        setattr(args, destination, recorded_value)
        return
    requested = getattr(args, destination)
    if requested < recorded_value:
        option = "--" + destination.replace("_", "-")
        _configuration_error(
            f"{option} is the run's total limit and cannot decrease when resuming "
            f"(recorded {recorded_value}, requested {requested})",
            code="project_resume_configuration_mismatch",
            stage="resume_resolution",
        )


def _restore_run_environment_selection(
    args: argparse.Namespace,
    record: RunEnvironmentRecord,
    explicit: frozenset[str],
) -> bool | None:
    """Restore the recorded environment selection or report a CLI mismatch."""
    if not hasattr(args, "docker") or not hasattr(args, "modal"):
        return None
    skypilot = bool(getattr(args, "skypilot", False))
    if "run_environment" in explicit:
        requested = getattr(args, "run_environment", None)
    elif skypilot:
        requested = "skypilot"
    elif args.modal:
        requested = "modal"
    elif args.docker:
        requested = "docker"
    else:
        requested = "local"
    explicit_environment = {"docker", "modal", "skypilot", "run_environment"} & explicit
    if explicit_environment:
        return requested != record.name
    args.docker = record.name == "docker"
    args.modal = record.name == "modal"
    if hasattr(args, "skypilot"):
        args.skypilot = record.name == "skypilot"
    if hasattr(args, "run_environment"):
        args.run_environment = record.name
    return False


def _restore_resume_run_environment(
    args: argparse.Namespace,
    record: RunEnvironmentRecord,
    explicit: frozenset[str],
) -> list[str]:
    """Restore the recorded runtime environment and reject contradictions."""
    changed: list[str] = []
    mismatch = _restore_run_environment_selection(args, record, explicit)
    if mismatch is None:
        return changed
    if mismatch:
        changed.append("run_environment")

    for destination, field in _RUN_ENVIRONMENT_OPTION_CLI_FIELDS.items():
        if not hasattr(args, destination):
            continue
        expected = getattr(record, field)
        if destination in explicit:
            if getattr(args, destination) != expected:
                changed.append(f"run_environment.{field}")
        elif expected is not None:
            setattr(args, destination, expected)
    return changed


def _normalized_resume_cli_value(destination: str, value: object) -> object:
    if destination == "objective":
        return tuple(f"{item.name}:{item.direction}" for item in cast("list[Objective]", value))
    if destination == "constraint":
        return tuple(item.strip() for item in cast("list[str]", value) if item.strip())
    if destination == "backend" and value is not None:
        if not isinstance(value, ComputeBackend):
            message = f"argparse backend value must be ComputeBackend, got {type(value).__name__}"
            raise TypeError(message)
        return value.value
    if destination == "profiler":
        if not isinstance(value, ProfilerKind):
            message = f"argparse profiler value must be ProfilerKind, got {type(value).__name__}"
            raise TypeError(message)
        return value.value
    return value


def _set_resume_cli_value(args: argparse.Namespace, destination: str, value: object) -> None:
    if destination == "agent_backend":
        is_stub = value == "stub"
        args.stub_agent = is_stub
        value = None if is_stub else value
    elif destination == "objective":
        value = [_parse_cli_objective(item) for item in cast("list[str]", value)]
    elif destination == "constraint":
        value = list(cast("list[str]", value))
    elif destination == "backend":
        try:
            value = ComputeBackend(value)
        except ValueError:
            _configuration_error(
                f"Run metadata records unknown compute backend {value!r}",
                code="project_resume_configuration_invalid",
                stage="resume_resolution",
            )
    elif destination == "profiler":
        try:
            value = coerce_profiler_kind(cast("str", value) or ProfilerKind.AUTO.value)
        except ValueError:
            _configuration_error(
                f"Run metadata records unknown profiler {value!r}",
                code="project_resume_configuration_invalid",
                stage="resume_resolution",
            )
    setattr(args, destination, value)


def _restore_cli_fields(
    args: argparse.Namespace,
    options: Mapping[str, object],
    *,
    budget: str,
    explicit: frozenset[str],
) -> list[str]:
    """Apply descriptor options to present CLI fields and report conflicts."""
    changed: list[str] = []
    for field, expected in options.items():
        if field in _CONFIG_ONLY_OPTIONS or field == budget:
            continue
        destination = _OPTION_TO_CLI.get(field, field)
        if not hasattr(args, destination):
            continue
        is_explicit = destination in explicit or (
            destination == "agent_backend" and "stub_agent" in explicit
        )
        if is_explicit:
            requested = (
                "stub"
                if destination == "agent_backend" and getattr(args, "stub_agent", False)
                else _normalized_resume_cli_value(destination, getattr(args, destination))
            )
            if requested != (tuple(expected) if isinstance(expected, list) else expected):
                changed.append(field)
        else:
            _set_resume_cli_value(args, destination, expected)
    return changed


def _restore_evolve_objectives(
    args: argparse.Namespace, options: Mapping[str, object], explicit: frozenset[str]
) -> list[str]:
    """Restore the recorded objective axes while preserving task tolerance."""
    metric_space = cast("dict[str, object]", options["metric_space"])
    axes = cast("list[dict[str, str]]", metric_space["objectives"])
    recorded = [f"{axis['name']}:{axis['direction']}" for axis in axes]
    if "objective" in explicit:
        requested = _normalized_resume_cli_value("objective", args.objective)
        return ["metric_space.objectives"] if requested != tuple(recorded) else []
    args.objective = [_parse_cli_objective(spec) for spec in recorded]
    return []


def _restore_descriptor_options(
    args: argparse.Namespace, manifest: OrchestrationRunManifest, *, loop_kind: str
) -> None:
    """Restore omitted flags from the single active descriptor format."""
    descriptor = manifest.orchestration
    try:
        recorded_loop, inner_loop = _POLICY_CLI_SELECTION[descriptor.id]
    except KeyError:
        _configuration_error(
            f"The CLI cannot resume orchestration {descriptor.id!r}",
            code="project_resume_configuration_invalid",
            stage="resume_resolution",
        )
    if recorded_loop != loop_kind:
        _configuration_error(
            f"Run uses --outer-loop {recorded_loop}, not {loop_kind}",
            code="project_resume_configuration_mismatch",
            stage="resume_resolution",
        )
    explicit = getattr(args, "explicit_cli_dests", frozenset())
    changed = _restore_resume_run_environment(args, manifest.run_environment, explicit)
    if inner_loop is not None:
        if "inner_loop" in explicit and args.inner_loop != inner_loop:
            changed.append("inner_loop")
        else:
            args.inner_loop = inner_loop
    options = descriptor.options
    budget = "max_generations" if recorded_loop == "evolve" else "max_rounds"
    _restore_resume_budget(
        args,
        destination=budget,
        recorded_value=cast("int", options[budget]),
        explicit=explicit,
    )
    changed.extend(_restore_cli_fields(args, options, budget=budget, explicit=explicit))
    execution_cli = {
        "agent_backend": manifest.execution.agent_backend,
        "cli_provider": manifest.execution.cli_provider,
        "compute_backend": manifest.execution.compute_backend,
        "profiler": manifest.execution.requested_profiler,
    }
    changed.extend(_restore_cli_fields(args, execution_cli, budget=budget, explicit=explicit))
    if recorded_loop == "evolve":
        changed.extend(_restore_evolve_objectives(args, options, explicit))
    if changed:
        _project_resume_mismatch(changed)
    args.project_run_configuration = SimpleNamespace(**manifest.execution.model_dump())


def _switch_project_resume_branch(project_root: Path, run_id: str) -> None:
    """Select the run branch before callers read committed project files."""
    if not (project_root / ".git").exists():
        return
    tracker = GitTracker(project_root, events=NullGitTrackerEvents(), run_id=run_id)
    try:
        tracker.init(existing=True)
    except (subprocess.SubprocessError, ValueError) as exc:
        _configuration_error(
            f"Cannot resume project run {run_id!r}: {exc}",
            code="resume_not_found",
            stage="resume_resolution",
        )


def _resolve_resume_args(args: argparse.Namespace, *, loop_kind: str) -> None:
    if args.resume is None:
        return
    if args.repo is not None:
        _configuration_error(
            "--repo creates a remote for a new experiment and cannot be combined with --resume",
            code="invalid_arguments",
            stage="argument_parsing",
        )

    direct = args.runs_dir is None
    project_root = (
        (args.input or Path.cwd()).expanduser().resolve()
        if direct
        else _resolve_project_root(args.resume, args.runs_dir)
    )
    try:
        store = Project.open(project_root).state
        if direct and args.resume != "latest":
            run_id = args.resume
        else:
            run_id = store.current_run_id()
            if run_id is None:
                run_id = store.resolve_run().run_id
        _switch_project_resume_branch(project_root, run_id)
        manifest = store.load_run(run_id)
    except ProjectStateError as exc:
        message = str(exc)
        _configuration_error(
            f"Cannot resume project run: {message}",
            code=(
                "unsupported_run_schema"
                if "unsupported run schema version" in message
                else "resume_not_found"
            ),
            stage="resume_resolution",
        )
    if not isinstance(manifest, OrchestrationRunManifest):
        _configuration_error(
            f"Run {run_id!r} uses an unsupported run schema; only v4 runs can resume",
            code="unsupported_run_schema",
            stage="resume_resolution",
        )
    validate_descriptor(manifest.orchestration)
    args.resume = run_id
    args.exp_name = run_id
    args.input = project_root
    if manifest.task_name is not None:
        if args.task is not None and args.task != manifest.task_name:
            _configuration_error(
                f"Run uses task {manifest.task_name!r}, not {args.task!r}",
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
            )
        args.task = manifest.task_name
    _restore_descriptor_options(args, manifest, loop_kind=loop_kind)
