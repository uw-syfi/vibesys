"""How the run loop hears the operator: control requests in, core control events out.

``RunControlBridge`` reads the cooperative ``RunControlChannel`` mailbox (the server's socket
and the session's ``RunControl`` write there) and turns what changed since the last poll into
``RunControlEvent``s with stable identities, followed, for a steer, by the session input that
delivers it (``_steer_delivery``).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Literal, Protocol

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    ControlId,
    ControlInput,
    CoreState,
    RunControlEvent,
    RunStatus,
    StopMode,
)
from vs_runtime._steer_delivery import SteerConsumption, steer_input, unrecorded_steers

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreEvent
    from vs_runtime._run_control import RunControlChannel


type ControlAction = Literal["pause", "resume", "stop", "steer"]


class SteerArtifacts(Protocol):
    """Where the text of a steer control lives; the control carries only its digest."""

    def put(self, ref: ArtifactRef, text: str) -> None:
        """Store the text under the reference before the control is submitted."""
        ...


class RunControlBridge:
    """Turns the cooperative ``RunControlChannel`` mailbox into core control events.

    The channel is level-triggered (a pause flag, a stop flag, a queue of steer texts).
    Each ``poll`` emits one event per change since the last poll, with stable
    identities ``control-<n>`` that continue the count of controls core already holds,
    so a resumed run never reuses an identity. A stop carries no result: the search
    adopts its last choice and ends the run itself.
    """

    def __init__(
        self,
        channel: RunControlChannel,
        steer: SteerArtifacts,
    ) -> None:
        """Bind the mailbox and the steer text store."""
        self._channel = channel
        self._steer = steer
        self._count = 0
        self._consumption = SteerConsumption()
        self._started = False
        self._paused = False
        self._stopped = False
        self._landed = False

    def watch_stop(self, listener: Callable[[], object]) -> Callable[[], None]:
        """Call ``listener`` (on the requesting thread) at each stop request; returns unsubscribe.

        Only wakes the loop so it reads the channel now; the stop reaches core through
        ``poll`` like every control.
        """
        return self._channel.on_stop_requested(listener)

    def poll(self, core: CoreState, now_at: float) -> tuple[CoreEvent, ...]:
        """Controls requested since the last poll, in submission order.

        Each steer control is followed by the session input that delivers it to the next
        turn (``_steer_delivery``), and a steer control whose input core never recorded
        gets that input too.
        """
        self._consumption.newly_delivered(core, self._report_consumed)
        if not self._started:
            self._started = True
            self._count = len(core.run.controls)
            self._paused = core.run.status == RunStatus.PAUSED
            self._stopped = any(c.action == "stop" for c in core.run.controls)
        events: list[CoreEvent] = list(unrecorded_steers(core, now_at))
        for text in self._channel.take_pending_steer():
            control = self._event("steer", now_at, text=text)
            events += (control, steer_input(core, control.control, now_at))
        if self._stopped:
            return tuple(events)
        if self._channel.stop_requested():
            self._stopped = True
            events.append(self._event("stop", now_at, mode=self._channel.stop_mode()))
        elif self._channel.pause_requested() != self._paused:
            self._paused = not self._paused
            events.append(self._event("pause" if self._paused else "resume", now_at))
        return tuple(events)

    def settled(self, core: CoreState, *, running: bool) -> None:
        """Report a pause once it has landed: core holds the run paused and nothing runs.

        Core takes no new work while paused, but a request already running (an agent turn,
        a job poll) finishes first; ``running`` says one still is. The pause is reported once
        per stretch of committed ``PAUSED`` status, so the operator sees the run paused when
        it is, not only at an agent turn's end.
        """
        if core.run.status != RunStatus.PAUSED:
            self._landed = False
        elif not running and not self._landed:
            self._landed = True
            self._channel.land_pause()

    def _report_consumed(self, role: str, round_label: str, execution_id: str) -> None:
        self._channel.notify_steer_consumed(
            agent_kind=role, round_label=round_label, execution_id=execution_id
        )

    def _event(
        self,
        action: ControlAction,
        now_at: float,
        *,
        text: str | None = None,
        mode: StopMode | None = None,
    ) -> RunControlEvent:
        self._count += 1
        identity = ControlId(root=f"control-{self._count}")
        artifact = None
        if text is not None:
            artifact = ArtifactRef(
                artifact_id=ArtifactId(root=f"steer-{identity.root}"),
                digest=hashlib.sha256(text.encode()).hexdigest(),
            )
            self._steer.put(artifact, text)
        control = ControlInput(control_id=identity, action=action, mode=mode, artifact=artifact)
        return RunControlEvent(control=control, now_at=now_at)


__all__ = ["RunControlBridge", "SteerArtifacts"]
