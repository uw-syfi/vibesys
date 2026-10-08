"""The paid-attempt budget of one round, shared by the live loop and resume."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PaidMarker(Protocol):
    """Durable proof that an attempt started: the newest one a state recorded."""

    @property
    def round_number(self) -> int:
        """Round the attempt belongs to."""
        ...

    @property
    def member_id(self) -> str:
        """Hypothesis or work item that owns the attempt."""
        ...

    @property
    def turn_number(self) -> int:
        """1-based number of the newest started attempt."""
        ...


@dataclass(frozen=True, slots=True)
class AttemptKey:
    """The unit of work whose attempts are budgeted."""

    round_number: int
    member_id: str


@dataclass(frozen=True, slots=True)
class Implement:
    """Run the implementer for this 1-based attempt number."""

    attempt: int


@dataclass(frozen=True, slots=True)
class CloseRound:
    """Every paid attempt is spent: close the round as failed."""


type NextStep = Implement | CloseRound


def next_step(*, marker: PaidMarker | None, key: AttemptKey, max_attempts: int) -> NextStep:
    """Decide what ``key`` does next from the newest durably paid attempt.

    An attempt is paid when it starts, not when it finishes, so a crash mid-attempt leaves
    it counted and it is never repeated. A marker for a different round or member does
    not count toward ``key``. The live loop and a resumed run both ask this one question,
    so they cannot disagree about whether another attempt is owed.
    """
    paid = (
        marker.turn_number
        if marker is not None
        and marker.round_number == key.round_number
        and marker.member_id == key.member_id
        else 0
    )
    if paid >= max_attempts:
        return CloseRound()
    return Implement(paid + 1)


__all__ = ["AttemptKey", "CloseRound", "Implement", "NextStep", "PaidMarker", "next_step"]
