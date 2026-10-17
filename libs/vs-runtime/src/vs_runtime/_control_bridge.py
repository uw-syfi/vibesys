"""How the run loop hears the operator: control requests in, core control events out.

``RunControlBridge`` reads the cooperative ``RunControlChannel`` mailbox (the server's socket
and the session's ``RunControl`` write there) and the run's durable inbox (``vibesys control``
writes there), and turns what changed since the last poll into ``RunControlEvent``s with stable
identities, followed, for a steer, by the session input that delivers it (``_steer_delivery``).

An inbox request is applied through the same ``ChannelRunControl.submit`` as a socket request,
and core records every control that has a ``control_id``, from either, under the identity
``op-<control id>`` (for an inbox request, also its name in the inbox). The bridge settles an
inbox request only once core holds that identity, so a request is applied exactly once across
a crash, a resume, or a resend through the other ingress: a request core already holds is
settled, one it does not is applied again. A control without an id (a session's
``RunControl`` call) is recorded as ``control-<n>``.
"""

from __future__ import annotations

import functools
import hashlib
import logging
from dataclasses import dataclass
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
from vs_decode.api import Unreadable, decode_json
from vs_project.api import ProjectStateError
from vs_runtime._operator_control import (
    CONTROL_REQUEST,
    ChannelRunControl,
    ControlStatus,
    PauseRequest,
    ResumeRequest,
    SteerRequest,
    StopRequest,
)
from vs_runtime._steer_delivery import SteerConsumption, steer_input, unrecorded_steers

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreEvent
    from vs_project.api import ControlInbox, InboxEntry
    from vs_runtime._operator_control import ControlRequest
    from vs_runtime._run_control import RunControlChannel


type ControlAction = Literal["pause", "resume", "stop", "steer"]


_LOG = logging.getLogger(__name__)

_MAX_REQUEST_BYTES = 1 << 20
"""The largest inbox request the run reads: a steer's text with ample room."""


class SteerArtifacts(Protocol):
    """Where the text of a steer control lives; the control carries only its digest."""

    def put(self, ref: ArtifactRef, text: str) -> None:
        """Store the text under the reference before the control is submitted."""
        ...


