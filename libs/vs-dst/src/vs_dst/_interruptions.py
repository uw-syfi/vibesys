"""Ways a run is interrupted, and which of them a suite tries.

An interruption is one of four actions (the host is killed, the operator pauses the run and the
process exits, the operator stops the run and its host ends it, or the provider's quota holds the
run) at one of three moments: just before a commit reaches the store, just after it, or while a
request's effect is in flight (it has begun, and nothing of its result is sealed or committed).
Which moments a run offers follows the same search depth as its crash points.
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
    STOP = "stop"
    """The operator stops the run (a drain): the host winds down and ends the run, which a Resume
    reopens."""
    QUOTA_PAUSE = "quota pause"
    """The provider's quota holds the run: nothing new starts, and the turns in flight wait in their
    calls. The quota lifts after ``Interruption.wait`` seconds if the host is alive then; a host that
    is given up on first is a host that died in the pause, and the operator's Resume lifts it."""


class Moment(StrEnum):
    """Where in the run the interruption lands."""

    BEFORE_COMMIT = "before commit"
    AFTER_COMMIT = "after commit"
    IN_FLIGHT = "in flight"
    """A request's effect has begun and has no sealed result."""


QUOTA_WAIT_S = 150.0
"""How long a quota pause holds the run before its timed lift: past the time a turn is given
(``WITHIN`` of the turn worlds), so a turn that waits it out has spent its own limit, and within the
patience an interrupted run's host is given."""

QUOTA_OUTLASTS_PATIENCE_S = 10_000.0
"""A quota wait no host outlasts: the operator gives up on the host first, which is a host that
died in the pause."""


@dataclass(frozen=True, repr=False)
class Interruption:
    """An action at a moment: the ``index``-th commit, or the ``index``-th effect to begin."""

    action: Action
    moment: Moment
    index: int
    wait: float = 0.0
    """For ``Action.QUOTA_PAUSE``: the virtual seconds the quota holds the run before it lifts."""

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
        waits = f" for {self.wait:g}s" if self.wait else ""
        return f"{self.kind} {self.index}{waits}"

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
        wait = QUOTA_WAIT_S if action is Action.QUOTA_PAUSE else 0.0
        points.extend(Interruption(action, moment, index, wait) for index in policy.indexes(count))
    return tuple(points)
