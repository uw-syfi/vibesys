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

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from vs_core.api import ACTION_REFUSAL_TEXT, IDENTITY_PATTERN, ActionRefusal, ResumeWhen, StopMode

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_runtime._run_control import RunControlChannel
    from vs_sim.api import Threads

CONTROL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
"""One to 128 letters, digits, ``.``, ``_`` and ``-``, starting with a letter or digit."""

type SubjectId = Annotated[str, Field(min_length=1, max_length=256, pattern=IDENTITY_PATTERN)]
"""A role, attempt or item a control names: an id core could hold, so no whitespace.

Every request field that names one is this type, so a request naming an id core's types
refuse is itself refused when it is read, with the key named, at every ingress."""


class ControlAction(StrEnum):
    """What a control request asks for; the tag of the ``ControlRequest`` union."""

    STEER = "steer"
    PAUSE = "pause"
    PAUSE_NOW = "pause_now"
    RESUME = "resume"
    WITHDRAW = "withdraw"
    CANCEL_ATTEMPT = "cancel_attempt"
    CANCEL_ITEM = "cancel_item"
    SET_MAX_PARALLEL = "set_max_parallel"


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
    id: SubjectId | None = None
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


def check_one_way_to_resume(*, fallback: bool, when: ResumeWhen) -> None:
    """Raise ``ValueError`` if a resume both switches to the fallback and waits for capacity.

    The one definition of the rule; every model with these two fields validates with it.
    """
    if fallback and when == "capacity_returns":
        message = "fallback resumes now; it cannot wait for capacity to return"
        raise ValueError(message)


class PauseRequest(_Request):
    """Finish turns in flight, release evaluation jobs, save and exit for relaunch.

    ``drain_seconds`` bounds the wait: when it passes, the pause cancels what still runs, as
    Pause-now does. Without it the pause waits for its turns' own limits.
    """

    action: Literal[ControlAction.PAUSE] = ControlAction.PAUSE
    expected_epoch: Annotated[int, Field(ge=0)] | None = None
    drain_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None


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


class PauseNowRequest(_Request):
    """Cancel unfinished work and exit; saved work remains resumable.

    ``drain_seconds`` is how long requests that cannot be cancelled at once get to finish before
    they are interrupted; the run's cleanup bound when absent.
    """

    action: Literal[ControlAction.PAUSE_NOW] = ControlAction.PAUSE_NOW
    expected_epoch: Annotated[int, Field(ge=0)] | None = None
    drain_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None


class CancelAttemptRequest(_Request):
    """Cancel one attempt: it stops, closes cancelled, and is never judged.

    The search hears it close as if it had withdrawn the attempt itself, and the run's agents
    are told at their next turn. A parked attempt waiting to reopen has that reopen cancelled
    and ends. An attempt that has ended is refused when submitted; a blocked one, a parked one
    with no reopen waiting, or one the run does not hold, is refused when the run takes the
    request.
    """

    action: Literal[ControlAction.CANCEL_ATTEMPT] = ControlAction.CANCEL_ATTEMPT
    attempt: SubjectId
    """The attempt id."""


class CancelItemRequest(_Request):
    """Cancel one work item: no attempt at it runs or starts again.

    Each running or waiting attempt at the item stops, as ``CancelAttemptRequest`` stops one;
    a parked one waiting to reopen has its reopen cancelled and ends; any other parked or
    blocked one stays parked and is never reopened; one already finishing finishes, and its
    result stands. The run starts no attempt at the item afterwards; the
    search hears each refused start and reopen. The run's agents are told what became of
    each attempt. An item already cancelled is refused when submitted; one the run does not
    hold is refused when the run takes the request.
    """

    action: Literal[ControlAction.CANCEL_ITEM] = ControlAction.CANCEL_ITEM
    item: SubjectId
    """The work item id."""


class SetMaxParallelRequest(_Request):
    """Change how many attempts the run admits at once.

    A lower limit stops no attempt that runs: the run admits nothing new until enough of them
    end. A higher one admits what now fits at once.
    """

    action: Literal[ControlAction.SET_MAX_PARALLEL] = ControlAction.SET_MAX_PARALLEL
    max_parallel: StrictInt = Field(ge=1)
    """At least 1, and at most the run's launch limit, which sized its host; an integer only."""


type ActionRequest = CancelAttemptRequest | CancelItemRequest | SetMaxParallelRequest
"""An operator action on the run's attempts, items or admission."""


def action_subject(request: ActionRequest) -> str:
    """What ``request`` acts on: the attempt or item id, or the new limit."""
    match request:
        case CancelAttemptRequest(attempt=attempt):
            return attempt
        case CancelItemRequest(item=item):
            return item
        case SetMaxParallelRequest(max_parallel=limit):
            return str(limit)


