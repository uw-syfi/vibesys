"""Run and validate profile attribution for this strategy."""

from __future__ import annotations

import contextlib
import shlex
import tempfile
import uuid
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from vibesys.loops.agent.state import ProfileBottleneck

if TYPE_CHECKING:
    from vibesys.evaluators.input_manifest import ProfileGuidedInput
    from vibesys.orchestration.runtime import RunContext

_BEGIN = "__VIBESYS_ATTRIBUTION_BEGIN__"
_END = "__VIBESYS_ATTRIBUTION_END__"


class ProfileAttributionError(RuntimeError):
    """The profile command failed to provide valid attribution."""

    @classmethod
    def execution(cls, error: Exception) -> ProfileAttributionError:
        """Report an execution failure."""
        return cls(f"profile-guided attribution command could not be executed: {error}")

    @classmethod
    def exit_code(cls, code: int | None) -> ProfileAttributionError:
        """Report a nonzero profiler exit code."""
        return cls(
            "profile-guided attribution command failed "
            f"with exit code {code}; check its output above"
        )

    @classmethod
    def missing_result(cls) -> ProfileAttributionError:
        """Report a missing framed output artifact."""
        return cls(
            "profile-guided attribution produced no result artifact; "
            "the command must write protocol v1 JSON to the path passed by --vs-output"
        )

    @classmethod
    def invalid_result(cls, error: ValueError) -> ProfileAttributionError:
        """Report a malformed output artifact."""
        return cls(f"profile-guided attribution returned invalid result protocol v1 JSON: {error}")

    @classmethod
    def duplicate_components(cls) -> ProfileAttributionError:
        """Report ambiguous attribution component names."""
        return cls("profile-guided attribution component names must be unique")


class _ProfileResultV1(BaseModel):
    """Exact task-to-framework attribution contract."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    cost_unit: str = Field(min_length=1)
    components: tuple[ProfileBottleneck, ...]


def _framed_payload(output: str) -> str | None:
    _, marker, framed = output.rpartition(_BEGIN)
    encoded, end_marker, _ = framed.partition(_END)
    if not marker or not end_marker:
        return None
    return encoded.strip()


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
        raise ProfileAttributionError.execution(error) from error
    finally:
        with contextlib.suppress(Exception):
            await ctx.environment.execute(f"rm -f -- {shlex.quote(output_path)}")
    if result.exit_code != 0:
        raise ProfileAttributionError.exit_code(result.exit_code)
    payload = _framed_payload(result.output)
    if payload is None:
        raise ProfileAttributionError.missing_result()
    try:
        parsed = _ProfileResultV1.model_validate_json(payload, strict=True)
    except ValueError as error:
        raise ProfileAttributionError.invalid_result(error) from error
    names = [component.name for component in parsed.components]
    if len(names) != len(set(names)):
        raise ProfileAttributionError.duplicate_components()
    return tuple(sorted(parsed.components, key=lambda item: (-item.cost, item.name)))
