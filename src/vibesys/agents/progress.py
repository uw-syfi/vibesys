"""Small display contract for agent progress shown in live CLI prefixes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AgentProgress(Protocol):
    """Loop-owned progress state that can render itself for terminal output."""

    def label(self) -> str:
        """Return the compact progress label for the live agent prefix."""
        ...


@dataclass(frozen=True)
class RoundProgress:
    """Progress for loops that advance one round at a time."""

    round_number: int
    total_rounds: int

    def label(self) -> str:
        return f"Round {self.round_number}/{self.total_rounds}"


@dataclass(frozen=True)
class CandidateProgress:
    """Progress for loops with multiple candidates inside each round."""

    round_number: int
    total_rounds: int
    candidate_number: int
    total_candidates: int

    def label(self) -> str:
        return (
            f"Round {self.round_number}/{self.total_rounds} "
            f"Cand {self.candidate_number}/{self.total_candidates}"
        )
