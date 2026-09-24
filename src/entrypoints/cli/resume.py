"""Restoring a resumed run's recorded CLI configuration and selecting its branch."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.cli.args import _parse_cli_objective
from entrypoints.cli.constants import (
    _AGENT_RESUME_CLI_FIELDS,
    _COMMON_RESUME_CLI_FIELDS,
    _EVOLVE_RESUME_CLI_FIELDS,
    _PLAIN_RESUME_CLI_FIELDS,
    _RUN_ENVIRONMENT_OPTION_CLI_FIELDS,
)
from entrypoints.cli.errors import _configuration_error, _project_resume_mismatch
from entrypoints.cli.loops import _migrate_run_environment_command, _resolve_project_root
from vibesys.api import ComputeBackend, ProfilerKind
from vibesys.api.request import coerce_profiler_kind
from vs_project.api import (
    AgentRunConfiguration,
    GitTracker,
    NullGitTrackerEvents,
    PlainRunConfiguration,
    Project,
    ProjectStateError,
    RunConfiguration,
    RunSchemaMigrationRequiredError,
)

if TYPE_CHECKING:
    import argparse


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


def _restore_resume_agent_backend(
    args: argparse.Namespace,
    recorded: AgentRunConfiguration,
    explicit: frozenset[str],
) -> bool:
    """Restore the backend and return whether an explicit value mismatched."""
    if not {"stub_agent", "agent_backend"}.intersection(explicit):
        args.stub_agent = recorded.agent_backend == "stub"
        args.agent_backend = None if args.stub_agent else recorded.agent_backend
        return False
    requested = "stub" if args.stub_agent else args.agent_backend
    return requested != recorded.agent_backend


def _restore_resume_constraints(
    args: argparse.Namespace,
    recorded: AgentRunConfiguration,
    explicit: frozenset[str],
) -> bool:
    """Restore constraints and return whether an explicit value mismatched."""
    if "constraint" not in explicit:
        args.constraint = list(recorded.operator_constraints)
        return False
    requested = tuple(constraint.strip() for constraint in args.constraint if constraint.strip())
    return requested != recorded.operator_constraints


def _restore_resume_run_environment(
    args: argparse.Namespace,
    recorded: RunConfiguration,
    explicit: frozenset[str],
) -> list[str]:
    """Restore the recorded runtime environment and reject contradictions.

    ``--docker`` and ``--modal`` are store-true flags, so an omitted flag is
    indistinguishable from ``--flag false``. The recorded environment therefore
    wins whenever the resume invocation says nothing about it, and only an
    explicitly passed flag that contradicts the recording is an error.
    """
    record = recorded.run_environment
    changed: list[str] = []
    if not hasattr(args, "docker") or not hasattr(args, "modal"):
        return changed
    skypilot = bool(getattr(args, "skypilot", False))
    generic = getattr(args, "run_environment", None)
    if {"docker", "modal", "skypilot", "run_environment"} & explicit:
        requested = (
            generic
            if "run_environment" in explicit
            else "skypilot"
            if skypilot
            else "modal"
            if args.modal
            else "docker"
            if args.docker
            else "local"
        )
        if requested != record.name:
            changed.append("run_environment")
    else:
        args.docker = record.name == "docker"
        args.modal = record.name == "modal"
        if hasattr(args, "skypilot"):
            args.skypilot = record.name == "skypilot"
        if hasattr(args, "run_environment"):
            args.run_environment = record.name

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
    if destination == "backend" and value is not None:
        assert isinstance(value, ComputeBackend)  # argparse contract
        return value.value
    if destination == "profiler":
        assert isinstance(value, ProfilerKind)  # argparse contract
        return ProfilerKind.NONE.value if value is ProfilerKind.AUTO else value.value
    return value


def _set_resume_cli_value(
    args: argparse.Namespace,
    destination: str,
    value: object,
) -> None:
    if destination == "backend":
        try:
            value = ComputeBackend(value)
        except ValueError:
            _configuration_error(
                f"Run metadata records unknown compute backend {value!r}",
                code="project_resume_configuration_invalid",
                stage="resume_resolution",
            )
    elif destination == "profiler":
        if value is not None and not isinstance(value, str):
            _configuration_error(
                f"Run metadata records unknown profiler {value!r}",
                code="project_resume_configuration_invalid",
                stage="resume_resolution",
            )
        try:
            value = coerce_profiler_kind(value or ProfilerKind.AUTO.value)
        except ValueError:
            _configuration_error(
                f"Run metadata records unknown profiler {value!r}",
                code="project_resume_configuration_invalid",
                stage="resume_resolution",
            )
    setattr(args, destination, value)


def _restore_project_resume_cli_args(
    args: argparse.Namespace,
    recorded: RunConfiguration,
    *,
    loop_kind: str,
) -> None:
    """Restore omitted run flags and reject explicit changes on resume."""
    if recorded.outer_loop != loop_kind:
        _configuration_error(
            f"Run uses --outer-loop {recorded.outer_loop}, not {loop_kind}",
            code="project_resume_configuration_mismatch",
            stage="resume_resolution",
        )

    explicit = getattr(args, "explicit_cli_dests", frozenset())
    fields, changed = _restore_loop_resume_fields(args, recorded, explicit)

    for destination, field in fields.items():
        if not hasattr(args, destination):
            continue
        expected = getattr(recorded, field)
        if destination in explicit:
            requested = _normalized_resume_cli_value(destination, getattr(args, destination))
            if requested != expected:
                changed.append(field)
            continue
        _set_resume_cli_value(args, destination, expected)

    if changed:
        _project_resume_mismatch(changed)


def _restore_loop_resume_fields(
    args: argparse.Namespace,
    recorded: RunConfiguration,
    explicit: frozenset[str],
) -> tuple[dict[str, str], list[str]]:
    """Restore a loop's budget and return its immutable CLI field map."""
    fields = dict(_COMMON_RESUME_CLI_FIELDS)
    changed: list[str] = _restore_resume_run_environment(args, recorded, explicit)
    if isinstance(recorded, AgentRunConfiguration):
        _restore_resume_budget(
            args,
            destination="max_rounds",
            recorded_value=recorded.max_rounds,
            explicit=explicit,
        )
        fields.update(_AGENT_RESUME_CLI_FIELDS)
        if _restore_resume_agent_backend(args, recorded, explicit):
            changed.append("agent_backend")
        if _restore_resume_constraints(args, recorded, explicit):
            changed.append("operator_constraints")
        fields.pop("agent_backend", None)
    elif isinstance(recorded, PlainRunConfiguration):
        _restore_resume_budget(
            args,
            destination="max_rounds",
            recorded_value=recorded.max_rounds,
            explicit=explicit,
        )
        fields.update(_PLAIN_RESUME_CLI_FIELDS)
    else:
        _restore_resume_budget(
            args,
            destination="max_generations",
            recorded_value=recorded.max_generations,
            explicit=explicit,
        )
        fields.update(_EVOLVE_RESUME_CLI_FIELDS)
        if "objective" in explicit:
            requested = tuple(f"{item.name}:{item.direction}" for item in args.objective)
            if requested != recorded.objectives:
                changed.append("objectives")
        else:
            args.objective = [_parse_cli_objective(item) for item in recorded.objectives]
    return fields, changed


