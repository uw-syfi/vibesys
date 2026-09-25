"""Pre-round role family: decide whether a profiling pass precedes planning.

Used by ``multi`` only today.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from vibesys.runtime import Fresh, ReadOnly, Role


class PreRoundContext(BaseModel):
    """Context for the multi strategy's pre-round profiling decision."""

    model_config = ConfigDict(frozen=True)

    objective_location: str
    regression_info: str | None
    exhaustion_info: str | None
    progress_location: str
    profiler_kind: str
    profile_execution: str
    has_history: bool


class PreRoundDecision(BaseModel):
    """Pre-round decision: does the orchestrator need a profile before planning?"""

    need_profile: bool = Field(
        description="True if a profiler run should happen before the orchestrator plans this round's task."
    )
    profile_focus: str = Field(
        default="",
        description="Guidance for the profiler (e.g. 'focus on decode-path kernels'). Empty when need_profile is False.",
    )
    reasoning: str = Field(description="Short explanation of the decision. One or two sentences.")


def _fallback_pre_round_decision() -> PreRoundDecision:
    return PreRoundDecision(
        need_profile=False, profile_focus="", reasoning="fallback: default to skip"
    )


MULTI_PRE_ROUND_DECISION = Role(
    id="orchestrator",
    template="loops/multi/orchestrator_pre_round_prompt.j2",
    reply=PreRoundDecision,
    fallback=_fallback_pre_round_decision,
    context=PreRoundContext,
    access=ReadOnly(),
    session=Fresh(),
    message=(
        "Decide whether a profiling pass is needed before planning this round. "
        "Return only the JSON object."
    ),
)

ALL_ROLES = (MULTI_PRE_ROUND_DECISION,)
