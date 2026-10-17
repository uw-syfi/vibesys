"""Operator control requests: the one schema every control ingress accepts.

An operator (a person, or an orchestrating agent) changes a running run by sending a
``ControlRequest``. The server's socket and the headless run's inbox both parse their input
into this one schema and hand it to ``ChannelRunControl.submit``, so every ingress accepts the
same requests and applies them the same way. The schema forbids unknown keys, so a request
that names an option this run does not offer is refused where it is parsed, with the key
named.

The schema has no field that reaches measurement or acceptance: no evaluation command, checker,
trust rule or resource partition. Steering changes what agents are told and what work runs, never
how a candidate is judged; ``tests/test_operator_control.py`` holds every field to that.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from vs_core.api import StopMode

if TYPE_CHECKING:
    from vs_runtime._run_control import RunControlChannel


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SteerRequest(_Request):
    """Free-text guidance for the run's next agent turn."""

    action: Literal["steer"] = "steer"
    text: str = Field(min_length=1)


class PauseRequest(_Request):
    """Reach the next safe boundary and hold the run there."""

    action: Literal["pause"] = "pause"


class ResumeRequest(_Request):
    """Continue a paused run.

    ``fallback`` switches a run paused on a provider capacity limit to its configured fallback
    provider as it resumes. ``when="capacity_returns"`` keeps such a run paused until capacity
    should be back, then resumes it; it is refused when the run is not paused on a capacity
    limit, and cannot be combined with ``fallback``, which resumes now.
    """

    action: Literal["resume"] = "resume"
    fallback: bool = False
    when: Literal["now", "capacity_returns"] = "now"

    @model_validator(mode="after")
    def _one_way_to_resume(self) -> ResumeRequest:
        if self.fallback and self.when == "capacity_returns":
            message = "fallback resumes now; it cannot wait for capacity to return"
            raise ValueError(message)
        return self


class StopRequest(_Request):
    """End the run, draining or cancelling the work in flight."""

    action: Literal["stop"] = "stop"
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
    """Not applicable to the run as it stands; nothing changed."""


class ControlAck(_Request):
    """The answer to one submitted control."""

    action: Literal["steer", "pause", "resume", "stop"]
    status: ControlStatus


class ChannelRunControl:
    """The run's controls over its cooperative ``RunControlChannel``.

    ``submit`` is the one entry point every ingress calls. The per-action methods are what it
    applies, kept for callers that hold a single action.
    """

    def __init__(self, channel: RunControlChannel) -> None:
        """Write every control to ``channel``, the mailbox the run loop reads."""
        self._channel = channel

    def submit(self, request: ControlRequest) -> ControlAck:
        """Apply one control request and say what became of it."""
        match request:
            case SteerRequest(text=text):
                self.steer(text)
                return ControlAck(action="steer", status=ControlStatus.PENDING)
            case PauseRequest():
                self.pause()
                return ControlAck(action="pause", status=ControlStatus.PENDING)
            case ResumeRequest(when="capacity_returns"):
                armed = self.resume_when_capacity_returns()
                status = ControlStatus.PENDING if armed else ControlStatus.REFUSED
                return ControlAck(action="resume", status=status)
            case ResumeRequest(fallback=True):
                self.resume_with_fallback()
                return ControlAck(action="resume", status=ControlStatus.CONSUMED)
            case ResumeRequest():
                self.resume()
                return ControlAck(action="resume", status=ControlStatus.CONSUMED)
            case StopRequest(mode=mode):
                self.stop(mode)
                return ControlAck(action="stop", status=ControlStatus.PENDING)

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
    "CONTROL_REQUEST",
    "ChannelRunControl",
    "ControlAck",
    "ControlRequest",
    "ControlStatus",
    "PauseRequest",
    "ResumeRequest",
    "SteerRequest",
    "StopRequest",
]
