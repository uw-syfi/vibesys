"""Operator control requests: the one schema every control ingress accepts.

An operator (a person, or an orchestrating agent) changes a running run by sending a
``ControlRequest``. The server's socket and the headless run's inbox both parse their input
into this one schema and hand it to ``ChannelRunControl.submit``, so every ingress accepts the
same requests and applies them the same way. The schema forbids unknown keys, so a request
that names an option this run does not offer is refused where it is parsed, with the key
named.

Every request carries a ``control_id`` its sender chooses. Submitting the same id again with
the same request returns the first answer and changes nothing, so a sender that is unsure a
request arrived may send it again; the same id with a different request is a conflict.

The schema has no field that reaches measurement or acceptance: no evaluation command, checker,
trust rule or resource partition. Steering changes what agents are told and what work runs, never
how a candidate is judged; ``tests/test_operator_control.py`` holds every field to an allow-list
with ``control_schema_fields`` (``_control_schema.py``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from vs_core.api import StopMode

if TYPE_CHECKING:
    from vs_runtime._run_control import RunControlChannel
    from vs_sim.api import Threads

CONTROL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
"""One to 128 letters, digits, ``.``, ``_`` and ``-``, starting with a letter or digit."""


class ControlAction(StrEnum):
    """What a control request asks for; the tag of the ``ControlRequest`` union."""

    STEER = "steer"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    control_id: str = Field(pattern=CONTROL_ID_PATTERN)
    """The sender's id for this request; resending it is safe."""


class SteerRequest(_Request):
    """Free-text guidance for the run's next agent turn."""

    action: Literal[ControlAction.STEER] = ControlAction.STEER
    text: str = Field(min_length=1)


class PauseRequest(_Request):
    """Reach the next safe boundary and hold the run there."""

    action: Literal[ControlAction.PAUSE] = ControlAction.PAUSE


ResumeWhen = Literal["now", "capacity_returns"]
"""Resume at once, or once provider capacity should be back."""


def check_one_way_to_resume(*, fallback: bool, when: ResumeWhen) -> None:
    """Raise ``ValueError`` if a resume both switches to the fallback and waits for capacity.

    The one definition of the rule; every model with these two fields validates with it.
    """
    if fallback and when == "capacity_returns":
        message = "fallback resumes now; it cannot wait for capacity to return"
        raise ValueError(message)


class ResumeRequest(_Request):
    """Continue a paused run.

    ``fallback`` switches a run paused on a provider capacity limit to its configured fallback
    provider as it resumes. ``when="capacity_returns"`` keeps such a run paused until capacity
    should be back, then resumes it; it is refused when the run is not paused on a capacity
    limit, and cannot be combined with ``fallback``, which resumes now.
    """

    action: Literal[ControlAction.RESUME] = ControlAction.RESUME
    fallback: bool = False
    when: ResumeWhen = "now"

    @model_validator(mode="after")
    def _one_way_to_resume(self) -> ResumeRequest:
        check_one_way_to_resume(fallback=self.fallback, when=self.when)
        return self


class StopRequest(_Request):
    """End the run, draining or cancelling the work in flight."""

    action: Literal[ControlAction.STOP] = ControlAction.STOP
    mode: StopMode = StopMode.DRAIN


type ControlRequest = Annotated[
    SteerRequest | PauseRequest | ResumeRequest | StopRequest, Field(discriminator="action")
]
"""One operator control, discriminated by ``action``."""

CONTROL_REQUEST: TypeAdapter[ControlRequest] = TypeAdapter(ControlRequest)
"""Parses and validates a ``ControlRequest`` from Python data or JSON text."""


class ControlStatus(StrEnum):
    """What became of a submitted control at the moment it was submitted."""

    PENDING = "pending"
    """Recorded; the run applies it at its next boundary."""
    CONSUMED = "consumed"
    """Already applied."""
    REFUSED = "refused"
    """Not applicable to the run as it stands (``ControlAck.reason`` says why); nothing changed."""
    CONFLICT = "conflict"
    """Its ``control_id`` already named a different request; nothing changed."""


