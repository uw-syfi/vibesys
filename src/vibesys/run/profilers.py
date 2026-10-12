"""Bind pure profiler policy to product configuration and host preflight."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Protocol

from vibesys.composition import agent_spec_from_config
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.orchestration.profilers import (
    ACTIVE_PROFILER_KINDS,
    ProfilerKind,
    ProfilerPreflightResult,
    preflight_profiler_kind,
    profiler_definition,
    resolve_profiler_choice,
)
from vs_agent.api import agent_supports_tool_servers
from vs_project.api import Project, ProjectLayoutError
from vs_runtime.api.infrastructure import (
    NativeCpuProfilerKind,
    RunEnvironment,
    ToolSite,
    build_run_environment,
    make_run_environment_spec,
    preflight_native_cpu_profiler,
    validate_run_environment_profile,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.run.contracts import RunRequest


class ProfilerPreflight(Protocol):
    """Check whether one resolved native profiler is usable on this host."""

    def __call__(self, kind: ProfilerKind, /) -> ProfilerPreflightResult:
        """Return structured capability evidence without opening run resources."""
        ...


def native_profiler_preflight(kind: ProfilerKind) -> ProfilerPreflightResult:
    """Adapt runtime-native host checks into profiler policy results."""
    native_kind = {
        ProfilerKind.LINUX_CPU: NativeCpuProfilerKind.LINUX,
        ProfilerKind.MACOS_CPU: NativeCpuProfilerKind.MACOS,
    }[kind]
    capability = preflight_native_cpu_profiler(native_kind)
    return ProfilerPreflightResult(
        kind,
        capability.usable,
        capability.diagnostics,
        capability.details,
    )


def profiler_tool_problem(kind: ProfilerKind, environment: RunEnvironment) -> str | None:
    """Why the environment cannot run *kind*'s tool at the site it needs, or ``None``.

    The message names the profiler, the executable, the site and the environment's reason,
    so an unrunnable profiler is refused at run start instead of failing in every turn.
    """
    if kind not in ACTIVE_PROFILER_KINDS:
        return None
    definition = profiler_definition(kind)
    if definition.tool is None:
        return None
    location = environment.locate_tool(
        definition.tool.executable, ToolSite(definition.tool.site.value)
    )
    if location.available:
        return None
    return (
        f"profiler {kind.value!r} needs `{definition.tool.executable}` at the "
        f"{definition.tool.site.value.replace('_', ' ')}, but {location.reason}."
    )


def _require_runnable_tool(
    request: RunRequest,
    environment: RunEnvironment,
    resolved: ProfilerKind,
    log: Callable[[str], None] | None,
) -> ProfilerKind:
    """Refuse an explicit profiler whose tool cannot run here; ``auto`` falls back to none."""
    problem = profiler_tool_problem(resolved, environment)
    if problem is None:
        return resolved
    if request.profiler_kind is not ProfilerKind.AUTO:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="profiler_tool_unavailable",
                stage="profiler_validation",
                message=f"--profiler {resolved.value}: {problem}",
            )
        )
    if log is not None:
        log(f"[profiler] {problem} --profiler auto is falling back to 'none'.")
    return ProfilerKind.NONE


def resolve_run_profiler(
    request: RunRequest,
    environment: RunEnvironment,
    *,
    preflight: bool = True,
    native_preflight: ProfilerPreflight = native_profiler_preflight,
    log: Callable[[str], None] | None = None,
) -> ProfilerKind:
    """Resolve and validate the profiler selected for one product run."""
    config = request.config
    try:
        choice = resolve_profiler_choice(
            request.profiler_kind,
            domain=request.input_bundle.domain,
            backend=request.backend,
            environment_default_profiler_kind=ProfilerKind(environment.default_profiler_id),
            environment_supported_profiler_kinds=(
                None
                if environment.supported_profiler_ids is None
                else frozenset(ProfilerKind(value) for value in environment.supported_profiler_ids)
            ),
        )
    except ValueError as exc:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="profiler_incompatible",
                stage="profiler_validation",
                message=f"--profiler: {exc}",
            )
        ) from exc
    resolved = choice.kind
    if choice.reason is not None and log is not None:
        log(f"[profiler] --profiler auto chose 'none': {choice.reason}.")
    resolved = _require_runnable_tool(request, environment, resolved, log)
    if preflight and environment.requires_local_profiler_preflight:
        result = preflight_profiler_kind(resolved, native_preflight=native_preflight)
        if not result.usable:
            if request.profiler_kind is not ProfilerKind.AUTO:
                raise ConfigurationError(
                    ConfigurationDiagnostic(
                        code="profiler_preflight_failed",
                        stage="profiler_preflight",
                        message=result.error_message(),
                    )
                )
            if log is not None:
                log(
                    f"[profiler] {result.error_message()} "
                    "--profiler auto is falling back to 'none'."
                )
            resolved = ProfilerKind.NONE
    if resolved in ACTIVE_PROFILER_KINDS:
        try:
            validate_run_environment_profile(environment, request.input_bundle.profile_command)
        except ValueError as exc:
            raise ConfigurationError(
                ConfigurationDiagnostic(
                    code="profile_workload_invalid",
                    stage="profiler_validation",
                    message=str(exc),
                )
            ) from exc
        agent_spec = agent_spec_from_config(
            config,
            backend=request.agent_backend,
            provider=request.cli_provider,
            model=config.model.name,
        )
        if not agent_supports_tool_servers(agent_spec):
            definition = profiler_definition(resolved)
            raise ConfigurationError(
                ConfigurationDiagnostic(
                    code="agent_profiler_incompatible",
                    stage="agent_capability_validation",
                    message=(
                        f"Profiler {resolved.value!r} requires agent tool server "
                        f"{definition.mcp_name!r}, but agent backend "
                        f"{agent_spec.backend.value!r} does not support agent tool "
                        "servers. Select the 'cli' agent backend or "
                        "disable profiling with --profiler none."
                    ),
                )
            )
    return resolved


def _require_attribution_runnable(command: tuple[str, ...], environment: RunEnvironment) -> None:
    """Refuse a profile-guided attribution command whose executable is not at its site.

    The command runs through the run's command runner, at the command environment site.
    An environment that can inspect that site (the host) refuses an executable that is not
    there, naming the command and the missing piece, before any agent work; one that cannot
    (a container image, a Slurm editor container) assumes it, and a command that then fails
    is reported by the strategy with the command and where it ran.
    """
    location = environment.locate_tool(command[0], ToolSite.COMMAND_ENVIRONMENT)
    if not location.available:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="attribution_command_unavailable",
                stage="profiler_validation",
                message=(
                    f"[profile_guided] command {shlex.join(command)!r} cannot run in the run's "
                    f"command environment: {location.reason}."
                ),
            )
        )


def validate_run_request(
    request: RunRequest, *, attribution_command: tuple[str, ...] | None = None
) -> None:
    """Validate placement, profiler policy, and workload before opening run resources.

    *attribution_command* is the profile-guided strategy's command, when the selected
    orchestration has one; it must be runnable in the run's command environment.
    """
    if request.runs_dir is not None:
        try:
            Project.validate_collection_root(request.runs_dir)
        except ProjectLayoutError as exc:
            raise ConfigurationError(
                ConfigurationDiagnostic(
                    code="invalid_runs_dir",
                    stage="request_validation",
                    message=f"--runs-dir: {exc}",
                )
            ) from exc
    environment = build_run_environment(request.run_environment or make_run_environment_spec())
    resolve_run_profiler(request, environment, preflight=False)
    if attribution_command is not None:
        _require_attribution_runnable(attribution_command, environment)


def validate_attribution_command(request: RunRequest, command: tuple[str, ...] | None) -> None:
    """Refuse a profile-guided attribution *command* the run's command environment cannot run.

    For callers that validate the request before the orchestration options are parsed and so
    cannot pass the command to :func:`validate_run_request`.
    """
    if command is None:
        return
    environment = build_run_environment(request.run_environment or make_run_environment_spec())
    _require_attribution_runnable(command, environment)


__all__ = [
    "native_profiler_preflight",
    "resolve_run_profiler",
    "validate_attribution_command",
    "validate_run_request",
]
