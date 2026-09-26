"""Task-owned profile attribution execution for the multi-agent policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.search.profile_focus import ProfileAttributionError, parse_attribution

if TYPE_CHECKING:
    from vibesys.evaluators.input_manifest import ProfileGuidedInput
    from vibesys.search.profile_focus import ProfileBottleneck
    from vs_runtime.api import CommandResult, RunHost, Workspace

_BEGIN = "__VIBESYS_ATTRIBUTION_BEGIN__"
_END = "__VIBESYS_ATTRIBUTION_END__"


class _AttributionCommandError(ProfileAttributionError):
    """The configured attribution command failed before producing a result."""

    @classmethod
    def execution(cls, error: Exception) -> _AttributionCommandError:
        return cls(f"profile-guided attribution command could not be executed: {error}")

    @classmethod
    def exit_code(cls, code: int | None) -> _AttributionCommandError:
        return cls(
            "profile-guided attribution command failed "
            f"with exit code {code}; check its output above"
        )


async def _capture_output(
    host: RunHost,
    argv: tuple[str, ...],
    *,
    workspace: Workspace,
    output_argument: str,
    timeout_seconds: int,
) -> CommandResult:
    try:
        return await host.commands.capture_output(
            argv,
            workspace=workspace,
            output_argument=output_argument,
            timeout_seconds=timeout_seconds,
        )
    except Exception as error:
        raise _AttributionCommandError.execution(error) from error


async def run_attribution(
    host: RunHost,
    config: ProfileGuidedInput,
    *,
    workspace: Workspace,
) -> tuple[ProfileBottleneck, ...]:
    """Run the configured profiler and validate profile result protocol v1."""
    host.log(f"[profile-guidance] running attribution: {' '.join(config.command)}")
    result = await _capture_output(
        host,
        config.command,
        workspace=workspace,
        output_argument="--vs-output",
        timeout_seconds=config.timeout_seconds,
    )
    if result.exit_code != 0:
        raise _AttributionCommandError.exit_code(result.exit_code)
    return parse_attribution(f"{_BEGIN}\n{result.output}\n{_END}")


__all__ = ["run_attribution"]
