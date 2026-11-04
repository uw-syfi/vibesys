"""Plain input/output carriers for :class:`~vibesys.hypothesis.search.HypothesisSearch`.

Replaces the mutable ``HypothesisEngine``/``replace_state`` and the
``PlanRequest``/``RoundSelection``/``AttemptRequest``/``TerminalRequest``/
``TerminalTransition``/``MultiRound`` carrier types from
``loops/{multi,profile_multi}/decisions.py``: these are immutable data, not
objects strategies build up across turns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.hypothesis.transitions import CarryOver

if TYPE_CHECKING:
    from vibesys.hypothesis.state import Hypothesis, HypothesisState, RoundRecord

__all__ = [
    "AttemptBudget",
    "CarryOver",
    "ClosedRound",
    "Continue",
    "Finished",
    "NewHypothesis",
    "NextRoundDecision",
    "PlanningContext",
    "StartedHypothesis",
]


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """Evidence a designer turn needs, computed once per round."""

    round_number: int
    records: tuple[RoundRecord, ...]
    carry: CarryOver
    plateau_warning: str | None


@dataclass(frozen=True, slots=True)
class NewHypothesis:
    """No hypothesis is active: the strategy must plan a new one."""

    context: PlanningContext


@dataclass(frozen=True, slots=True)
class Continue:
    """A hypothesis is already active and should keep running."""

    hypothesis: Hypothesis
    context: PlanningContext


@dataclass(frozen=True, slots=True)
class Finished:
    """The round budget is exhausted; no further round should start."""


NextRoundDecision = NewHypothesis | Continue | Finished


@dataclass(frozen=True, slots=True)
class AttemptBudget:
    """One implementer attempt's position within its round's retry budget."""

    retry: int
    max_retries: int

    @property
    def remaining(self) -> int:
        """Attempts still available after this one, floored at zero."""
        return max(self.max_retries - self.retry, 0)

    @property
    def exhausted(self) -> bool:
        """Whether this attempt is already past the configured budget."""
        return self.retry > self.max_retries


@dataclass(frozen=True, slots=True)
class StartedHypothesis:
    """A freshly started hypothesis and the state it started in."""

    state: HypothesisState
    hypothesis: Hypothesis


@dataclass(frozen=True, slots=True)
class ClosedRound:
    """State and next-turn guidance committed after one completed round."""

    state: HypothesisState
    next_active: Hypothesis | None
    carry: CarryOver
    exhaustion_feedback: str | None
