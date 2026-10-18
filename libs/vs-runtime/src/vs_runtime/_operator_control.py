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

from vs_core.api import ResumeWhen, StopMode

if TYPE_CHECKING:
    from collections.abc import Callable

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
    WITHDRAW = "withdraw"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    control_id: str = Field(pattern=CONTROL_ID_PATTERN)
    """The sender's id for this request; resending it is safe."""


class SteerTargetKind(StrEnum):
    """What a steer is addressed to."""

    RUN = "run"
    """The next session turn of any role."""
    ROLE = "role"
    """The next turn of the role with this role id; the search must declare it."""
    ATTEMPT = "attempt"
    """The next turn in this attempt."""
    ITEM = "item"
    """The next turn of any attempt at this work item."""


class SteerTarget(BaseModel):
    """Whom a steer is for: the whole run, or one role, attempt or item, by id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SteerTargetKind = SteerTargetKind.RUN
    id: str | None = Field(default=None, min_length=1, max_length=256)
    """The role, attempt or item id; absent for the run."""

    @model_validator(mode="after")
    def _id_names_the_target(self) -> SteerTarget:
        if (self.kind is SteerTargetKind.RUN) != (self.id is None):
            message = "id names the role, attempt or item; the run takes none"
            raise ValueError(message)
        return self


class SteerMode(StrEnum):
    """How a steer reaches the turns of its target."""

    NEXT_TURN = "next_turn"
    """The next turn of the target takes it, once."""
    STANDING = "standing"
    """Every turn of the target dispatched from now on takes it, until it is withdrawn
    (``WithdrawRequest``) or the target ends."""
    INTERRUPT = "interrupt"
    """Offered to a turn of the target running now, on a provider that takes a message
    mid-turn; with no such turn, or when the provider refuses it, the next turn takes it."""


class SteerRequest(_Request):
    """Free-text guidance for the agent turns of its target, the whole run by default.

    With ``mode`` ``next_turn`` the message reaches exactly one turn of the target, once; with
    ``standing`` it reaches every turn of the target until withdrawn; with ``interrupt`` it
    reaches a running turn of the target now, or else the next one. A target the run cannot
    resolve (a role the search does not declare, an attempt or item it does not hold, or an
    attempt that has ended) is refused, and the message reaches no one.
    """

    action: Literal[ControlAction.STEER] = ControlAction.STEER
    text: str = Field(min_length=1)
    target: SteerTarget = SteerTarget()
    mode: SteerMode = SteerMode.NEXT_TURN


class WithdrawRequest(_Request):
    """End standing guidance: the standing steer sent under ``withdraws``.

    Turns dispatched after the run takes the withdrawal no longer receive the guidance. A
    withdrawal of a steer the run does not hold as standing guidance in force is refused.
    """

    action: Literal[ControlAction.WITHDRAW] = ControlAction.WITHDRAW
    withdraws: str = Field(pattern=CONTROL_ID_PATTERN)
    """The ``control_id`` of the standing steer to end."""


class PauseRequest(_Request):
    """Reach the next safe boundary and hold the run there."""

    action: Literal[ControlAction.PAUSE] = ControlAction.PAUSE


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
    SteerRequest | PauseRequest | ResumeRequest | StopRequest | WithdrawRequest,
    Field(discriminator="action"),
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
    """A wait for capacity or a switch to the fallback, for a run with no turn waiting on a
    capacity limit to take it."""
    STOPPING = "stopping"
    """A wait for capacity or a switch to the fallback, for a run with a stop pending."""
    UNKNOWN_TARGET = "unknown_target"
    """A steer to a role the search does not declare: no later state of the run declares it.

    An attempt or item the run does not hold yet is not refused when submitted, since the run
    may create it before taking the steer; one it still does not hold then is reported dropped.
    """
    ENDED_TARGET = "ended_target"
    """A steer to an attempt that has ended: no turn of it could take the message."""
    NOT_STANDING = "not_standing"
    """A withdrawal naming no standing steer of the run that is still in force."""
    RUN_ENDED = "run_ended"
    """Any control sent after the run ended: no turn, pause or stop of the run is left to take it."""


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
        self._recorded: Callable[[ControlRequest], bool | None] = _nothing_recorded
        self._check: Callable[[ControlRequest], RefusalReason | None] | None = None
        self._ended = False

    def recall(self, recorded: Callable[[ControlRequest], bool | None]) -> None:
        """Answer ids the run recorded before this process, as ``recorded`` says.

        ``recorded(request)`` is None when the run holds no control under the request's id,
        True when it holds this request, and False when it holds a different one. The run
        loop installs it at each control poll, so after a restart a resent control replays
        its ack without touching the channel, and a different one under its id conflicts,
        as within one process.
        """
        with self._lock:
            self._recorded = recorded

    def check_requests(self, check: Callable[[ControlRequest], RefusalReason | None]) -> None:
        """Refuse each later steer or withdrawal that ``check`` refuses, when it is submitted.

        The run loop installs a check over its latest state at each control poll. That state
        may be a poll behind, and a refusal is the id's answer for good, so the check refuses
        only what no later state could accept: a steer to an undeclared role or an ended
        attempt, or a withdrawal of a steer that is not, or is no longer, standing guidance.
        The run resolves an accepted steer's target, and an accepted withdrawal's guidance,
        again when it takes them, and reports one it cannot resolve then as dropped.
        """
        with self._lock:
            self._check = check

    def end(self) -> None:
        """Refuse every control first sent from now on: the run has ended.

        The run calls this once its end is durable, before it answers what the channel still
        holds, so no control reaches the channel after that last pass. A control answered
        before replays its first ack.
        """
        with self._lock:
            self._ended = True

    def submit(self, request: ControlRequest) -> ControlAck:
        """Apply one control request once per ``control_id`` and say what became of it."""
        with self._lock:
            answered = self._answered.get(request.control_id)
            if answered is not None:
                first, ack = answered
                if first == request:
                    return ack
                return self._ack(request, ControlStatus.CONFLICT)
            match self._recorded(request):
                case True:
                    # Applied by an earlier process: the status it was answered with then.
                    status = (
                        ControlStatus.CONSUMED
                        if isinstance(request, ResumeRequest)
                        else ControlStatus.PENDING
                    )
                    ack = self._ack(request, status)
                case False:
                    return self._ack(request, ControlStatus.CONFLICT)
                case None if self._ended:
                    ack = self._ack(request, ControlStatus.REFUSED, RefusalReason.RUN_ENDED)
                case None:
                    ack = self._apply(request)
            self._answered[request.control_id] = (request, ack)
            return ack

    def _apply(self, request: ControlRequest) -> ControlAck:
        reason = None
        match request:
            case SteerRequest() | WithdrawRequest() if (
                refusal := self._refusal(request)
            ) is not None:
                status, reason = ControlStatus.REFUSED, refusal
            case SteerRequest(text=text, control_id=control_id, target=target, mode=mode):
                self._channel.queue_steer(text, control_id, target, mode)
                status = ControlStatus.PENDING
            case WithdrawRequest(withdraws=withdraws, control_id=control_id):
                self._channel.queue_withdraw(withdraws, control_id)
                status = ControlStatus.PENDING
            case PauseRequest(control_id=control_id):
                self._channel.request_pause(control_id)
                status = ControlStatus.PENDING
            case ResumeRequest():
                status, reason = self._resume(request)
            case StopRequest(mode=mode, control_id=control_id):
                self._channel.request_stop(mode, control_id)
                status = ControlStatus.PENDING
        return self._ack(request, status, reason)

    def _refusal(self, request: SteerRequest | WithdrawRequest) -> RefusalReason | None:
        return None if self._check is None else self._check(request)

    def _resume(self, request: ResumeRequest) -> tuple[ControlStatus, RefusalReason | None]:
        """Apply a resume: at once, with the fallback, or when capacity returns."""
        if request.when == "capacity_returns":
            accepted = self._channel.resume_when_capacity_returns(request.control_id)
            status = ControlStatus.PENDING
        elif request.fallback:
            accepted = self._channel.resume_with_fallback(request.control_id)
            status = ControlStatus.CONSUMED
        else:
            self._channel.resume(request.control_id)
            return ControlStatus.CONSUMED, None
        if accepted:
            return status, None
        # A stop stays pending until a resume, and the refused request resumed nothing, so a
        # stop pending now was pending when the channel refused.
        if self._channel.stop_requested():
            return ControlStatus.REFUSED, RefusalReason.STOPPING
        return ControlStatus.REFUSED, RefusalReason.NOT_PAUSED_ON_CAPACITY

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

    def resume_with_fallback(self) -> bool:
        """Resume, switching a run paused on a capacity limit to its fallback; False if not so paused."""
        return self._channel.resume_with_fallback()

    def resume_when_capacity_returns(self) -> bool:
        """Resume a run paused on a capacity limit once capacity returns; False if not so paused."""
        return self._channel.resume_when_capacity_returns()

    def stop(self, mode: StopMode = StopMode.DRAIN) -> None:
        """Request the run end; draining is the default at this edge."""
        self._channel.request_stop(mode)


def _nothing_recorded(request: ControlRequest) -> bool | None:
    """Before the run loop's first poll: no control is known to be recorded."""
    del request
    return None


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
    "SteerMode",
    "SteerRequest",
    "SteerTarget",
    "SteerTargetKind",
    "StopMode",
    "StopRequest",
    "WithdrawRequest",
    "check_one_way_to_resume",
]
