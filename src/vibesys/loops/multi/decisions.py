"""Prompt-shaping carriers the multi strategy passes to its turns.

Hypothesis scheduling (when to start, continue, or retire a claim, and when
review or an official evaluation is due) lives in
``vibesys.search.hypothesis``, and profile-focus component selection (when
``options.profile`` turns profiling on) in ``vibesys.search.profile_focus``,
both driven directly by ``session.py``. This module keeps only what
``turns.py`` renders prompts from: the small request objects. When profiling
is off, ``session.py`` passes ``FocusView()``, whose default fields render
nothing (see the ``is defined and ...`` guards in the shared templates), so
a plain multi run and a profile-guided one share one ``profile_guidance``
type instead of a separate no-op guidance carrier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.roles.profiler import ProfilerSummary
    from vibesys.search.hypothesis import CarryOver
    from vibesys.search.hypothesis.plan import OrchestratorPlan
    from vibesys.search.hypothesis.state import Hypothesis, HypothesisState
    from vibesys.search.profile_focus import FocusView
    from vs_loop_state.api import RoundRecord


@dataclass(frozen=True)
class PlanRequest:
    """Evidence supplied to the multi designer."""

    round_number: int
    state: HypothesisState
    records: list[RoundRecord]
    carry: CarryOver
    profiler_summary: ProfilerSummary | None
    plateau_warning: str | None
    provisional_candidates: int
    profile_guidance: FocusView


@dataclass(frozen=True)
class AttemptRequest:
    """Multi strategy facts for one bounded implementer attempt."""

    round_number: int
    plan: OrchestratorPlan
    planned_official_reason: str | None
    records: list[RoundRecord]
    active_hypothesis: Hypothesis
    profile_focus: FocusView
    last_profile_focus: str
