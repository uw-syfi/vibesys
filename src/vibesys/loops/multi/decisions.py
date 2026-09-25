"""Prompt-shaping carriers the multi strategy passes to its turns.

Hypothesis scheduling (when to start, continue, or retire a claim, and when
review or an official evaluation is due) now lives in
``vibesys.search.hypothesis`` and is driven directly by ``session.py``. This
module keeps only what ``turns.py`` renders prompts from: the small request
objects, and the no-op guidance chain multi has always rendered through.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.agent_run.evidence import CarryOver
    from vibesys.agent_run.state import AgentRunState, Hypothesis
    from vibesys.schemas import OrchestratorPlan, ProfilerSummary
    from vs_loop_state.api import RoundRecord


@dataclass(frozen=True)
class PlainGuidance:
    """Prompt context for a strategy without component attribution."""

    def plan_prompt_context(self) -> dict[str, object]:
        """Render no profile constraints for a designer."""
        return {}

    def implementer_prompt_context(self) -> dict[str, object]:
        """Render no profile constraints for an implementer."""
        return {}


@dataclass(frozen=True)
class _StaticGuidance:
    """Fixed stand-in for the old mutable hypothesis engine's guidance chain.

    ``turns.py`` renders ``request.engine.controller.guidance...``. Multi no
    longer carries a stateful engine (``session.py`` holds one
    ``HypothesisState`` value instead), so every request shares this single
    handle exposing the same no-op guidance the engine always returned.
    """

    @property
    def controller(self) -> _StaticGuidance:
        """Expose prompt guidance through the existing engine vocabulary."""
        return self

    @property
    def guidance(self) -> PlainGuidance:
        """Return empty component guidance."""
        return PlainGuidance()


STATIC_GUIDANCE = _StaticGuidance()


@dataclass(frozen=True)
class PlanRequest:
    """Evidence supplied to the multi designer."""

    round_number: int
    state: AgentRunState
    records: list[RoundRecord]
    carry: CarryOver
    profiler_summary: ProfilerSummary | None
    plateau_warning: str | None
    provisional_candidates: int
    profile_guidance: PlainGuidance


@dataclass(frozen=True)
class AttemptRequest:
    """Multi strategy facts for one bounded implementer attempt."""

    round_number: int
    plan: OrchestratorPlan
    planned_official_reason: str | None
    records: list[RoundRecord]
    active_hypothesis: Hypothesis
    engine: _StaticGuidance
    last_profile_focus: str
