"""Run this strategy's configured profiler command and parse its attribution.

Parsing (extracting and validating the framed result-protocol-v1 payload) is
pure and lives in ``vibesys.search.profile_focus.attribution``; this module
keeps only what needs ``RunContext``: running the profiler command and
raising for the ways that execution itself can fail.
"""

from __future__ import annotations

import contextlib
import shlex
import tempfile
import uuid
from typing import TYPE_CHECKING

from vibesys.search.profile_focus import ProfileAttributionError, parse_attribution

if TYPE_CHECKING:
    from vibesys.evaluators.input_manifest import ProfileGuidedInput
    from vibesys.orchestration.runtime import RunContext
    from vibesys.search.profile_focus import ProfileBottleneck

_BEGIN = "__VIBESYS_ATTRIBUTION_BEGIN__"
_END = "__VIBESYS_ATTRIBUTION_END__"


def _attribution_execution_error(error: Exception) -> ProfileAttributionError:
    """Report an execution failure."""
    return ProfileAttributionError(
        f"profile-guided attribution command could not be executed: {error}"
    )


def _attribution_exit_code_error(code: int | None) -> ProfileAttributionError:
    """Report a nonzero profiler exit code."""
    return ProfileAttributionError(
        f"profile-guided attribution command failed with exit code {code}; check its output above"
    )


async def run_attribution(
    ctx: RunContext, config: ProfileGuidedInput, *, round_number: int
) -> tuple[ProfileBottleneck, ...]:
    """Run the configured profiler and validate profile result protocol v1."""
    output_path = (
        f"{tempfile.gettempdir()}/vibesys-attribution-{round_number}-{uuid.uuid4().hex[:12]}.json"
    )
    profiler_command = shlex.join((*config.command, "--vs-output", output_path))
    command = (
        f"rm -f -- {shlex.quote(output_path)}"
        f" && {profiler_command}"
        f" && printf '\n{_BEGIN}\n'"
        f" && cat {shlex.quote(output_path)}"
        f" && printf '\n{_END}\n'"
    )
    ctx.log(f"[profile-guidance] running attribution: {shlex.join(config.command)}")
    try:
        result = await ctx.environment.execute(command, timeout_seconds=config.timeout_seconds)
    except Exception as error:
        raise _attribution_execution_error(error) from error
    finally:
        with contextlib.suppress(Exception):
            await ctx.environment.execute(f"rm -f -- {shlex.quote(output_path)}")
    if result.exit_code != 0:
        raise _attribution_exit_code_error(result.exit_code)
    return parse_attribution(result.output)