class RunControlBridge:
    """Turns the cooperative ``RunControlChannel`` mailbox into core control events.

    The channel is level-triggered (a pause flag, a stop flag, a queue of steer texts).
    Each ``poll`` emits one event per change since the last poll. A change whose control
    had an id is recorded as ``op-<id>``, and one core already holds is not emitted again;
    any other has the identity ``control-<n>``, continuing the count of controls core
    already holds, so a resumed run never reuses an identity. A stop carries no result: the search
    adopts its last choice and ends the run itself.

    With an ``inbox``, each poll also applies the inbox's requests, in id order, each as one
    control ``op-<control id>``; see the module docstring for how each applies once.

    ``control`` (by default the inbox's) is where operators submit controls; each poll tells
    it which ids core already records, so a control resent after a restart replays its ack
    and a different control under a recorded id conflicts, as within one process.
    """

    def __init__(
        self,
        channel: RunControlChannel,
        steer: SteerArtifacts,
        inbox: OperatorInbox | None = None,
        *,
        control: ChannelRunControl | None = None,
    ) -> None:
        """Bind the mailbox, the steer text store, a headless run's inbox, operators' controls."""
        self._channel = channel
        self._steer = steer
        self._inbox = inbox
        self._control = control if control is not None or inbox is None else inbox.control
        self._submitted: set[str] = set()
        self._warned_damaged = False
        self._count = 0
        self._position = 0
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
        self._position = len(core.run.controls)
        if self._control is not None:
            self._control.recall(functools.partial(_held_as, core))
        events: list[CoreEvent] = list(unrecorded_steers(core, now_at))
        self._take_steers(core, now_at, events)
        if self._inbox is not None:
            # A damaged inbox (a symlinked or misplaced directory) leaves its requests waiting
            # and the run going on; reporting it to the operator is tracked in #3061.
            try:
                self._read_inbox(self._inbox, core, now_at, events)
            except ProjectStateError as error:
                if not self._warned_damaged:
                    self._warned_damaged = True
                    _LOG.warning(
                        "the run's control inbox cannot be read; its requests wait: %s", error
                    )
        if self._stopped:
            return tuple(events)
        if self._channel.stop_requested():
            self._stopped = True
            by = self._channel.requested_by("stop")
            stop = self._event(
                core, now_at, _Change("stop", mode=self._channel.stop_mode(), control_id=by)
            )
            events += () if stop is None else (stop,)
        elif self._channel.pause_requested() != self._paused:
            self._paused = not self._paused
            action = "pause" if self._paused else "resume"
            by = self._channel.requested_by("pause")
            change = self._event(core, now_at, _Change(action, control_id=by))
            events += () if change is None else (change,)
        return tuple(events)

    def _take_steers(self, core: CoreState, now_at: float, events: list[CoreEvent]) -> None:
        """Emit the queued steers, each under its control's id if it had one."""
        for steer in self._channel.take_pending_steer():
            control = self._event(
                core, now_at, _Change("steer", text=steer.text, control_id=steer.control_id)
            )
            if control is not None:
                events += (control, steer_input(core, control.control, now_at, self._position))

    def _read_inbox(
        self, inbox: OperatorInbox, core: CoreState, now_at: float, events: list[CoreEvent]
    ) -> None:
        """Settle the requests core holds and apply, once per process, the ones it does not.

        Raises ``ProjectStateError`` when the inbox itself cannot be read or marked.
        """
        held = {control.control_id.root: control for control in core.run.controls}
        for entry in inbox.requests.pending():
            if entry.unreadable is not None:
                inbox.requests.reject(entry.request_id, f"unreadable: {entry.unreadable}")
                continue
            control = held.get(_identity(entry.request_id).root)
            if control is not None:
                if _is_held(entry, control):
                    inbox.requests.settle(entry.request_id)
                else:
                    reason = "its control_id already named a different control"
                    inbox.requests.reject(entry.request_id, reason)
            elif entry.request_id not in self._submitted:
                reason = self._apply(inbox, entry, core, now_at, events)
                if reason is None:
                    self._submitted.add(entry.request_id)
                else:
                    inbox.requests.reject(entry.request_id, reason)

    def _apply(
        self,
        inbox: OperatorInbox,
        entry: InboxEntry,
        core: CoreState,
        now_at: float,
        events: list[CoreEvent],
    ) -> str | None:
        """Apply one inbox request; the reason it cannot apply, or None once it is submitted."""
        request = self._submitted_request(inbox, entry, core)
        if isinstance(request, str):
            return request
        identity = request.control_id
        match request:
            case ResumeRequest(when="capacity_returns"):
                # The channel holds the wait and core records nothing: the request is done.
                inbox.requests.settle(identity)
            case PauseRequest():
                self._paused = True
                change = self._event(core, now_at, _Change("pause", control_id=identity))
                events += () if change is None else (change,)
            case ResumeRequest():
                self._paused = False
                change = self._event(core, now_at, _Change("resume", control_id=identity))
                events += () if change is None else (change,)
            case StopRequest(mode=mode):
                self._stopped = True
                stop = self._event(core, now_at, _Change("stop", mode=mode, control_id=identity))
                events += () if stop is None else (stop,)
            case SteerRequest():
                # Applied now, or queued earlier through the socket under the same id.
                self._take_steers(core, now_at, events)
        return None

    def _submitted_request(
        self, inbox: OperatorInbox, entry: InboxEntry, core: CoreState
    ) -> ControlRequest | str:
        """Parse and submit one inbox request; the reason it cannot apply, if it cannot."""
        request = _parsed(entry)
        if isinstance(request, Unreadable):
            return f"not a control request: {request.details()}"
        if request.control_id != entry.request_id:
            return f"its control_id {request.control_id!r} is not its name in the inbox"
        refusal = _refusal(request, core, stopped=self._stopped)
        if refusal is not None:
            return refusal
        ack = inbox.control.submit(request)
        if ack.status is ControlStatus.CONFLICT:
            return "its control_id already named a different control"
        if ack.reason is not None:
            return f"refused: {ack.reason.value}"
        return request

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

    def _event(self, core: CoreState, now_at: float, change: _Change) -> RunControlEvent | None:
        """The event recording one control, or None when core already holds that control.

        A control with an id is recorded as ``op-<id>``. Each poll tells the operators'
        controls which ids core records, so a reused id is refused at submission; only one
        submitted after a restart and before the first poll can reach here under an id core
        holds for a different control. It is recorded as ``control-<n>``, with a warning, so
        it applies and never conflicts with the record.
        """
        if change.control_id is not None:
            control = change.control(_identity(change.control_id))
            held = next((c for c in core.run.controls if c.control_id == control.control_id), None)
            if held == control:
                return None
            if held is None:
                return self._recorded(control, change.text, now_at)
            _LOG.warning(
                "control %s reuses an id the run recorded for another control; recorded as a new control",
                change.control_id,
            )
        self._count += 1
        control = change.control(ControlId(root=f"control-{self._count}"))
        return self._recorded(control, change.text, now_at)

    def _recorded(self, control: ControlInput, text: str | None, now_at: float) -> RunControlEvent:
        self._position += 1
        if text is not None and control.artifact is not None:
            self._steer.put(control.artifact, text)
        return RunControlEvent(control=control, now_at=now_at)


