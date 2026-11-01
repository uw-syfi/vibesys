"""Ways a run is interrupted, and which of them a suite tries.

An interruption is one of two actions (the host is killed, or the operator pauses the run and
the process exits) at one of three moments: just before a commit reaches the store, just after
it, or while a request's effect is in flight (it has begun, and nothing of its result is
sealed or committed). Which moments a run offers follows the same search depth as its crash
points.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_dst._crash_points import CrashKind, CrashPoint

if TYPE_CHECKING:
    from vs_dst._crash_points import CrashPointPolicy


class Action(StrEnum):
    """What interrupts the run."""

    KILL = "kill"
    """The host dies: its memory is gone and nothing it was doing is finished."""
    PAUSE = "pause"
    """The operator pauses the run: the host winds down and exits."""


class Moment(StrEnum):
    """Where in the run the interruption lands."""

    BEFORE_COMMIT = "before commit"
    AFTER_COMMIT = "after commit"
    IN_FLIGHT = "in flight"
    """A request's effect has begun and has no sealed result."""


@dataclass(frozen=True, repr=False)
class Interruption:
    """An action at a moment: the ``index``-th commit, or the ``index``-th effect to begin."""

    action: Action
    moment: Moment
    index: int

    @classmethod
    def kill(cls, point: CrashPoint) -> Interruption:
        """The kill that is ``point``, a host dying at a commit."""
        moment = (
            Moment.BEFORE_COMMIT if point.kind is CrashKind.BEFORE_COMMIT else Moment.AFTER_COMMIT
        )
        return cls(Action.KILL, moment, point.commit)

    @property
    def kind(self) -> str:
        """The class of interruption it is, such as ``kill in flight``: a row of a report."""
        return f"{self.action.value} {self.moment.value}"

    @property
    def crash_point(self) -> CrashPoint | None:
        """The commit this interruption lands on, or None for one that lands in flight."""
        match self.moment:
            case Moment.BEFORE_COMMIT:
                return CrashPoint(CrashKind.BEFORE_COMMIT, self.index)
            case Moment.AFTER_COMMIT:
                return CrashPoint(CrashKind.AFTER_COMMIT, self.index)
            case Moment.IN_FLIGHT:
                return None

    def __str__(self) -> str:
        """Read as a sentence in an assertion message: ``pause in flight 17``."""
        return f"{self.kind} {self.index}"

    __repr__ = __str__


KINDS = tuple((action, moment) for action in Action for moment in Moment)
"""Every class of interruption, in the order a report lists them."""


def choose(policy: CrashPointPolicy, commits: int, effects: int) -> tuple[Interruption, ...]:
    """The interruptions of a run of ``commits`` commits and ``effects`` begun effects.

    Each class is sampled (or taken whole) as the policy says; a run that made none of a
    moment offers none of it.
    """
    points: list[Interruption] = []
    for action, moment in KINDS:
        count = effects if moment is Moment.IN_FLIGHT else commits
        points.extend(Interruption(action, moment, index) for index in policy.indexes(count))
    return tuple(points)
