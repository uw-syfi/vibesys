"""Prompt-shaping carriers the profile guided multi strategy passes to its turns.

Hypothesis scheduling (when to start, continue, or retire a claim, and when
review or an official evaluation is due) lives in
``vibesys.search.hypothesis``, and profile-focus component selection in
``vibesys.search.profile_focus``, both driven directly by ``session.py``.
This module keeps only what ``turns.py`` renders prompts from: the small
request objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.agent_run.evidence import CarryOver
    from vibesys.agent_run.state import AgentRunState, Hypothesis
    from vibesys.roles.profiler import ProfilerSummary
    from vibesys.schemas import OrchestratorPlan
    from vibesys.search.profile_focus import FocusView
    from vs_loop_state.api import RoundRecord


@dataclass(frozen=True)
class PlanRequest:
    """Evidence supplied to the profile guided multi designer."""

    round_number: int
    state: AgentRunState
    records: list[RoundRecord]
    carry: CarryOver
    profiler_summary: ProfilerSummary | None
    plateau_warning: str | None
    provisional_candidates: int
    profile_guidance: FocusView


@dataclass(frozen=True)
class AttemptRequest:
    """Profile guided multi strategy facts for one bounded implementer attempt."""

    round_number: int
    plan: OrchestratorPlan
    planned_official_reason: str | None
    records: list[RoundRecord]
    active_hypothesis: Hypothesis
    profile_focus: FocusView
    last_profile_focus: str