@dataclass(frozen=True)
class OperatorInbox:
    """A run's durable control inbox and the run's controls it is applied through."""

    requests: ControlInbox
    control: ChannelRunControl


def _identity(control_id: str) -> ControlId:
    """The identity core records a control with an id under."""
    return ControlId(root=f"op-{control_id}")


@dataclass(frozen=True)
class _Change:
    """One control to record: its action, a steer's text, a stop's mode, the sender's id."""

    action: ControlAction
    text: str | None = None
    mode: StopMode | None = None
    control_id: str | None = None

    def control(self, identity: ControlId) -> ControlInput:
        """The control core records; a steer carries its text's digest, not the text."""
        artifact = None
        if self.text is not None:
            artifact = ArtifactRef(
                artifact_id=ArtifactId(root=f"steer-{identity.root}"),
                digest=hashlib.sha256(self.text.encode()).hexdigest(),
            )
        return ControlInput(
            control_id=identity, action=self.action, mode=self.mode, artifact=artifact
        )


def _is_held(entry: InboxEntry, control: ControlInput) -> bool:
    """Whether ``control``, which core holds under the entry's identity, is the entry's request."""
    request = _parsed(entry)
    return not isinstance(request, Unreadable) and _records(control, request)


def _parsed(entry: InboxEntry) -> ControlRequest | Unreadable:
    """The request an inbox entry holds, or why it holds none; another process wrote it."""
    return decode_json(
        entry.payload,
        CONTROL_REQUEST,
        source=f"control inbox entry {entry.request_id}",
        max_bytes=_MAX_REQUEST_BYTES,
    )


def _held_as(core: CoreState, request: ControlRequest) -> bool | None:
    """Whether core holds ``request`` under its id: None if nothing, False if another control."""
    identity = _identity(request.control_id)
    control = next((c for c in core.run.controls if c.control_id == identity), None)
    return None if control is None else _records(control, request)


def _records(control: ControlInput, request: ControlRequest) -> bool:
    """Whether ``control`` records ``request``.

    Core records the action, a stop's mode and the digest of a steer's text; a request that
    differs in any of them reuses the id for a different control.
    """
    match request:
        case SteerRequest(text=text):
            digest = hashlib.sha256(text.encode()).hexdigest()
            return control.artifact is not None and control.artifact.digest == digest
        case StopRequest(mode=mode):
            return control.action == "stop" and control.mode == mode
        case _:
            return control.action == request.action.value


def _refusal(request: ControlRequest, core: CoreState, *, stopped: bool) -> str | None:
    """Why core would refuse ``request`` now, or None; refused requests never reach core."""
    if core.run.status == RunStatus.TERMINAL:
        return "the run has ended"
    if not isinstance(request, (PauseRequest, ResumeRequest)):
        return None
    if stopped:
        return "the run is stopping; it cannot pause or resume"
    if core.run.status in (RunStatus.CLOSING, RunStatus.BLOCKED) or core.run.result is not None:
        return "the run is finishing; it cannot pause or resume"
    return None


__all__ = ["OperatorInbox", "RunControlBridge", "SteerArtifacts"]
