"""Task-owned profile attribution execution for the multi-agent policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.profile_focus import ProfileAttributionError, parse_attribution

if TYPE_CHECKING:
    from vibesys.inputs import ProfileGuidedInput
    from vibesys.profile_focus import ProfileBottleneck
    from vs_runtime.api import CommandResult, Run, Workspace

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
    run: Run,
    argv: tuple[str, ...],
    *,
    workspace: Workspace,
    output_argument: str,
    timeout_seconds: int,
) -> CommandResult:
    try:
        return await run.commands.capture_output(
            argv,
            workspace=workspace,
            output_argument=output_argument,
            timeout_seconds=timeout_seconds,
        )
    except Exception as error:
        raise _AttributionCommandError.execution(error) from error


async def run_attribution(
    run: Run,
    config: ProfileGuidedInput,
    *,
    workspace: Workspace,
) -> tuple[ProfileBottleneck, ...]:
    """Run the configured profiler and validate profile result protocol v1."""
    run.observations.note(f"[profile-guidance] running attribution: {' '.join(config.command)}")
    result = await _capture_output(
        run,
        config.command,
        workspace=workspace,
        output_argument="--vs-output",
        timeout_seconds=config.timeout_seconds,
    )
    if result.exit_code != 0:
        raise _AttributionCommandError.exit_code(result.exit_code)
    return parse_attribution(f"{_BEGIN}\n{result.output}\n{_END}")


__all__ = ["run_attribution"]
