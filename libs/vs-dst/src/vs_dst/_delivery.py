"""The one way an operator's or the host's control reaches a running host at a moment of its run.

A pause and a stop are controls submitted to the host while it is alive, when its run reaches a
moment (`Moment`): just before a commit, just after one, or as an effect begins. Whoever sends one
(an interruption of a run, or a stop sweep) sends it through `Delivery`, so the two cannot differ
in when a control is taken or what it is named.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import ControlId, ControlInput, PauseOwner, RunControlEvent
from vs_dst._interruptions import Action, Interruption, Moment
from vs_dst._step_host import HostNotOpenError
from vs_dst._stops import StopKind

if TYPE_CHECKING:
    from vs_dst._snapshots import RunningHost, Store


def operator_control(point: Interruption) -> ControlInput:
    """The control the operator sends for a pause or a stop at ``point``.

    Each point is its own control: a second one, on a resumed host, must not be taken for the
    first, which the log still holds.
    """
    serial = f"{point.moment.name.lower()}-{point.index}"
    match point.action:
        case Action.STOP:
            return StopKind.OPERATOR_DRAIN.control(serial)
        case Action.PAUSE:
            return ControlInput(
                control_id=ControlId(root=f"operator-pause-{serial}"),
                action="pause",
                owner=PauseOwner.OPERATOR,
            )
        case Action.QUOTA_PAUSE:
            return ControlInput(
                control_id=ControlId(root=f"quota-pause-{serial}"),
                action="pause",
                owner=PauseOwner.QUOTA,
            )
        case Action.KILL:
            message = "a kill is no control the operator sends to a live host"
            raise ValueError(message)


def quota_lift(point: Interruption) -> ControlInput:
    """The resume the quota's timed lift sends once ``point``'s wait is over."""
    serial = f"{point.moment.name.lower()}-{point.index}"
    return ControlInput(
        control_id=ControlId(root=f"quota-lift-{serial}"), action="resume", when="now"
    )


class Delivery:
    """Submits one control to the host when its run reaches a moment, and as soon as the host can.

    A control that is due before the host has opened its log waits for the first moment after: the
    operator's request is held until the process can take it, as a request to a process that is
    still starting is. It is delivered once.
    """

    def __init__(
        self,
        moment: Moment,
        index: int,
        control: ControlInput,
        lift: tuple[float, ControlInput] | None = None,
    ) -> None:
        """Deliver ``control`` once the run reaches the ``index``-th commit or effect of ``moment``.

        ``lift`` is a control the host takes that many seconds after ``control``, if it is still
        running then: a quota's timed resume.
        """
        self._at = (moment, index)
        self._control = control
        self._lift = lift
        self._host: RunningHost | None = None
        self._due = False
        self.delivered_at: float | None = None
        """The virtual time the control reached the host; None until then."""

    @classmethod
    def stop(cls, moment: Moment, index: int, kind: StopKind) -> Delivery:
        """The stop of ``kind`` at ``moment`` ``index``."""
        return cls(moment, index, kind.control())

    @classmethod
    def operator(cls, point: Interruption) -> Delivery:
        """The operator's pause or stop, or the provider's quota pause with its timed lift, at ``point``."""
        lift = (point.wait, quota_lift(point)) if point.action is Action.QUOTA_PAUSE else None
        return cls(point.moment, point.index, operator_control(point), lift)

    @property
    def stopped(self) -> bool:
        """Whether a stop has reached the host, which therefore ends the run."""
        return self.delivered_at is not None and self._control.action == "stop"

    def watch(self, store: Store, host: RunningHost) -> None:
        """Start hearing ``store``'s commits, for the run ``host`` drives."""
        self._host = host
        store.watcher = self

    def reached(self, moment: Moment, index: int) -> None:
        """Submit the control once the run is at the moment, and as soon as the host can take it."""
        if self._host is None or self.delivered_at is not None:
            return
        self._due = self._due or (moment, index) == self._at
        if not self._due:
            return
        now = self._host.clock.now()
        try:
            self._host.submit(RunControlEvent(control=self._control, now_at=now))
        except HostNotOpenError:
            return
        self.delivered_at = now
        if self._lift is not None:
            wait, lift = self._lift
            self._host.hold_turns(wait)
            self._host.submit_after(wait, lambda at: RunControlEvent(control=lift, now_at=at))
