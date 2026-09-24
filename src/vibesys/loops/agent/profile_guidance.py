"""Attribution process adapter for profile-guided hypothesis policy."""

from __future__ import annotations

import contextlib
import shlex
import uuid
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from vibesys.loops.agent.model import ProfileBottleneck

if TYPE_CHECKING:
    from vibesys.evaluators.input_manifest import ProfileGuidedInput
    from vibesys.run import LoopContext

_ATTRIBUTION_MARKER = "__VIBESYS_ATTRIBUTION_BEGIN__"
_ATTRIBUTION_END_MARKER = "__VIBESYS_ATTRIBUTION_END__"


class ProfileGuidanceError(RuntimeError):
    """A required attribution command or result contract failed."""


class _ProfileResultV1(BaseModel):
    """Exact task-to-framework attribution contract."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    cost_unit: str = Field(min_length=1)
    components: tuple[ProfileBottleneck, ...]


def run_attribution(
    ctx: LoopContext,
    config: ProfileGuidedInput,
    *,
    round_number: int,
) -> tuple[ProfileBottleneck, ...]:
    """Run the configured profiler and validate profile result protocol v1."""
    output_path = f"/tmp/vibesys-attribution-{round_number}-{uuid.uuid4().hex[:12]}.json"  # noqa: S108  # lint-waiver: LW-010206 [S108]; profiler and benchmark commands exchange output through the evaluator's shared /tmp namespace.
    profiler_command = shlex.join((*config.command, "--vs-output", output_path))
    command = (
        f"rm -f -- {shlex.quote(output_path)}"
        f" && {profiler_command}"
        f" && printf '\n{_ATTRIBUTION_MARKER}\n'"
        f" && cat {shlex.quote(output_path)}"
        f" && printf '\n{_ATTRIBUTION_END_MARKER}\n'"
    )
    ctx.lprint(f"[profile-guidance] running attribution: {shlex.join(config.command)}")
    try:
        result = ctx.judge_backend.execute(command, timeout=config.timeout_seconds)
    except Exception as exc:
        _exception_message_3 = f"profile-guided attribution command could not be executed: {exc}"
        raise ProfileGuidanceError(_exception_message_3) from exc
    finally:
        with contextlib.suppress(Exception):
            ctx.judge_backend.execute(f"rm -f -- {shlex.quote(output_path)}")
    if result.exit_code != 0:
        message = f"profile-guided attribution command failed with exit code {result.exit_code}; check its output above"
        raise ProfileGuidanceError(message)
    payload = _framed_payload(result.output)
    if payload is None:
        _exception_message = "profile-guided attribution produced no result artifact; the command must write protocol v1 JSON to the path passed by --vs-output"
        raise ProfileGuidanceError(_exception_message)
    try:
        result_v1 = _ProfileResultV1.model_validate_json(payload, strict=True)
    except ValueError as exc:
        _exception_message_4 = (
            f"profile-guided attribution returned invalid result protocol v1 JSON: {exc}"
        )
        raise ProfileGuidanceError(_exception_message_4) from exc
    names = [component.name for component in result_v1.components]
    if len(names) != len(set(names)):
        _exception_message_2 = "profile-guided attribution component names must be unique"
        raise ProfileGuidanceError(_exception_message_2)
    return tuple(sorted(result_v1.components, key=lambda item: (-item.cost, item.name)))


def _framed_payload(output: str) -> str | None:
    _, marker, framed = output.rpartition(_ATTRIBUTION_MARKER)
    encoded, end_marker, _ = framed.partition(_ATTRIBUTION_END_MARKER)
    if not marker or not end_marker:
        return None
    return encoded.strip()