def action_summary(request: ActionRequest) -> str:
    """The action and its subject, as one event text: ``cancel_attempt A-1``."""
    return f"{request.action.value} {action_subject(request)}"


type ControlRequest = Annotated[
    SteerRequest
    | PauseRequest
    | PauseNowRequest
    | ResumeRequest
    | WithdrawRequest
    | CancelAttemptRequest
    | CancelItemRequest
    | SetMaxParallelRequest,
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
    ATTEMPT_ENDED = "attempt_ended"
    """A cancel of an attempt that has ended: there is nothing left to stop."""
    ALREADY_CANCELLED = "already_cancelled"
    """A cancel of a work item the run already cancelled."""
    ABOVE_LAUNCH_LIMIT = "above_launch_limit"
    """A parallel limit above the run's launch limit."""


REFUSAL_TEXT: dict[RefusalReason, str] = {
    RefusalReason.NOT_PAUSED_ON_CAPACITY: "the run is not paused on a provider capacity limit",
    RefusalReason.STOPPING: "the run is stopping",
    RefusalReason.UNKNOWN_TARGET: "the run has no such steer target",
    RefusalReason.ENDED_TARGET: "the steer's target can no longer take a message",
    RefusalReason.NOT_STANDING: "the withdrawal names no standing guidance in force",
    RefusalReason.RUN_ENDED: "the run has ended",
    RefusalReason.ATTEMPT_ENDED: ACTION_REFUSAL_TEXT[ActionRefusal.ENDED],
    RefusalReason.ALREADY_CANCELLED: ACTION_REFUSAL_TEXT[ActionRefusal.ALREADY_CANCELLED],
    RefusalReason.ABOVE_LAUNCH_LIMIT: ACTION_REFUSAL_TEXT[ActionRefusal.ABOVE_LAUNCH_LIMIT],
}
"""How each refusal reads to an operator, at every ingress; an action's is core's wording."""


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
        """Refuse each later steer, withdrawal or action that ``check`` refuses, when submitted.

        The run loop installs a check over its latest state at each control poll. That state
        may be a poll behind, and a refusal is the id's answer for good, so the check refuses
        only what no later state could accept: a steer to an undeclared role or an ended
        attempt, a withdrawal of a steer that is not, or is no longer, standing guidance, a
        cancel of an ended attempt or of an item already cancelled. The run resolves an
        accepted steer's target and an accepted withdrawal's guidance again when it takes
        them, and reports one it cannot resolve then as dropped; it judges an accepted action
        again by core's rule, and refuses one core refuses then.
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
            case (
                SteerRequest()
                | WithdrawRequest()
                | CancelAttemptRequest()
                | CancelItemRequest()
                | SetMaxParallelRequest()
            ) if (refusal := self._refusal(request)) is not None:
                status, reason = ControlStatus.REFUSED, refusal
            case SteerRequest(text=text, control_id=control_id, target=target, mode=mode):
                self._channel.queue_steer(text, control_id, target, mode)
                status = ControlStatus.PENDING
            case WithdrawRequest(withdraws=withdraws, control_id=control_id):
                self._channel.queue_withdraw(withdraws, control_id)
                status = ControlStatus.PENDING
            case PauseRequest() | PauseNowRequest():
                self._channel.queue_action(request)
                status = ControlStatus.PENDING
            case ResumeRequest():
                status, reason = self._resume(request)
            case CancelAttemptRequest() | CancelItemRequest() | SetMaxParallelRequest():
                self._channel.queue_action(request)
                status = ControlStatus.PENDING
        return self._ack(request, status, reason)

    def _refusal(self, request: ControlRequest) -> RefusalReason | None:
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
            accepted = self._channel.resume(request.control_id)
            status = ControlStatus.CONSUMED
        if accepted:
            return status, None
        # A stop, once pending, stays pending (a stop only escalates), so a stop pending now
        # was pending when the channel refused.
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
        """Release a paused run or cancel a pending pause; a pending stop stays pending."""
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
    "REFUSAL_TEXT",
    "ActionRequest",
    "CancelAttemptRequest",
    "CancelItemRequest",
    "ChannelRunControl",
    "ControlAck",
    "ControlAction",
    "ControlRequest",
    "ControlStatus",
    "PauseNowRequest",
    "PauseRequest",
    "RefusalReason",
    "ResumeRequest",
    "ResumeWhen",
    "SetMaxParallelRequest",
    "SteerMode",
    "SteerRequest",
    "SteerTarget",
    "SteerTargetKind",
    "StopMode",
    "SubjectId",
    "WithdrawRequest",
    "action_subject",
    "action_summary",
    "check_one_way_to_resume",
]
