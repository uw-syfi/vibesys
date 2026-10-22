"""Restore one current-generation run before building its request."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from entrypoints.cli.args import _parse_cli_objective
from entrypoints.cli.config import _load_effective_config
from entrypoints.cli.constants import _RUN_ENVIRONMENT_OPTION_CLI_FIELDS
from entrypoints.cli.errors import _configuration_error, _project_resume_mismatch
from entrypoints.cli.loops import _resolve_project_root
from entrypoints.cli.remote import _is_remote_project
from launch import validate_descriptor
from vibesys.api import ComputeBackend, ProfilerKind
from vibesys.api.profilers import coerce_profiler_kind
from vibesys.api.request import ensure_resumable, migrate_recorded_run_environment
from vs_project.api import (
    Git,
    GitRunHistory,
    NullGitTrackerEvents,
    OrchestrationRunManifest,
    Project,
    ProjectStateError,
    open_git_repository,
)
from vs_sim.api import OsProcesses

if TYPE_CHECKING:
    import argparse
    from collections.abc import Mapping

    from vibesys.api.metrics import Objective
    from vs_project.api import RunEnvironmentRecord


_POLICY_CLI_SELECTION = {
    "dynamic": ("dynamic", None),
    "multi-agent": ("agent", "multi-agent"),
    "single-agent": ("agent", "single-agent"),
    "profile-guided-multi-agent": ("profile-guided", "multi-agent"),
    "profile-guided-single-agent": ("profile-guided", "single-agent"),
    "plain": ("plain", None),
    "evolve": ("evolve", None),
}
_OPTION_TO_CLI = {
    "compute_backend": "backend",
    "mode": "dynamic_mode",
    "pick": "evaluation_pick",
    "operator_constraints": "constraint",
}
_CONFIG_ONLY_OPTIONS = frozenset(
    {
        "model",
        "cli_timeout",
        "default_reasoning_effort",
        "metric_space",
        "profile_guided",
        "plan",
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
    if not hasattr(args, "modal"):
        return None
    # A run recorded with the retired host environment continues in Docker; the
    # runtime rewrites the record and logs the migration when it opens the run.
    recorded = migrate_recorded_run_environment(record).name
    skypilot = bool(getattr(args, "skypilot", False))
    if "run_environment" in explicit:
        requested = getattr(args, "run_environment", None)
    elif skypilot:
        requested = "skypilot"
    elif args.modal:
        requested = "modal"
    elif getattr(args, "slurm_config", None) is not None:
        requested = record.name if record.name == "slurm-gpu" else "slurm"
    else:
        requested = recorded
    explicit_environment = {
        "modal",
        "skypilot",
        "run_environment",
        "slurm_config",
    } & explicit
    if explicit_environment:
        return requested != recorded
    args.modal = recorded == "modal"
    if hasattr(args, "skypilot"):
        args.skypilot = recorded == "skypilot"
    if hasattr(args, "run_environment"):
        # A host run is derived from its backend, not selectable: leave the choice
        # unset so the runtime re-derives it and rejects a changed backend or host.
        args.run_environment = None if recorded == "host" else recorded
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
        if destination == "slurm_config" and expected is not None:
            expected = Path(expected)
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
    if destination == "objective":
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
        is_explicit = destination in explicit
        if is_explicit:
            requested = _normalized_resume_cli_value(destination, getattr(args, destination))
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


def _recorded_cli_selection(orchestration_id: str) -> tuple[str, str | None]:
    try:
        return _POLICY_CLI_SELECTION[orchestration_id]
    except KeyError:
        _configuration_error(
            f"The CLI cannot resume orchestration {orchestration_id!r}",
            code="project_resume_configuration_invalid",
            stage="resume_resolution",
        )


def _restore_descriptor_options(
    args: argparse.Namespace, manifest: OrchestrationRunManifest, *, loop_kind: str
) -> None:
    """Restore omitted flags from the single active descriptor format."""
    descriptor = manifest.orchestration
    recorded_loop, inner_loop = _recorded_cli_selection(descriptor.id)
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
        "model": manifest.execution.model,
        "compute_backend": manifest.execution.compute_backend,
        "profiler": manifest.execution.requested_profiler,
    }
    changed.extend(_restore_cli_fields(args, execution_cli, budget=budget, explicit=explicit))
    if recorded_loop == "evolve":
        changed.extend(_restore_evolve_objectives(args, options, explicit))
    if changed:
        _project_resume_mismatch(changed)
    args.project_run_configuration = SimpleNamespace(**manifest.execution.model_dump())


def _latest_run_id(project: Project) -> str:
    """The project's latest run, whichever branch is checked out (each run has its own)."""
    events = NullGitTrackerEvents()
    repository = open_git_repository(project.root, git=Git(OsProcesses()), faults=events)
    runs = GitRunHistory(project, repository).recorded().runs
    if not runs:
        message = f"No VibeSys runs exist under {project.root}"
        raise ProjectStateError(message)
    return runs[-1].run_id


def _requested_resume(args: argparse.Namespace, *, direct: bool) -> str | None:
    """The run a resume names, or pins with ``--run-id``; ``None`` selects the current run."""
    if direct and args.resume != "latest":
        if args.run_id not in (None, args.resume):
            _configuration_error(
                f"--run-id {args.run_id} names another run than --resume {args.resume}",
                code="invalid_arguments",
                stage="argument_parsing",
            )
        return str(args.resume)
    return None if args.run_id is None else str(args.run_id)


@dataclass(frozen=True)
class _ResumeSelection:
    project_root: Path
    run_id: str
    manifest: OrchestrationRunManifest


def _load_resume_selection(args: argparse.Namespace) -> _ResumeSelection | None:
    """Read saved identity and settings once, before choosing a strategy parser."""
    if args.resume is None:
        return None
    if args.repo is not None:
        _configuration_error(
            "--repo creates a remote for a new experiment and cannot be combined with --resume",
            code="invalid_arguments",
            stage="argument_parsing",
        )

    direct = args.runs_dir is None
    if not direct and _is_remote_project(args.resume):
        # Remote project discovery needs a clone, but launch configuration is
        # already available and must fail before GitHub auth or network access.
        _load_effective_config(args)
    project_root = (
        (args.input or Path.cwd()).expanduser().resolve()
        if direct
        else _resolve_project_root(args.resume, args.runs_dir)
    )
    try:
        project = Project.open(project_root)
        store = project.state
        run_id = _requested_resume(args, direct=direct)
        if run_id is None:
            run_id = store.current_run_id() or _latest_run_id(project)
        events = NullGitTrackerEvents()
        repository = open_git_repository(project_root, git=Git(OsProcesses()), faults=events)
        manifest = GitRunHistory(project, repository).manifest(run_id)

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
            f"Run {run_id!r} uses an unsupported run schema; only v5 runs can resume",
            code="unsupported_run_schema",
            stage="resume_resolution",
        )
    validate_descriptor(manifest.orchestration)
    return _ResumeSelection(project_root, run_id, manifest)


def _resolve_resume_args(
    args: argparse.Namespace,
    *,
    loop_kind: str,
    selection: _ResumeSelection | None = None,
    read_only: bool = False,
) -> None:
    selected = selection or _load_resume_selection(args)
    if selected is None:
        return
    project_root, run_id, manifest = selected.project_root, selected.run_id, selected.manifest
    if not read_only:
        ensure_resumable(project_root, run_id)
    args.resume = run_id
    args.run_id = run_id
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
