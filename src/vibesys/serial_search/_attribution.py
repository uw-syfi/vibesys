"""The profile-guided preset's attribution command and the focus policy it feeds.

The command runs against a retained revision (the `CaptureCommand` operation) and prints the
profile result protocol; the ranked components it reports steer the designer through
`ProfileFocus`, and each closed round updates the focus state. These are pure functions over the
hypothesis ledger, shared by every strategy that offers the preset.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.hypothesis import rounds
from vibesys.profile_focus import (
    FocusView,
    ProfileBottleneck,
    ProfileFocus,
    ProfileFocusConfig,
    parse_attribution,
)

# The delimiters `parse_attribution` expects around the command's output.
_BEGIN = "__VIBESYS_ATTRIBUTION_BEGIN__"
_END = "__VIBESYS_ATTRIBUTION_END__"
# The argument the runtime appends, followed by the temporary output path.
OUTPUT_ARGUMENT = "--vs-output"

type Positive = Annotated[int, Field(gt=0)]


class AttributionConfig(BaseModel):
    """The profile-guided preset's attribution command and its plateau policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: Positive = 1800
    min_measured_rounds: Positive = 2
    min_relative_improvement: Annotated[float, Field(ge=0)] = 0.02


def focus_policy(attribution: AttributionConfig | None) -> ProfileFocus | None:
    """The component-focus policy of a profile-guided run, else None."""
    if attribution is None:
        return None
    return ProfileFocus(
        ProfileFocusConfig(
            plateau_min_rounds=attribution.min_measured_rounds,
            min_relative_improvement=attribution.min_relative_improvement,
        )
    )


def bottlenecks_of(output: str) -> tuple[ProfileBottleneck, ...]:
    """The ranked components the attribution command printed.

    Raises:
        ProfileAttributionError: the output is not a valid profile result.
    """
    return parse_attribution(f"{_BEGIN}\n{output}\n{_END}")


def observe(
    ledger: rounds.Ledger,
    policy: ProfileFocus,
    *,
    round_number: int,
    bottlenecks: tuple[ProfileBottleneck, ...],
) -> rounds.Ledger:
    """The ledger once the attribution of ``round_number`` is folded into the focus state."""
    guidance = ledger.search.profile_guidance or policy.initial()
    focused = policy.observe(guidance, round_number=round_number, bottlenecks=bottlenecks)
    search = ledger.search.model_copy(update={"profile_guidance": focused}, deep=True)
    return rounds.Ledger(search, ledger.carry)


def focus_of(ledger: rounds.Ledger, policy: ProfileFocus | None) -> FocusView | None:
    """What the designer sees of the focus state, or None for a plain run."""
    guidance = ledger.search.profile_guidance
    if policy is None or guidance is None:
        return None
    return policy.focus(guidance)


def record_round(
    ledger: rounds.Ledger, policy: ProfileFocus | None, *, passed: bool
) -> rounds.Ledger:
    """Record the newest closed round's result on a profile-guided run's focus state."""
    guidance = ledger.search.profile_guidance
    if policy is None or guidance is None:
        return ledger
    record = ledger.records[-1]
    delta = record.perf_delta_pct
    focused = policy.record(
        guidance,
        round_number=record.round_number,
        passed=passed and record.official_evaluation,
        relative_improvement=None if delta is None else delta / 100,
    )
    return rounds.Ledger(
        ledger.search.model_copy(update={"profile_guidance": focused}), ledger.carry
    )
