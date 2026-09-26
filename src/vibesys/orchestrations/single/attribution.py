"""Task-owned profile attribution execution for the single-agent policy."""

from __future__ import annotations

import contextlib
import tempfile
import uuid
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

    @classmethod
    def unreadable_result(cls, code: int | None) -> _AttributionCommandError:
        return cls(
            f"profile-guided attribution result artifact could not be read (exit code {code})"
        )


async def _run_command(
    host: RunHost,
    argv: tuple[str, ...],
    *,
    workspace: Workspace,
    timeout_seconds: int | None = None,
) -> CommandResult:
    try:
        return await host.commands.run(
            argv,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
        )
    except Exception as error:
        raise _AttributionCommandError.execution(error) from error


async def run_attribution(
    host: RunHost,
    config: ProfileGuidedInput,
    *,
    workspace: Workspace,
    round_number: int,
) -> tuple[ProfileBottleneck, ...]:
    """Run the configured profiler and validate profile result protocol v1.

    The runtime accepts argv only. The policy therefore performs execution,
    artifact readback, and cleanup as three explicit sandboxed commands instead
    of embedding shell composition in the runtime contract.
    """
    output_path = (
        f"{tempfile.gettempdir()}/vibesys-attribution-{round_number}-{uuid.uuid4().hex[:12]}.json"
    )
    host.log(f"[profile-guidance] running attribution: {' '.join(config.command)}")
    try:
        result = await _run_command(
            host,
            (*config.command, "--vs-output", output_path),
            workspace=workspace,
            timeout_seconds=config.timeout_seconds,
        )
        if result.exit_code != 0:
            raise _AttributionCommandError.exit_code(result.exit_code)
        artifact = await _run_command(
            host,
            ("cat", output_path),
            workspace=workspace,
        )
        if artifact.exit_code != 0:
            raise _AttributionCommandError.unreadable_result(artifact.exit_code)
    finally:
        with contextlib.suppress(Exception):
            await host.commands.run(("rm", "-f", output_path), workspace=workspace)
    framed = f"{_BEGIN}\n{artifact.output}\n{_END}"
    return parse_attribution(framed)


__all__ = ["run_attribution"]
