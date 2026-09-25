"""Pure parsing of one profiler's attribution output.

The impure half (running the configured profiler command over ``RunContext``)
stays in ``loops/profile_multi/attribution.py`` until the rewiring phase
moves the orchestration call site here. This module owns only what is
deterministic: extracting the framed result-protocol-v1 payload from raw
command output and validating it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vibesys.search.profile_focus.state import ProfileBottleneck

_BEGIN = "__VIBESYS_ATTRIBUTION_BEGIN__"
_END = "__VIBESYS_ATTRIBUTION_END__"


class ProfileAttributionError(RuntimeError):
    """The profile command's output could not be parsed as valid attribution."""

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


def framed_payload(output: str) -> str | None:
    """Extract the payload between the framework's begin/end markers, if any."""
    _, marker, framed = output.rpartition(_BEGIN)
    encoded, end_marker, _ = framed.partition(_END)
    if not marker or not end_marker:
        return None
    return encoded.strip()


def parse_attribution(output: str) -> tuple[ProfileBottleneck, ...]:
    """Parse and validate one profiler run's framed result protocol v1 output.

    Components are returned sorted by descending cost (ties broken by name),
    the ranking ``ProfileFocus.observe`` and the designer prompt both rely on.
    """
    payload = framed_payload(output)
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


__all__ = ["ProfileAttributionError", "framed_payload", "parse_attribution"]