def _switch_project_resume_branch(project_root: Path, run_id: str) -> None:
    """Select the run branch before callers read committed project files."""
    # Store-only fixtures and corrupt partial initializations are diagnosed by
    # context creation. Real runs always have a repository here.
    if not (project_root / ".git").exists():
        return
    tracker = GitTracker(
        project_root,
        events=NullGitTrackerEvents(),
        run_id=run_id,
    )
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
        run_manifest = store.load_run(run_id)
    except RunSchemaMigrationRequiredError as exc:
        _configuration_error(
            f"{exc} Run: {_migrate_run_environment_command(project_root, exc.run_id)}",
            code="project_run_schema_migration_required",
            stage="resume_resolution",
        )
    except ProjectStateError as exc:
        _configuration_error(
            f"Cannot resume project run: {exc}",
            code="resume_not_found",
            stage="resume_resolution",
        )
    args.resume = run_id
    args.exp_name = run_id
    args.input = project_root
    if run_manifest.task_name is not None:
        if args.task is not None and args.task != run_manifest.task_name:
            _configuration_error(
                f"Run uses task {run_manifest.task_name!r}, not {args.task!r}",
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
            )
        args.task = run_manifest.task_name
    args.project_run_configuration = run_manifest.configuration
    _restore_project_resume_cli_args(args, run_manifest.configuration, loop_kind=loop_kind)
