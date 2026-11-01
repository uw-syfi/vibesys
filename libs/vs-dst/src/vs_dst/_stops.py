"""Ways a run is stopped, and the one moment of a run at which a stop is delivered.

A stop is a soft control: the search is told, winds its flows down and ends the run, which is
how the operator and the host stop a live run. Four kinds reach core, and they differ in what
they ask for (a drain lets the work in flight finish, a cancel does not) and in how the run
ends. The moments are the ones an interruption lands on (`Moment`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_core.api import (
    DEADLINE_STOP,
    ENVIRONMENT_STOP,
    ControlId,
    ControlInput,
    RunControlEvent,
    StopMode,
)
from vs_dst._crash_points import EVERY_POINT, CrashPointPolicy
from vs_dst._interruptions import Moment
from vs_dst._step_host import HostNotOpenError
from vs_sim.api.testing import Depth, depth

if TYPE_CHECKING:
    from vs_core.api import CoreState
    from vs_dst._snapshots import RunningHost, Store
    from vs_proto.api import Record

FIRST_EPOCH = 1
"""The epoch of the first host, which the host's own stops are named for."""

STOP_SAMPLE_SIZE = 4
"""Points tried per moment when a suite samples: a stop is tried by four kinds in every world,
and each stop is a whole run, so a pull request tries fewer places than an interruption does."""


class StopKind(StrEnum):
    """Who stops the run, and what the stop asks for."""

    OPERATOR_CANCEL = "operator cancel"
    OPERATOR_DRAIN = "operator drain"
    DEADLINE = "deadline"
    """The run's deadline passed: the host stops it, draining."""
    ENVIRONMENT = "environment"
    """The run environment can run none of the run's work: the host stops it, cancelling, and
    the run ends failed."""

    @property
    def mode(self) -> StopMode:
        """What the stop asks of the work in flight."""
        match self:
            case StopKind.OPERATOR_CANCEL | StopKind.ENVIRONMENT:
                return StopMode.CANCEL
            case StopKind.OPERATOR_DRAIN | StopKind.DEADLINE:
                return StopMode.DRAIN

    @property
    def control(self) -> ControlInput:
        """The control the host or the operator delivers, under the id core names that stop by."""
        match self:
            case StopKind.OPERATOR_CANCEL | StopKind.OPERATOR_DRAIN:
                identity = f"operator-stop-{self.mode.value}"
            case StopKind.DEADLINE:
                identity = f"{DEADLINE_STOP}:{FIRST_EPOCH}"
            case StopKind.ENVIRONMENT:
                identity = f"{ENVIRONMENT_STOP}:{FIRST_EPOCH}"
        return ControlInput(control_id=ControlId(root=identity), action="stop", mode=self.mode)


@dataclass(frozen=True, repr=False)
class StopPoint:
    """The ``index``-th commit's moment, or the ``index``-th effect to begin: where a stop lands."""

    moment: Moment
    index: int

    def __str__(self) -> str:
        """Read as a sentence in an assertion message: ``before commit 17``."""
        return f"{self.moment.value} {self.index}"

    __repr__ = __str__


def stop_points(commits: int, effects: int) -> tuple[StopPoint, ...]:
    """The places in a run of ``commits`` commits and ``effects`` begun effects a suite stops it.

    A sample on pull requests (`STOP_SAMPLE_SIZE` per moment, the first and the last included),
    every point under ``--depth=nightly``.
    """
    policy = EVERY_POINT if depth() is Depth.NIGHTLY else CrashPointPolicy(STOP_SAMPLE_SIZE)
    return tuple(
        StopPoint(moment, index)
        for moment in Moment
        for index in policy.indexes(effects if moment is Moment.IN_FLIGHT else commits)
    )


@dataclass(frozen=True)
class Stopped:
    """A run that was stopped at one point, and how it ended up."""

    point: StopPoint
    kind: StopKind
    delivered_at: float | None
    """The virtual time the stop reached the run; None when the run ended before the point."""
    ended_at: float
    """The virtual time the host that was stopped returned."""
    ended: bool
    """Whether the run reached its end: terminal, its end recorded."""
    core: CoreState | None
    halted: Exception | None
    """Why the loop gave up on the run (it waits on nothing, or started too many steps), or None."""
    failure: Exception | None
    """What the host failed with, when it did."""
    log_records: tuple[Record, ...]

    @property
    def took(self) -> float | None:
        """How long the run took to end after the stop reached it, in virtual seconds."""
        return None if self.delivered_at is None else self.ended_at - self.delivered_at

    @property
    def why_not_ended(self) -> str:
        """What the run is stuck on, in words; empty when it ended."""
        if self.ended:
            return ""
        if self.failure is not None:
            return f"{type(self.failure).__name__}: {self.failure}"
        if self.halted is not None:
            return f"{type(self.halted).__name__}: {self.halted}"
        status = "no state" if self.core is None else self.core.run.status.value
        return f"run {status} when the host gave up on it"


class Stopper:
    """Stops the run the way the operator or the host does, when the run reaches one point.

    A stop that is due before the host has opened its log waits for the first moment after: the
    operator's request is held until the process can take it.
    """

    def __init__(self, point: StopPoint, kind: StopKind) -> None:
        """Deliver ``kind`` when the run reaches ``point``."""
        self._point = point
        self._kind = kind
        self._host: RunningHost | None = None
        self._due = False
        self.delivered_at: float | None = None

    def watch(self, store: Store, host: RunningHost) -> None:
        """Start hearing ``store``'s commits, for the run ``host`` drives."""
        self._host = host
        store.watcher = self

    def reached(self, moment: Moment, index: int) -> None:
        """Submit the stop once the run is at the point, and as soon as the host can take it."""
        if self._host is None or self.delivered_at is not None:
            return
        self._due = self._due or (moment, index) == (self._point.moment, self._point.index)
        if not self._due:
            return
        now = self._host.clock.now()
        try:
            self._host.submit(RunControlEvent(control=self._kind.control, now_at=now))
        except HostNotOpenError:
            return
        self.delivered_at = now
