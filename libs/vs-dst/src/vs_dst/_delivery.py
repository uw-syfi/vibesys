"""The one way an operator's or the host's control reaches a running host at a moment of its run.

A pause and a stop are controls sent to the host while it is alive, when its run reaches a
moment (`Moment`): just before a commit, just after one, or as an effect begins. Whoever sends one
(an interruption of a run, or a stop sweep) sends it through `Delivery`, so the two cannot differ
in when a control is taken or what it is named. Each goes where its sender's goes in production:
the operator's pause and stop and the provider's quota pause to the control channel, which the
host's `RunControlBridge` turns into the controls core records, and the deadline's stop to core.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_core.api import RunControlEvent
from vs_dst._interruptions import Action, Interruption, Moment
from vs_dst._step_host import HostNotOpenError
from vs_dst._stops import StopKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_dst._snapshots import RunningHost, Store
    from vs_runtime.api.infrastructure import ControlRequest


def _serial(point: Interruption) -> str:
    """The name of the control sent at ``point``.

    Each point is its own control: a second one, on a resumed host, must not be taken for the
    first, which the log still holds.
    """
    return f"{point.moment.name.lower()}-{point.index}"


def _operator_sends(point: Interruption) -> Callable[[RunningHost], None]:
    """What the operator, or the provider's quota, sends the host at ``point``."""
    serial = _serial(point)
    match point.action:
        case Action.STOP:
            return _stops(StopKind.OPERATOR_DRAIN, serial)
        case Action.PAUSE:
            return lambda host: host.pause(f"operator-pause-{serial}")
        case Action.QUOTA_PAUSE:
            return lambda host: host.quota_pause(point.wait)
        case Action.KILL:
            message = "a kill is no control the operator sends to a live host"
            raise ValueError(message)


def _stops(kind: StopKind, serial: str = "") -> Callable[[RunningHost], None]:
    """What the operator or the host sends to stop the run as ``kind`` says."""
    identity = kind.control(serial).control_id.root
    match kind:
        case StopKind.OPERATOR_CANCEL | StopKind.OPERATOR_DRAIN:
            return lambda host: host.stop(kind.mode, identity)
        case StopKind.ENVIRONMENT:
            return lambda host: host.environment_stop()
        case StopKind.DEADLINE:
            return lambda host: host.submit(
                RunControlEvent(control=kind.control(serial), now_at=host.clock.now())
            )


@dataclass(frozen=True)
class OperatorRequest:
    """A request the operator posts to the run's control inbox when the run reaches a point."""

    moment: Moment
    index: int
    """The ``index``-th commit's moment, or the ``index``-th effect to begin."""
    request: ControlRequest


class Delivery:
    """Sends one control to the host when its run reaches a moment, and as soon as the host can.

    A control that is due before the host has opened its log waits for the first moment after: the
    operator's request is held until the process can take it, as a request to a process that is
    still starting is. It is delivered once.
    """

    def __init__(
        self,
        moment: Moment,
        index: int,
        send: Callable[[RunningHost], None],
        *,
        stops: bool = False,
    ) -> None:
        """Send once the run reaches the ``index``-th commit or effect of ``moment``.

        ``stops`` says what is sent is a stop, which ends the run once it reaches the host.
        """
        self._at = (moment, index)
        self._send = send
        self._stops = stops
        self._host: RunningHost | None = None
        self._due = False
        self.delivered_at: float | None = None
        """The virtual time the control reached the host; None until then."""

    @classmethod
    def request(cls, moment: Moment, index: int, request: ControlRequest) -> Delivery:
        """The operator's ``request``, posted to the run's control inbox at ``moment`` ``index``."""
        return cls(moment, index, lambda host: host.post(request))

    @classmethod
    def stop(cls, moment: Moment, index: int, kind: StopKind) -> Delivery:
        """The stop of ``kind`` at ``moment`` ``index``."""
        return cls(moment, index, _stops(kind), stops=True)

    @classmethod
    def operator(cls, point: Interruption) -> Delivery:
        """The operator's pause or stop, or the provider's quota pause, at ``point``."""
        return cls(
            point.moment, point.index, _operator_sends(point), stops=point.action is Action.STOP
        )

    @property
    def stopped(self) -> bool:
        """Whether a stop has reached the host, which therefore ends the run."""
        return self.delivered_at is not None and self._stops

    def watch(self, store: Store, host: RunningHost) -> None:
        """Start hearing ``store``'s commits, for the run ``host`` drives."""
        self._host = host
        store.watcher = self

    def reached(self, moment: Moment, index: int) -> None:
        """Send the control once the run is at the moment, and as soon as the host can take it."""
        if self._host is None or self.delivered_at is not None:
            return
        self._due = self._due or (moment, index) == self._at
        if not self._due:
            return
        now = self._host.clock.now()
        try:
            self._send(self._host)
        except HostNotOpenError:
            return
        self.delivered_at = now
