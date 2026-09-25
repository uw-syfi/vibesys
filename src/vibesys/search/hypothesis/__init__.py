"""Pure hypothesis-lifecycle search policy.

Ports the deterministic logic from ``vibesys.agent_run.{hypotheses,evidence,
attempts,record}`` and the shared parts of ``loops/{multi,profile_multi}/
decisions.py`` into one policy strategies compose instead of hand-rolling.
See :class:`~vibesys.search.hypothesis.search.HypothesisSearch` for the
public entry point.
"""

from __future__ import annotations

from vibesys.search.hypothesis.config import HypothesisConfig
from vibesys.search.hypothesis.results import (
    AttemptBudget,
    CarryOver,
    ClosedRound,
    Continue,
    Finished,
    NewHypothesis,
    NextRoundDecision,
    PlanningContext,
    RollbackTarget,
    StartedHypothesis,
)
from vibesys.search.hypothesis.search import HypothesisSearch
from vibesys.search.hypothesis.state import (
    Hypothesis,
    HypothesisMeasurement,
    HypothesisResolution,
    HypothesisReview,
    HypothesisState,
    HypothesisStrategy,
)

__all__ = [
    "AttemptBudget",
    "CarryOver",
    "ClosedRound",
    "Continue",
    "Finished",
    "Hypothesis",
    "HypothesisConfig",
    "HypothesisMeasurement",
    "HypothesisResolution",
    "HypothesisReview",
    "HypothesisSearch",
    "HypothesisState",
    "HypothesisStrategy",
    "NewHypothesis",
    "NextRoundDecision",
    "PlanningContext",
    "RollbackTarget",
    "StartedHypothesis",
]
