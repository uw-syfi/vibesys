"""The paid-attempt budget of one round, shared by the live loop and resume."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Implement:
    """Run the implementer for this 1-based attempt number."""

    attempt: int


@dataclass(frozen=True, slots=True)
class CloseRound:
    """Every paid attempt is spent: close the round as failed."""


type NextStep = Implement | CloseRound


def next_step(*, last_paid: int | None, max_attempts: int) -> NextStep:
    """Decide what a round does next from its last durably paid attempt.

    ``last_paid`` is the newest attempt number whose start was committed for this round
    and hypothesis, or ``None`` if none was. An attempt is paid when it starts, not when it
    finishes, so a crash mid-attempt leaves it counted and it is never repeated. The live
    loop and a resumed run both ask this one question, so they cannot disagree about
    whether another attempt is owed.
    """
    upcoming = 1 if last_paid is None else last_paid + 1
    if upcoming > max_attempts:
        return CloseRound()
    return Implement(upcoming)


__all__ = ["CloseRound", "Implement", "NextStep", "next_step"]
