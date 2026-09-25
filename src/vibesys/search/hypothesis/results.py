"""Plain input/output carriers for :class:`~vibesys.search.hypothesis.search.HypothesisSearch`.

Replaces the mutable ``HypothesisEngine``/``replace_state`` and the
``PlanRequest``/``RoundSelection``/``AttemptRequest``/``TerminalRequest``/
``TerminalTransition``/``MultiRound`` carrier types from
``loops/{multi,profile_multi}/decisions.py``: these are immutable data, not
objects strategies build up across turns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.search.hypothesis.transitions import CarryOver

if TYPE_CHECKING:
    from vibesys.search.hypothesis.state import Hypothesis, HypothesisState, RoundRecord

__all__ = [
    "AttemptBudget",
    "CarryOver",
    "ClosedRound",
    "Continue",
    "Finished",
    "NewHypothesis",
    "NextRoundDecision",
    "PlanningContext",
    "RollbackTarget",
    "StartedHypothesis",
]


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """Evidence a designer turn needs, computed once per round."""

    round_number: int
    records: tuple[RoundRecord, ...]
    carry: CarryOver
    plateau_warning: str | None
    provisional_candidates: int


@dataclass(frozen=True, slots=True)
class NewHypothesis:
    """No hypothesis is active: the strategy must plan a new one."""

    default_parent_round: int | None
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
class RollbackTarget:
    """The workspace commit a requested ``revert_to_round`` resolves to.

    ``commit`` is the tree orchestration should actually check out: usually
    the named round's own commit, but a later failed child hypothesis of that
    round is rolled back to its own pre-hypothesis parent instead, so a
    validated independent repair on top of it is not erased along with the
    failed implementation. ``resolved`` is ``False`` when the named round has
    no recorded commit to roll back to at all.
    """

    commit: str | None
    failed_child_round: int | None
    resolved: bool


@dataclass(frozen=True, slots=True)
class StartedHypothesis:
    """A freshly started hypothesis and its resolved rollback target."""

    state: HypothesisState
    hypothesis: Hypothesis
    rollback: RollbackTarget | None


@dataclass(frozen=True, slots=True)
class ClosedRound:
    """State and next-turn guidance committed after one completed round."""

    state: HypothesisState
    next_active: Hypothesis | None
    carry: CarryOver
    exhaustion_feedback: str | None