class RefusalReason(StrEnum):
    """Why a control was refused."""

    NOT_PAUSED_ON_CAPACITY = "not_paused_on_capacity"
    """A wait for capacity, for a run not paused on a provider capacity limit."""


class ControlAck(_Request):
    """The answer to one submitted control: ``reason`` is set exactly when it was refused."""

    action: ControlAction
    status: ControlStatus
    reason: RefusalReason | None = None

    @model_validator(mode="after")
    def _reason_iff_refused(self) -> ControlAck:
        if (self.reason is None) == (self.status is ControlStatus.REFUSED):
            message = "a refused control says why, and only a refused one"
            raise ValueError(message)
        return self


class ChannelRunControl:
    """The run's controls over its cooperative ``RunControlChannel``.

    ``submit`` is the one entry point every ingress calls. The per-action methods are what it
    applies, kept for callers that hold a single action.
    """

    def __init__(self, channel: RunControlChannel, threads: Threads) -> None:
        """Write every control to ``channel``, the mailbox the run loop reads."""
        self._channel = channel
        self._lock = threads.lock()
        self._answered: dict[str, tuple[ControlRequest, ControlAck]] = {}

    def submit(self, request: ControlRequest) -> ControlAck:
        """Apply one control request once per ``control_id`` and say what became of it."""
        with self._lock:
            answered = self._answered.get(request.control_id)
            if answered is not None:
                first, ack = answered
                if first == request:
                    return ack
                return self._ack(request, ControlStatus.CONFLICT)
            ack = self._apply(request)
            self._answered[request.control_id] = (request, ack)
            return ack

    def _apply(self, request: ControlRequest) -> ControlAck:
        reason = None
        match request:
            case SteerRequest(text=text):
                self.steer(text)
                status = ControlStatus.PENDING
            case PauseRequest():
                self.pause()
                status = ControlStatus.PENDING
            case ResumeRequest(when="capacity_returns"):
                status = ControlStatus.PENDING
                if not self.resume_when_capacity_returns():
                    status, reason = ControlStatus.REFUSED, RefusalReason.NOT_PAUSED_ON_CAPACITY
            case ResumeRequest(fallback=True):
                self.resume_with_fallback()
                status = ControlStatus.CONSUMED
            case ResumeRequest():
                self.resume()
                status = ControlStatus.CONSUMED
            case StopRequest(mode=mode):
                self.stop(mode)
                status = ControlStatus.PENDING
        return self._ack(request, status, reason)

    @staticmethod
    def _ack(
        request: ControlRequest, status: ControlStatus, reason: RefusalReason | None = None
    ) -> ControlAck:
        return ControlAck(
            control_id=request.control_id, action=request.action, status=status, reason=reason
        )

    def steer(self, text: str) -> None:
        """Queue free-text steering for the next turn."""
        self._channel.queue_steer(text)

    def pause(self) -> None:
        """Request a pause at the next safe boundary."""
        self._channel.request_pause()

    def resume(self) -> None:
        """Release a paused run, cancelling a pending pause or stop."""
        self._channel.resume()

    def resume_with_fallback(self) -> None:
        """Resume, switching a run paused on a provider capacity limit to its fallback."""
        self._channel.resume_with_fallback()

    def resume_when_capacity_returns(self) -> bool:
        """Resume a run paused on a capacity limit once capacity returns; False if not so paused."""
        return self._channel.resume_when_capacity_returns()

    def stop(self, mode: StopMode = StopMode.DRAIN) -> None:
        """Request the run end; draining is the default at this edge."""
        self._channel.request_stop(mode)


__all__ = [
    "CONTROL_ID_PATTERN",
    "CONTROL_REQUEST",
    "ChannelRunControl",
    "ControlAck",
    "ControlAction",
    "ControlRequest",
    "ControlStatus",
    "PauseRequest",
    "RefusalReason",
    "ResumeRequest",
    "ResumeWhen",
    "SteerRequest",
    "StopMode",
    "StopRequest",
    "check_one_way_to_resume",
]
