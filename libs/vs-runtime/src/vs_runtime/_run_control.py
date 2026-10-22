"""Thread-safe cooperative run control."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from vs_agent.api import AgentExecutionCancelledError
from vs_core.api import PauseOwner, RunPause, StopMode, escalate_stop
from vs_runtime._operator_control import (
    ControlAction,
    PauseNowRequest,
    PauseRequest,
    SteerMode,
    SteerTarget,
    action_summary,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CapacityHold, Scope
    from vs_runtime._operator_control import ActionRequest
    from vs_sim.api import Threads


class RunControlTransitionKind(StrEnum):
    """Closed set of cooperative run-control observations."""

    STEER_QUEUED = "steer_queued"
    GUIDANCE_QUEUED = "guidance_queued"
    """A standing steer was queued: guidance in force until withdrawn, not one turn's."""
    PAUSE_REQUESTED = "pause_requested"
    PARK_REQUESTED = "park_requested"
    RESUMED = "resumed"
    STOP_REQUESTED = "stop_requested"
    STEER_CONSUMED = "steer_consumed"
    STEER_DELIVERED_MID_TURN = "steer_delivered_mid_turn"
    """An interrupt-mode steer reached a turn while it ran; the turn accepted it."""
    STEER_DROPPED = "steer_dropped"
    WITHDRAW_QUEUED = "withdraw_queued"
    GUIDANCE_ENDED = "guidance_ended"
    """Standing guidance reaches no turn any more: withdrawn, or its target or the run ended."""
    GUIDANCE_REACHED = "guidance_reached"
    """Standing guidance reached its first turn: it is in force from that turn on."""
    CONTROL_REFUSED = "control_refused"
    """The run refused an operator control it had acknowledged; ``text`` names it and why."""
    REQUEST_REJECTED = "request_rejected"
    """The run set aside a request from its control inbox: ``request_id``, and ``text`` why."""
    PAUSED = "paused"
    STOPPED = "stopped"
    ACTION_QUEUED = "action_queued"
    """An operator action (a cancel, a new parallel limit) was queued for the run to take."""
    ACTION_APPLIED = "action_applied"
    """Core recorded an operator action, so it applied; ``text`` names it."""


class SteerDeliveryState(StrEnum):
    """Where one operator steer or withdrawal stands, as a client shows it."""

    QUEUED = "queued"
    """The run acknowledged the steer; it waits for the run to take it, then for a turn of its
    target (``mode`` says which turns)."""
    WITHDRAW_QUEUED = "withdraw_queued"
    """The run acknowledged a withdrawal of standing guidance (``withdraws`` names the steer)."""
    REACHED = "reached"
    """A turn received the steer in its prompt (``role``, ``attempt_id``)."""
    REACHED_MID_TURN = "reached_mid_turn"
    """An interrupt-mode steer joined a turn while it ran (``role``, ``attempt_id``)."""
    IN_FORCE = "in_force"
    """Standing guidance reached its first turn; it stays in force until it ends."""
    DROPPED = "dropped"
    """The steer, or the withdrawal, reached no one (``reason``)."""
    ENDED = "ended"
    """Standing guidance reaches no turn any more: withdrawn, or its target or the run ended
    (``reason``)."""
    REFUSED = "refused"
    """The run refused a steer or withdrawal it had acknowledged (``reason``)."""


_STATE_OF_KIND: dict[RunControlTransitionKind, SteerDeliveryState] = {
    RunControlTransitionKind.STEER_QUEUED: SteerDeliveryState.QUEUED,
    RunControlTransitionKind.GUIDANCE_QUEUED: SteerDeliveryState.QUEUED,
    RunControlTransitionKind.WITHDRAW_QUEUED: SteerDeliveryState.WITHDRAW_QUEUED,
    RunControlTransitionKind.STEER_CONSUMED: SteerDeliveryState.REACHED,
    RunControlTransitionKind.STEER_DELIVERED_MID_TURN: SteerDeliveryState.REACHED_MID_TURN,
    RunControlTransitionKind.GUIDANCE_REACHED: SteerDeliveryState.IN_FORCE,
    RunControlTransitionKind.STEER_DROPPED: SteerDeliveryState.DROPPED,
    RunControlTransitionKind.GUIDANCE_ENDED: SteerDeliveryState.ENDED,
    RunControlTransitionKind.CONTROL_REFUSED: SteerDeliveryState.REFUSED,
}
"""The one table from a transition kind to the steer step it is; kinds not in it are no step."""

_FACTS_OF_STATE: dict[SteerDeliveryState, tuple[str, ...]] = {
    SteerDeliveryState.QUEUED: ("mode",),
    SteerDeliveryState.WITHDRAW_QUEUED: ("withdraws",),
    SteerDeliveryState.REACHED: ("role",),
    SteerDeliveryState.REACHED_MID_TURN: ("role",),
    SteerDeliveryState.IN_FORCE: ("role",),
    SteerDeliveryState.DROPPED: ("reason",),
    SteerDeliveryState.ENDED: ("reason",),
    SteerDeliveryState.REFUSED: ("reason",),
}


class SteerDelivery(BaseModel):
    """One step in a steer's life: the facts a client needs to show it, by control id.

    ``control_id`` is the id the operator sent the steer or withdrawal under, so a client
    matches the step to its own send; None for a control sent without one. A step carries
    the facts its state names and no other: ``mode`` when queued, ``withdraws`` for a queued
    withdrawal, ``role`` when a turn took the steer, and ``reason`` when it ended unreached.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: SteerDeliveryState
    control_id: str | None = None
    action: Literal[ControlAction.STEER, ControlAction.WITHDRAW] = ControlAction.STEER
    """Whether the control is a steer or a withdrawal (either can be dropped or refused)."""
    mode: SteerMode | None = None
    target: SteerTarget | None = None
    """Whom the steer is for, set with ``mode``; None is the whole run."""
    withdraws: str | None = None
    """The control id of the standing steer a withdrawal ends."""
    role: str | None = None
    """The session role of the turn the steer reached."""
    attempt_id: str | None = None
    """The attempt the reaching turn ran in; None for a turn of the run itself."""
    reason: str | None = None

    @model_validator(mode="after")
    def _facts_match_state(self) -> SteerDelivery:
        needed = _FACTS_OF_STATE[self.state]
        for name in ("mode", "withdraws", "role", "reason"):
            if (getattr(self, name) is not None) != (name in needed):
                verb = "needs" if name in needed else "has no"
                message = f"{name}: a {self.state.value} step {verb} {name}"
                raise ValueError(message)
        return self


class RunControlTransition(BaseModel):
    """One immutable run-control transition emitted synchronously."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RunControlTransitionKind
    text: str = ""
    agent_kind: str | None = None
    round_label: str | None = None
    execution_id: str | None = None
    request_id: str | None = None
    """The inbox request a ``REQUEST_REJECTED`` transition names; None for every other kind."""
    delivery: SteerDelivery | None = None
    """The steer step this transition is: set for every steer-lifecycle kind, and for a
    ``CONTROL_REFUSED`` only when the refused control was a steer or withdrawal."""

    @model_validator(mode="after")
    def _request_named_only_when_rejected(self) -> RunControlTransition:
        rejected = self.kind is RunControlTransitionKind.REQUEST_REJECTED
        if rejected != (self.request_id is not None):
            message = "request_id: a request_rejected transition names its request; no other does"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _delivery_is_the_kind_step(self) -> RunControlTransition:
        state = _STATE_OF_KIND.get(self.kind)
        refused = self.kind is RunControlTransitionKind.CONTROL_REFUSED
        if self.delivery is None:
            if state is not None and not refused:
                message = f"delivery: a {self.kind.value} transition is a steer step"
                raise ValueError(message)
        elif self.delivery.state is not state:
            message = f"delivery: a {self.kind.value} transition carries no {self.delivery.state.value} step"
            raise ValueError(message)
        return self

    @property
    def summary(self) -> str:
        """The transition as one line for the run's journal, built only here."""
        if self.request_id is not None:
            return f"control request {self.request_id} rejected: {self.text}"
        return self.text


class PauseWake(StrEnum):
    """Why one turn's bounded wait on the run's pause ended."""

    RESUMED = "resumed"
    """The operator resumed the run (directly, or by an operator request to wait for capacity)."""
    ELAPSED = "elapsed"
    """The channel resumed the run itself: its timed resume came due, or the last turn that
    stopped on the quota left a quota pause nothing else would end."""
    DETACHED = "detached"
    """The waiting turn left (it was cancelled); the run's pause state is unchanged."""


class CapacityWake(StrEnum):
    """Why a wait for provider capacity, which the operator asked for, ended."""

    ELAPSED = "elapsed"
    """Capacity should be back: the channel resumed the run itself."""
    RESUMED = "resumed"
    """The operator resumed the run now, ending the wait."""
    CANCELLED = "cancelled"
    """The operator paused the run again: it waits for the operator, not for capacity."""
    REARMED = "rearmed"
    """The operator asked again: the newer request replaces the wait."""
    DETACHED = "detached"
    """The waiting turn left (it was cancelled); the run and the request are unchanged."""


@dataclass(frozen=True, slots=True)
class CapacityWaitEnd:
    """How one wait for capacity ended, and the request now in force after a ``REARMED``."""

    wake: CapacityWake
    request: int


type RefusedPart = Literal["hold", "capacity", "fallback"]
"""What a refused control asked of the channel: an operator hold, a wait for capacity, or a
switch to the quota fallback."""


def _never() -> bool:
    """The *ended* condition of a wait only the channel's own state ends."""
    return False


class RunControlEventSink(Protocol):
    """Receive one run-control transition before its operation returns.

    The channel calls the sink while it holds its own (reentrant) lock, so transitions
    arrive in the order the channel's state changed. The sink may call back into the
    channel on the same thread; it must not wait for another thread that uses the channel.
    """

    def __call__(self, transition: RunControlTransition) -> object:
        """Record or project one semantic transition."""
        ...


@dataclass(frozen=True)
class SteerTurn:
    """The turn a steer reached: its session role, its scope's owner and the attempt it ran in."""

    role: str
    round_label: str
    """The owner of the turn's scope: its attempt, or the run for a turn of the run itself."""
    execution_id: str | None
    attempt_id: str | None
    """The attempt the turn ran in; None for a turn of the run itself."""


@dataclass(frozen=True)
class QueuedSteer:
    """One queued steer: its text, the id of the control that sent it, whom it is for and how.

    A steer with no target is for the whole run.
    """

    text: str
    control_id: str | None = None
    target: SteerTarget | None = None
    mode: SteerMode = SteerMode.NEXT_TURN


@dataclass(frozen=True)
class QueuedWithdraw:
    """One queued withdrawal: the standing steer it ends and the id of the control that sent it."""

    withdraws: str
    control_id: str


class RunControlChannel(Protocol):
    """Cooperative control mailbox shared by callers and the run thread."""

    def queue_steer(
        self,
        text: str,
        control_id: str | None = None,
        target: SteerTarget | None = None,
        mode: SteerMode = SteerMode.NEXT_TURN,
    ) -> None:
        """Queue steering for the turns of ``target`` (the run when None), as ``mode`` says.

        ``control_id`` names the control that sent it. The queue is the single source of
        truth. The run loop drains it (:meth:`take_pending_steer`) and turns each message into
        a durable session input, which the next matching turn receives exactly once, or every
        matching turn while it stands; one that reaches no turn is reported
        (:meth:`notify_steer_dropped`).
        """
        ...

    def queue_withdraw(self, withdraws: str, control_id: str) -> None:
        """Queue the end of the standing steer sent under ``withdraws``.

        The run loop drains withdrawals (:meth:`take_pending_withdrawals`) after the steers
        queued with them, so a steer and its withdrawal taken in one poll apply in that order.
        """
        ...

    def request_pause(self, control_id: str | None = None) -> None:
        """Hold the run for the operator at the next cooperative boundary.

        ``control_id`` names the operator's control. Only the operator calls this, and only
        :meth:`resume` (or :meth:`resume_with_fallback`) lifts the hold: it disarms the
        quota pause's timed resume and keeps any later one from being armed. It also drops
        the operator's own capacity request (:meth:`resume_when_capacity_returns`).
        """
        ...

    def pause_for_quota(self, resume_in: float | None = None) -> None:
        """Pause the run because a turn stopped on a provider capacity limit.

        The capacity gate's entry point; the operator never calls it. ``resume_in`` arms the
        run's timed resume that many seconds from now (the earliest armed wins); ``None``
        leaves the run paused until the operator or an earlier timed resume ends it. Under
        an operator hold nothing is armed. A quota pause never touches the operator's
        capacity request.
        """
        ...

    def resume(self, control_id: str | None = None) -> bool:
        """End a pause or cancel a pending one, releasing a parked run; whether it did.

        A stop only escalates, and nothing undoes it: while a stop is pending the resume
        changes nothing and answers False.
        """
        ...

    def requested_by(self, flag: Literal["pause", "stop", "capacity"]) -> str | None:
        """The id of the control that last set ``flag``, or None when it had none.

        ``pause`` names the latest pause or resume (the run resuming itself has none),
        ``stop`` the pending stop and ``capacity`` the latest capacity wait, so the run
        records each under the id its sender gave.
        """
        ...

    def land_pause(self) -> None:
        """Report that a pause has landed: the run is held paused and nothing of it runs.

        Emits ``PAUSED`` when a pause is in force and no stop is pending; otherwise nothing,
        so a resume or a stop that came first is never reported as a pause. The core run loop
        calls it, as a parked turn reports its own pause.
        """
        ...

    def resume_with_fallback(self, control_id: str | None = None) -> bool:
        """Resume like :meth:`resume`, asking a run paused on a provider capacity limit to switch.

        False, and nothing changes, unless a quota stop paused the run (:meth:`pause_for_quota`),
        a turn stopped on the quota still waits to take the request (:meth:`enter_capacity`),
        and no stop is pending. The request stays pending until a resumed turn takes it with
        :meth:`consume_fallback_request`; the run's next resume drops a request no turn took.
        A run with no fallback configured ignores it.
        """
        ...

    def consume_fallback_request(self) -> bool:
        """Take the pending fallback request: True once per request."""
        ...

    def pause_state(self) -> RunPause | None:
        """Who holds the run paused and whether a capacity wait is armed; None unless paused.

        The facts core records for the pause: the operator owns a pause it holds, whether or
        not a quota stop paused the run as well. This is the run loop's poll, so it resumes
        the run first when the quota pause's timed resume is due, even when no turn waits
        on it any more (a cancelled turn leaves the pause and its timer behind).
        """
        ...

    def resumed_with_fallback(self) -> bool:
        """Whether the run's latest resume asked for the fallback, taken or not."""
        ...

    def capacity_wait_stands(self) -> bool:
        """Whether the operator's wait for capacity stands for the next quota stop.

        Only a restart leaves one (:meth:`seed`); the next turn that parks on a capacity
        limit takes it, and any resume, operator pause or stop ends it.
        """
        ...

    def seed(self, pause: RunPause | None, *, capacity_wait_stands: bool) -> None:
        """Start from the pause core recorded before a restart (``vs_core.run_pause``).

        The operator's hold stays: the run stays paused until the operator resumes it. A
        capacity wait asked for under the hold is dropped, since no turn that could tell
        when capacity returns survived; the operator ends the hold. A quota pause belonged
        to turns that did not survive the restart, so the run resumes and a turn that stops
        on the limit again pauses it again with fresh facts. The operator's wait for capacity
        on a quota pause, or one standing (``vs_core.capacity_wait_stands``), stands: the
        next turn stopped on a capacity limit waits for capacity without asking the operator
        again. Call it once, before any control.
        """
        ...

    def request_stop(self, mode: StopMode, control_id: str | None = None) -> None:
        """Request an unwind at the next cooperative boundary, draining or cancelling work."""
        ...

    def request_environment_stop(self) -> None:
        """Cancel the run because its run environment can run none of its work.

        Unlike an operator's stop, which a resume lifts, the run records this one under
        core's ``ENVIRONMENT_STOP`` (``environment_stopped``): the run ends failed, for good.
        """
        ...

    def environment_stopped(self) -> bool:
        """Whether ``request_environment_stop`` was called."""
        ...

    def take_pending_steer(self) -> list[QueuedSteer]:
        """Drain queued steering exactly once."""
        ...

    def take_pending_withdrawals(self) -> list[QueuedWithdraw]:
        """Drain queued withdrawals exactly once."""
        ...

    def queue_action(self, request: ActionRequest | PauseRequest | PauseNowRequest) -> None:
        """Queue an operator action for the run loop to take at its next control poll.

        The run loop drains actions (:meth:`take_pending_actions`) in the order they were
        queued; core refuses one that can change nothing then (``control_refusal``), and the
        refusal reaches the operator as any refused control's does
        (:meth:`notify_control_refused`, or the inbox's rejection). One core records is
        reported applied (:meth:`notify_action_applied`).
        """
        ...

    def take_pending_actions(self) -> list[ActionRequest | PauseRequest | PauseNowRequest]:
        """Drain queued operator actions exactly once."""
        ...

    def land_park(self, jobs: tuple[str, ...] = (), *, immediate: bool = False) -> None:
        """Report that core committed a park drain."""
        ...

    def notify_action_applied(self, summary: str) -> None:
        """Record that core recorded an operator action, so it applied; ``summary`` names it."""
        ...

    def notify_steer_consumed(
        self, turn: SteerTurn, *, control_id: str | None, note: str = ""
    ) -> None:
        """Record which invocation consumed queued steering.

        ``control_id`` is the id the steer was sent under. ``note`` says why an interrupt-mode
        steer reached this next turn, not a running one.
        """
        ...

    def notify_steer_delivered_mid_turn(self, turn: SteerTurn, *, control_id: str | None) -> None:
        """Record the running invocation an interrupt-mode steer reached, which accepted it."""
        ...

    def notify_guidance_reached(self, turn: SteerTurn, *, control_id: str | None) -> None:
        """Record the first turn standing guidance reached: it is in force from that turn."""
        ...

    def notify_steer_dropped(
        self, text: str, *, why: str, control_id: str | None, withdraws: str | None = None
    ) -> None:
        """Record that an accepted steer, or a withdrawal (``withdraws``), reached no one.

        ``text`` names the control and ``why`` is the bare reason. A steer's target ended or
        became unknown before the run took it, or before a turn of it ran; a withdrawal
        found no standing guidance in force.
        """
        ...

    def notify_guidance_ended(self, text: str, *, why: str, control_id: str | None) -> None:
        """Record that the standing steer ``control_id`` reaches no turn any more.

        ``text`` names it and ``why`` is the bare reason: it was withdrawn, its target or the
        run ended, or its attempt was reopened.
        """
        ...

    def notify_request_rejected(self, request_id: str, reason: str) -> None:
        """Record that the run set aside the inbox request ``request_id``, and why."""
        ...

    def notify_control_refused(
        self, text: str, *, why: str, action: ControlAction, control_id: str | None
    ) -> None:
        """Record that the run refused an operator control of ``action``, and why.

        ``text`` names the control and ``why`` is core's reason. A refused steer or withdrawal
        also reports the step (``SteerDelivery``) with the control id.
        """
        ...

    def undo_refused(self, part: RefusedPart, control_id: str | None) -> None:
        """Undo the ``part`` of the pause the control ``control_id`` asked for: the run refused it.

        The channel then holds the pause core records: a refused hold is lifted (the run
        resumes, unless a quota stop still pauses it), and a refused wait for capacity or
        fallback request is dropped. Nothing changes when a later control has set that part
        since (``control_id`` no longer names it). The quota pause and a pending stop are not
        an operator's request and stay.
        """
        ...

    def prepare_evaluation(self, resource_id: str, scope: Scope) -> None:
        """Identify an evaluation requested by an admitted turn, rather than the strategy."""
        ...

    def evaluation_is_dependency(self, resource_id: str) -> bool:
        """Whether an executing turn still owns this evaluation dependency."""
        ...

    def prepare_execution(self, execution_id: str, scope: Scope) -> None:
        """Associate the turn's execution identity with its declared owner."""
        ...

    def finish_execution(self, execution_id: str) -> None:
        """Release the authority of a turn that finished executing."""
        ...

    def start_execution(
        self,
        started: Callable[[], None],
        *,
        execution_id: str | None = None,
        evaluation_id: str | None = None,
    ) -> None:
        """Publish a dispatch under the same lock as the pause drain."""
        ...

    @property
    def drain_ready(self) -> bool:
        """Every queued job has been fenced or positively observed executing."""
        ...

    def settle_drain_job(self, resource_id: str) -> None:
        """Release the queue barrier for this job."""
        ...

    def wait_while_paused(self) -> None:
        """Park while paused and land a stop that releases the wait.

        The run's timed resume (see :meth:`pause_for_quota`) ends the wait when it comes due.
        """
        ...

    def wait_resumed(self, *, ended: Callable[[], bool] = _never) -> PauseWake:
        """Park until the run resumes; the wait only observes the run's pause.

        The timed resume belongs to the channel (:meth:`pause_for_quota` arms it): any
        waiter or a :meth:`pause_requested` poll resumes the run when it comes due
        (``ELAPSED``), atomically, so a stop request that arrives at the same moment is
        never cleared. ``RESUMED`` says the operator resumed the run. *ended* (rechecked
        when :meth:`interrupt_waits` wakes the wait) ends only this wait, as ``DETACHED``:
        the pause, its timed resume and every other waiter stay as they were. A stop request
        ends the wait by raising :class:`RunStopped`.
        """
        ...

    def raise_if_stopped(self) -> None:
        """Raise :class:`RunStopped` when a stop is pending."""
        ...

    def resume_when_capacity_returns(self, control_id: str | None = None) -> bool:
        """Ask a run parked on a provider capacity limit to resume itself once capacity returns.

        False, and nothing changes, when no turn is parked on a capacity limit. Asking again
        replaces the earlier request; ``resume`` ends the wait at once and ``request_pause``
        cancels it, leaving the run paused for the operator. Another turn's quota stop
        (:meth:`pause_for_quota`) leaves it in force. When the wait ends the run resumes,
        an operator hold included: the operator asked for that resume.
        """
        ...

    def park_for_capacity(
        self, *, waiting: bool = False, ended: Callable[[], bool] = _never
    ) -> int | PauseWake:
        """Park a turn stopped on a capacity limit until the operator resumes it or asks to wait.

        Returns the operator's capacity request (``resume_when_capacity_returns``) when one
        ended the park. Otherwise it returns why the park ended, as the channel recorded it:
        ``RESUMED`` (the operator), ``ELAPSED`` (the run's timed resume) or ``DETACHED``
        (*ended* became true; the run stays paused). With ``waiting`` (the turn already
        waited for capacity at the operator's request) it does not park, and returns a fresh
        request. A stop request ends the park by raising :class:`RunStopped`.
        """
        ...

    def wait_for_capacity(
        self, seconds: float, request: int, *, ended: Callable[[], bool] = _never
    ) -> CapacityWaitEnd:
        """Wait at most ``seconds`` for capacity under ``request``; see :class:`CapacityWake`.

        On ``ELAPSED`` the channel has resumed the run itself, atomically, so a stop arriving
        at that moment is never cleared. *ended* becoming true ends only this wait, as
        ``DETACHED``, changing nothing else. A stop request ends the wait by raising
        :class:`RunStopped`.
        """
        ...

    def interrupt_waits(self) -> None:
        """Wake every parked wait, so each rechecks its *ended* condition.

        Nothing else changes: a wait whose condition still holds parks again.
        """
        ...

    def enter_capacity(self, *, timed: bool = False) -> None:
        """A turn stopped on a quota limit waits on the run's quota pause; it owns the pause.

        Call it before the turn pauses the run (:meth:`pause_for_quota`), so the pause never
        exists without a turn that owns it. A turn that parks for the operator
        (:meth:`park_for_capacity`) lets the operator ask the run to wait for capacity; a
        ``timed`` one waits on the run's timed resume instead. Either kind takes a fallback
        request when the run resumes. Pairs with one :meth:`leave_capacity` of the same kind;
        several turns may be in at once.
        """
        ...

    def leave_capacity(self, *, timed: bool = False) -> None:
        """One turn of that kind stopped waiting on its quota limit.

        When the last turn that parks for the operator leaves, further capacity requests are
        refused and the pending one is dropped. When no turn of either kind is left, a quota
        pause that nothing else ends (no operator hold, no timed resume, no pending stop)
        ends: the run resumes itself (``ELAPSED``), as a restart would resume it, so the run
        never stays paused with no turn, no timer and no request. Leaving without a matching
        :meth:`enter_capacity` raises ``RuntimeError``.
        """
        ...

    def stop_requested(self) -> bool:
        """Return whether a stop is pending, without landing it."""
        ...

    def stop_mode(self) -> StopMode | None:
        """How the pending stop treats work in flight, or ``None`` when no stop is pending."""
        ...

    def pause_requested(self) -> bool:
        """Return whether a pause is pending, without parking on it.

        The run loop polls it, so it resumes the run first when the run's timed resume
        (see :meth:`pause_for_quota`) is due, even when no turn waits on it any more.
        """
        ...

    def on_stop_requested(self, listener: Callable[[], object]) -> Callable[[], None]:
        """Call *listener* after each stop request; return its unsubscribe callable.

        The listener runs on the requesting thread, after the request is
        recorded and published, and must not block.
        """
        ...

    def set_run_deadline(self, at: float) -> None:
        """Record when the run must end, in epoch seconds on the run clock's timeline.

        The run loop records it as it starts, so work the run does on other threads (a
        turn waiting out a provider outage) can bound its own waits by it.
        """
        ...

    def run_deadline(self) -> float | None:
        """When the run must end, or ``None`` before the run loop recorded it."""
        ...

    def note_capacity_hold(self, invocation_id: str, hold: CapacityHold) -> None:
        """Record the capacity gate's latest report of how long it held turn ``invocation_id``.

        The gate holds a stopped turn inside the turn's own call, on the turn's thread; the
        run loop takes the reports at its control poll (:meth:`take_capacity_holds`) and
        core credits them to the turn's limit. A report replaces the turn's earlier one.
        """
        ...

    def take_capacity_holds(self) -> dict[str, CapacityHold]:
        """Drain the latest hold report of each turn reported since the previous poll."""
        ...


class RunStopped(BaseException):
    """A requested stop landed at a cooperative run boundary."""


class RuntimeRunControlChannel:
    """Thread-safe mailbox between control callers and a running policy.

    The run's pause has two owners, kept apart so neither undoes the other: the operator's
    hold (:meth:`request_pause`, lifted only by :meth:`resume`) and the quota pause
    (:meth:`pause_for_quota`, lifted by the operator or by its timed resume). The run is
    paused while either is in force. Every transition is emitted while the lock is held,
    so the event stream follows the order of the state changes.
    """

    def __init__(self, events: RunControlEventSink, threads: Threads) -> None:
        """Bind the channel to a synchronous semantic event sink; ``threads`` makes its lock."""
        self._events = events
        # Reentrant: the sink runs under the lock and may read the channel on this thread.
        self._lock = threads.condition(threads.rlock())
        self._pending_steer: list[QueuedSteer] = []
        self._pending_withdrawals: list[QueuedWithdraw] = []
        self._pending_actions: list[ActionRequest | PauseRequest | PauseNowRequest] = []
        self._held = False
        self._quota_paused = False
        self._stop_requested = False
        self._stop_mode: StopMode | None = None
        self._environment_stopped = False
        self._requested_by: dict[Literal["pause", "stop", "capacity"], str | None] = {}
        self._fallback_requested = False
        self._resumed_with_fallback = False
        # A capacity wait core recorded before a restart: the next capacity stop takes it.
        self._capacity_standing = False
        self._stop_listeners: list[Callable[[], object]] = []
        self._execution_closed = False
        self._prepared_executions: dict[str, Scope] = {}
        self._evaluation_dependencies: dict[str, frozenset[str]] = {}
        self._executing: dict[str, Scope] = {}
        self._drain_jobs: set[str] = set()
        self._now = threads.now
        self._capacity_turns = 0
        self._timed_turns = 0
        self._capacity_request: int | None = None
        self._capacity_requests = 0
        self._run_deadline: float | None = None
        self._capacity_holds: dict[str, CapacityHold] = {}
        # The quota pause's timed resume, on the ``threads.now`` timeline. ``None`` unless
        # a quota pause armed one, and always ``None`` under the operator's hold.
        self._resume_at: float | None = None
        self._resumed_by = PauseWake.RESUMED

    def prepare_evaluation(self, resource_id: str, scope: Scope) -> None:
        """Capture the executing callers once; a later turn cannot inherit their queued jobs."""
        with self._lock:
            self._evaluation_dependencies.setdefault(
                resource_id,
                frozenset(key for key, owner in self._executing.items() if owner == scope),
            )

    def evaluation_is_dependency(self, resource_id: str) -> bool:
        """A turn that requested this evaluation must still be executing."""
        with self._lock:
            owners = self._evaluation_dependencies.get(resource_id, frozenset())
            return not self._stop_requested and not owners.isdisjoint(self._executing)

    def prepare_execution(self, execution_id: str, scope: Scope) -> None:
        """Associate dispatch identity with its declared owner before preparation ends."""
        with self._lock:
            self._prepared_executions[execution_id] = scope

    def finish_execution(self, execution_id: str) -> None:
        """Release this execution's dependency authority after the provider returns."""
        with self._lock:
            self._prepared_executions.pop(execution_id, None)
            self._executing.pop(execution_id, None)

    def start_execution(
        self,
        started: Callable[[], None],
        *,
        execution_id: str | None = None,
        evaluation_id: str | None = None,
    ) -> None:
        """Publish dispatch atomically with the pause drain, preserving live dependencies."""
        with self._lock:
            allowed = not self._stop_requested and not self._execution_closed
            if execution_id is None and evaluation_id is not None:
                allowed = allowed or self.evaluation_is_dependency(evaluation_id)
            if not allowed:
                message = "pause drain cancelled preparation before execution"
                raise AgentExecutionCancelledError(message)
            if execution_id is not None:
                scope = self._prepared_executions.pop(execution_id, None)
                if scope is not None:
                    self._executing[execution_id] = scope
            started()

    @property
    def drain_ready(self) -> bool:
        """Queued jobs have either ended or been positively observed already executing."""
        with self._lock:
            return not self._drain_jobs

    def settle_drain_job(self, resource_id: str) -> None:
        """Release a queued-job barrier for the run's next control poll."""
        with self._lock:
            self._drain_jobs.discard(resource_id)

    @property
    def _paused(self) -> bool:
        """Whether the run is paused, by the operator or by a quota stop; lock held."""
        return self._held or self._quota_paused

    def queue_steer(
        self,
        text: str,
        control_id: str | None = None,
        target: SteerTarget | None = None,
        mode: SteerMode = SteerMode.NEXT_TURN,
    ) -> None:
        """Queue free-text steering for the run loop to take at its next control poll."""
        with self._lock:
            self._pending_steer.append(QueuedSteer(text, control_id, target, mode))
            self._events(
                RunControlTransition(
                    kind=RunControlTransitionKind.GUIDANCE_QUEUED
                    if mode is SteerMode.STANDING
                    else RunControlTransitionKind.STEER_QUEUED,
                    text=text,
                    delivery=SteerDelivery(
                        state=SteerDeliveryState.QUEUED,
                        control_id=control_id,
                        mode=mode,
                        target=target or SteerTarget(),
                    ),
                )
            )

    def queue_withdraw(self, withdraws: str, control_id: str) -> None:
        """Queue a withdrawal for the run loop to take at its next control poll."""
        with self._lock:
            self._pending_withdrawals.append(QueuedWithdraw(withdraws, control_id))
            self._events(
                RunControlTransition(
                    kind=RunControlTransitionKind.WITHDRAW_QUEUED,
                    text=withdraws,
                    delivery=SteerDelivery(
                        state=SteerDeliveryState.WITHDRAW_QUEUED,
                        control_id=control_id,
                        action=ControlAction.WITHDRAW,
                        withdraws=withdraws,
                    ),
                )
            )

    def request_pause(self, control_id: str | None = None) -> None:
        """Hold the run for the operator; only an operator resume lifts the hold.

        The request is published before a boundary can see it, so a run that
        parks concurrently never reports ``PAUSED`` ahead of the request.
        """
        with self._lock:
            self._emit(RunControlTransitionKind.PAUSE_REQUESTED)
            self._held = True
            self._resume_at = None
            self._requested_by["pause"] = control_id
            self._capacity_request = None
            self._requested_by.pop("capacity", None)
            self._capacity_standing = False
            self._lock.notify_all()

    def pause_for_quota(self, resume_in: float | None = None) -> None:
        """Pause the run for a quota stop, arming its timed resume unless the operator holds it."""
        with self._lock:
            self._emit(RunControlTransitionKind.PAUSE_REQUESTED)
            self._quota_paused = True
            if resume_in is not None and not self._held:
                at = self._now() + resume_in
                self._resume_at = at if self._resume_at is None else min(self._resume_at, at)
            self._lock.notify_all()

    def resume(self, control_id: str | None = None) -> bool:
        """End or cancel a pause and release a parked run, unless a stop is pending."""
        with self._lock:
            if self._stop_requested:
                return False
            self._release(fallback=False, control_id=control_id)
        return True

    def requested_by(self, flag: Literal["pause", "stop", "capacity"]) -> str | None:
        """The id of the control that last set ``flag``, or None when it had none."""
        with self._lock:
            return self._requested_by.get(flag)

    def land_pause(self) -> None:
        """Emit ``PAUSED`` while a pause is in force and no stop is pending."""
        with self._lock:
            self._land_pause()

    def _land_pause(self) -> None:
        """Emit ``PAUSED`` if a pause is in force and no stop is pending; lock held."""
        if self._paused and not self._stop_requested:
            self._emit(RunControlTransitionKind.PAUSED)

    def _release(self, *, fallback: bool, control_id: str | None) -> None:
        """Resume the run for an operator; the caller holds the lock and no stop is pending."""
        with self._lock:
            self._unpause(PauseWake.RESUMED)
            self._fallback_requested = self._resumed_with_fallback = fallback
            self._requested_by = {"pause": control_id}

    def _unpause(self, cause: PauseWake) -> None:
        """Resume the run, wake every waiter and emit ``RESUMED``; the caller holds the lock."""
        self._held = False
        self._quota_paused = False
        self._resumed_by = cause
        self._resume_at = None
        self._capacity_request = None
        self._requested_by.pop("capacity", None)
        # A fallback request answers the pause it ended; one no turn took is stale by now.
        self._fallback_requested = self._resumed_with_fallback = False
        self._capacity_standing = False
        # The run resuming itself has no control id; an operator resume records its own after.
        self._requested_by["pause"] = None
        self._lock.notify_all()
        self._emit(RunControlTransitionKind.RESUMED)

    def _fire_due(self) -> bool:
        """Resume the run if its timed resume is due; the caller holds the lock."""
        due = (
            self._paused
            and not self._stop_requested
            and self._resume_at is not None
            and self._now() >= self._resume_at
        )
        if due:
            self._unpause(PauseWake.ELAPSED)
        return due

    def _wait_until_due(self, until: float | None = None) -> None:
        """Park on the condition until notified, the timed resume, or *until*; lock held."""
        deadlines = [at for at in (self._resume_at, until) if at is not None]
        if not deadlines:
            self._lock.wait()
            return
        self._lock.wait(max(min(deadlines) - self._now(), 0.0))

    def resume_with_fallback(self, control_id: str | None = None) -> bool:
        """Resume a quota-paused run, leaving a fallback request for a waiting turn to take."""
        with self._lock:
            waiting = self._capacity_turns + self._timed_turns
            if not (self._quota_paused and waiting) or self._stop_requested:
                return False
            self._release(fallback=True, control_id=control_id)
        return True

    def consume_fallback_request(self) -> bool:
        """Take the pending fallback request: True once per request."""
        with self._lock:
            requested, self._fallback_requested = self._fallback_requested, False
        return requested

    def pause_state(self) -> RunPause | None:
        """The pause as core records it; None while the run is not paused."""
        with self._lock:
            # The run's poll: with no turn left waiting, nothing else fires a due timed resume.
            self._fire_due()
            if not self._paused:
                return None
            owner = PauseOwner.OPERATOR if self._held else PauseOwner.QUOTA
            return RunPause(owner=owner, capacity_wait=self._capacity_request is not None)

    def resumed_with_fallback(self) -> bool:
        """Whether the latest resume asked for the fallback."""
        with self._lock:
            return self._resumed_with_fallback

    def capacity_wait_stands(self) -> bool:
        """Whether a wait for capacity a restart kept still waits for the next quota stop."""
        with self._lock:
            return self._capacity_standing

    def seed(self, pause: RunPause | None, *, capacity_wait_stands: bool) -> None:
        """Take the pause core recorded before this process started; see the protocol."""
        with self._lock:
            if pause is not None and pause.owner is PauseOwner.OPERATOR:
                self._held = True
            elif capacity_wait_stands or (pause is not None and pause.capacity_wait):
                self._capacity_standing = True

    def request_stop(self, mode: StopMode, control_id: str | None = None) -> None:
        """Request that the run unwind at its next cooperative boundary, as ``mode`` says.

        The request is published before a boundary can see it, so a run that
        reaches a boundary concurrently never reports ``STOPPED`` ahead of the
        request: a consumer folding the events would otherwise see a stop land
        on a run that was never stopping.

        A stop only escalates (``escalate_stop``): a cancel replaces a pending drain, and
        a later drain never weakens a pending cancel. The request that set the mode in
        force names the stop. Only a request that changes the stop in force is announced
        (``STOP_REQUESTED`` and the stop listeners); a repeat changes nothing.
        """
        with self._lock:
            held = self._stop_mode if self._stop_requested else None
            in_force = escalate_stop(held, mode)
            if in_force is held:
                return
            self._emit(RunControlTransitionKind.STOP_REQUESTED)
            self._requested_by["stop"] = control_id
            self._stop_requested = True
            self._stop_mode = in_force
            self._lock.notify_all()
            listeners = tuple(self._stop_listeners)
        for listener in listeners:
            listener()

    def request_environment_stop(self) -> None:
        """Cancel the run for its environment's failure; see the protocol."""
        with self._lock:
            self._environment_stopped = True
        self.request_stop(StopMode.CANCEL)

    def environment_stopped(self) -> bool:
        """Whether the run environment stopped the run."""
        with self._lock:
            return self._environment_stopped

    def stop_requested(self) -> bool:
        """Return whether a stop is pending, without landing it."""
        with self._lock:
            return self._stop_requested

    def stop_mode(self) -> StopMode | None:
        """How the pending stop treats work in flight, or ``None`` when none is pending."""
        with self._lock:
            return self._stop_mode if self._stop_requested else None

    def pause_requested(self) -> bool:
        """Return whether a pause is pending, without parking on it.

        This is the run loop's poll, so it also fires the run's timed resume once due.
        """
        with self._lock:
            self._fire_due()
            return self._paused

    def on_stop_requested(self, listener: Callable[[], object]) -> Callable[[], None]:
        """Call *listener* after each stop request; return its unsubscribe callable."""
        with self._lock:
            self._stop_listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._stop_listeners:
                    self._stop_listeners.remove(listener)

        return unsubscribe

    def set_run_deadline(self, at: float) -> None:
        """Record when the run must end."""
        with self._lock:
            self._run_deadline = at

    def run_deadline(self) -> float | None:
        """When the run must end, or ``None`` before it was recorded."""
        with self._lock:
            return self._run_deadline

    def note_capacity_hold(self, invocation_id: str, hold: CapacityHold) -> None:
        """Keep the latest hold report of the turn until the next poll takes it."""
        with self._lock:
            self._capacity_holds[invocation_id] = hold

    def take_capacity_holds(self) -> dict[str, CapacityHold]:
        """Drain the hold reports made since the previous poll, the latest of each turn."""
        with self._lock:
            holds, self._capacity_holds = self._capacity_holds, {}
        return holds

    def take_pending_steer(self) -> list[QueuedSteer]:
        """Drain steering queued since the previous invocation boundary."""
        with self._lock:
            pending, self._pending_steer = self._pending_steer, []
        return pending

    def take_pending_withdrawals(self) -> list[QueuedWithdraw]:
        """Drain withdrawals queued since the previous control poll."""
        with self._lock:
            pending, self._pending_withdrawals = self._pending_withdrawals, []
        return pending

    def queue_action(self, request: ActionRequest | PauseRequest | PauseNowRequest) -> None:
        """Queue an operator action for the run loop to take at its next control poll."""
        with self._lock:
            self._pending_actions.append(request)
            if not isinstance(request, PauseRequest | PauseNowRequest):
                self._emit(RunControlTransitionKind.ACTION_QUEUED, text=action_summary(request))

    def take_pending_actions(self) -> list[ActionRequest | PauseRequest | PauseNowRequest]:
        """Drain operator actions queued since the previous control poll."""
        with self._lock:
            pending, self._pending_actions = self._pending_actions, []
        return pending

    def land_park(self, jobs: tuple[str, ...] = (), *, immediate: bool = False) -> None:
        """Report core's accepted drain, after it is persisted."""
        with self._lock:
            self._execution_closed = True
            if immediate:
                self._executing.clear()
            self._drain_jobs.update(jobs)
            self._emit(RunControlTransitionKind.PARK_REQUESTED)

    def notify_action_applied(self, summary: str) -> None:
        """Record that core recorded an operator action, so it applied."""
        with self._lock:
            self._emit(RunControlTransitionKind.ACTION_APPLIED, text=summary)

    def notify_steer_consumed(
        self, turn: SteerTurn, *, control_id: str | None, note: str = ""
    ) -> None:
        """Record the invocation identity that consumed queued steering, and any note."""
        self._reached(
            RunControlTransitionKind.STEER_CONSUMED,
            SteerDeliveryState.REACHED,
            turn,
            control_id,
            text=note,
        )

    def notify_steer_delivered_mid_turn(self, turn: SteerTurn, *, control_id: str | None) -> None:
        """Record the running invocation an interrupt-mode steer reached."""
        self._reached(
            RunControlTransitionKind.STEER_DELIVERED_MID_TURN,
            SteerDeliveryState.REACHED_MID_TURN,
            turn,
            control_id,
        )

    def notify_guidance_reached(self, turn: SteerTurn, *, control_id: str | None) -> None:
        """Record the first turn standing guidance reached."""
        self._reached(
            RunControlTransitionKind.GUIDANCE_REACHED,
            SteerDeliveryState.IN_FORCE,
            turn,
            control_id,
        )

    def _reached(
        self,
        kind: RunControlTransitionKind,
        state: SteerDeliveryState,
        turn: SteerTurn,
        control_id: str | None,
        *,
        text: str = "",
    ) -> None:
        """Publish a step that names the turn a steer reached."""
        with self._lock:
            self._events(
                RunControlTransition(
                    kind=kind,
                    text=text,
                    agent_kind=turn.role,
                    round_label=turn.round_label,
                    execution_id=turn.execution_id,
                    delivery=SteerDelivery(
                        state=state,
                        control_id=control_id,
                        role=turn.role,
                        attempt_id=turn.attempt_id,
                    ),
                )
            )

    def notify_steer_dropped(
        self, text: str, *, why: str, control_id: str | None, withdraws: str | None = None
    ) -> None:
        """Record why a queued steer, or a withdrawal, reached no one."""
        with self._lock:
            self._events(
                RunControlTransition(
                    kind=RunControlTransitionKind.STEER_DROPPED,
                    text=text,
                    delivery=SteerDelivery(
                        state=SteerDeliveryState.DROPPED,
                        control_id=control_id,
                        action=ControlAction.STEER if withdraws is None else ControlAction.WITHDRAW,
                        reason=why,
                    ),
                )
            )

    def notify_guidance_ended(self, text: str, *, why: str, control_id: str | None) -> None:
        """Record that standing guidance reaches no turn any more, and why."""
        with self._lock:
            self._events(
                RunControlTransition(
                    kind=RunControlTransitionKind.GUIDANCE_ENDED,
                    text=text,
                    delivery=SteerDelivery(
                        state=SteerDeliveryState.ENDED, control_id=control_id, reason=why
                    ),
                )
            )

    def notify_control_refused(
        self, text: str, *, why: str, action: ControlAction, control_id: str | None
    ) -> None:
        """Record that the run refused an operator control, and why."""
        steer_step = action in (ControlAction.STEER, ControlAction.WITHDRAW)
        with self._lock:
            self._events(
                RunControlTransition(
                    kind=RunControlTransitionKind.CONTROL_REFUSED,
                    text=text,
                    delivery=SteerDelivery(
                        state=SteerDeliveryState.REFUSED,
                        control_id=control_id,
                        action=action,
                        reason=why,
                    )
                    if steer_step
                    else None,
                )
            )

    def undo_refused(self, part: RefusedPart, control_id: str | None) -> None:
        """Undo what the refused control asked of the channel; see the protocol."""
        with self._lock:
            match part:
                case "hold" if self._held and self._requested_by.get("pause") == control_id:
                    self._held = False
                    self._requested_by["pause"] = None
                    if self._quota_paused:
                        self._lock.notify_all()
                    else:
                        self._unpause(PauseWake.RESUMED)
                case "capacity" if (
                    self._capacity_request is not None
                    and self._requested_by.get("capacity") == control_id
                ):
                    self._capacity_request = None
                    self._requested_by.pop("capacity", None)
                    self._lock.notify_all()
                case "fallback" if (
                    self._resumed_with_fallback and self._requested_by.get("pause") == control_id
                ):
                    self._fallback_requested = self._resumed_with_fallback = False
                case _:
                    pass

    def notify_request_rejected(self, request_id: str, reason: str) -> None:
        """Record that an inbox request was set aside, naming it and the reason."""
        with self._lock:
            self._events(
                RunControlTransition(
                    kind=RunControlTransitionKind.REQUEST_REJECTED,
                    text=reason,
                    request_id=request_id,
                )
            )

    def wait_while_paused(self) -> None:
        """Park while paused, then land any stop that released the wait."""
        with self._lock:
            self._land_pause()
            while self._paused and not self._stop_requested:
                if self._fire_due():
                    break
                self._wait_until_due()
            self._raise_if_stopped()

    def wait_resumed(self, *, ended: Callable[[], bool] = _never) -> PauseWake:
        """Park until the run resumes or *ended* (``DETACHED``); arms nothing."""
        with self._lock:
            self._land_pause()
            while True:
                if self._stop_requested:
                    wake = PauseWake.RESUMED
                    break
                if self._fire_due():
                    wake = PauseWake.ELAPSED
                    break
                if not self._paused:
                    wake = self._resumed_by
                    break
                if ended():
                    wake = PauseWake.DETACHED
                    break
                self._wait_until_due()
            self._raise_if_stopped()
        return wake

    def resume_when_capacity_returns(self, control_id: str | None = None) -> bool:
        """Arm a resume for when capacity returns; False when no turn waits on a capacity limit."""
        with self._lock:
            if not (self._capacity_turns and self._paused and not self._stop_requested):
                return False
            self._capacity_request = self._next_capacity_request()
            self._requested_by["capacity"] = control_id
            self._lock.notify_all()
        return True

    def park_for_capacity(
        self, *, waiting: bool = False, ended: Callable[[], bool] = _never
    ) -> int | PauseWake:
        """Park until asked to wait for capacity (the request), resumed, or *ended*."""
        with self._lock:
            self._land_pause()
            waiting, self._capacity_standing = waiting or self._capacity_standing, False
            if waiting and self._paused and not self._stop_requested:
                # The run asks for itself: no operator control names this request.
                self._capacity_request = self._next_capacity_request()
                self._requested_by.pop("capacity", None)
            while (
                self._paused
                and not self._stop_requested
                and self._capacity_request is None
                and not ended()
            ):
                if self._fire_due():
                    break
                self._wait_until_due()
            self._raise_if_stopped()
            if not self._paused:
                return self._resumed_by
            if self._capacity_request is None or ended():
                return PauseWake.DETACHED
            return self._capacity_request

    def wait_for_capacity(
        self, seconds: float, request: int, *, ended: Callable[[], bool] = _never
    ) -> CapacityWaitEnd:
        """Wait for capacity under ``request``, ending as :class:`CapacityWake` says."""
        with self._lock:
            until = self._now() + seconds
            while True:
                self._fire_due()
                if self._stop_requested or not self._paused or self._capacity_request != request:
                    end = self._capacity_end(request)
                    break
                if ended():
                    end = CapacityWaitEnd(CapacityWake.DETACHED, request)
                    break
                if self._now() >= until:
                    # The operator asked for this resume, so it lifts a hold too.
                    self._unpause(PauseWake.ELAPSED)
                    end = CapacityWaitEnd(CapacityWake.ELAPSED, request)
                    break
                self._wait_until_due(until)
            self._raise_if_stopped()
        return end

    def interrupt_waits(self) -> None:
        """Wake every parked wait to recheck its *ended* condition."""
        with self._lock:
            self._lock.notify_all()

    def enter_capacity(self, *, timed: bool = False) -> None:
        """Count one more turn waiting on a quota stop, of the kind ``timed`` says."""
        with self._lock:
            if timed:
                self._timed_turns += 1
            else:
                self._capacity_turns += 1

    def leave_capacity(self, *, timed: bool = False) -> None:
        """Count one turn out; see the protocol for what the last one out ends."""
        with self._lock:
            if not (self._timed_turns if timed else self._capacity_turns):
                message = "leave_capacity without a matching enter_capacity"
                raise RuntimeError(message)
            if timed:
                self._timed_turns -= 1
            else:
                self._capacity_turns -= 1
            if not self._capacity_turns:
                self._capacity_request = None
                self._requested_by.pop("capacity", None)
            orphaned = (
                self._quota_paused
                and not (self._held or self._stop_requested)
                and self._resume_at is None
                and not (self._capacity_turns or self._timed_turns)
            )
            if orphaned:
                # The turns that stopped owned the pause and none is left.
                self._unpause(PauseWake.ELAPSED)

    def _capacity_end(self, request: int) -> CapacityWaitEnd:
        """How the wait for ``request`` stands; the caller holds the lock."""
        if self._stop_requested:
            return CapacityWaitEnd(CapacityWake.CANCELLED, request)
        if not self._paused:
            timed = self._resumed_by is PauseWake.ELAPSED
            return CapacityWaitEnd(CapacityWake.ELAPSED if timed else CapacityWake.RESUMED, request)
        if self._capacity_request is None:
            return CapacityWaitEnd(CapacityWake.CANCELLED, request)
        return CapacityWaitEnd(CapacityWake.REARMED, self._capacity_request)

    def _next_capacity_request(self) -> int:
        self._capacity_requests += 1
        return self._capacity_requests

    def raise_if_stopped(self) -> None:
        """Land and raise a requested stop at the current boundary."""
        with self._lock:
            self._raise_if_stopped()

    def _raise_if_stopped(self) -> None:
        """Emit ``STOPPED`` and raise when a stop is pending; lock held."""
        if self._stop_requested:
            self._emit(RunControlTransitionKind.STOPPED)
            raise RunStopped

    def _emit(self, kind: RunControlTransitionKind, *, text: str = "") -> None:
        """Publish one transition; the caller holds the lock, so events follow state order."""
        self._events(RunControlTransition(kind=kind, text=text))


__all__ = [
    "CapacityWaitEnd",
    "CapacityWake",
    "PauseWake",
    "QueuedSteer",
    "QueuedWithdraw",
    "RefusedPart",
    "RunControlChannel",
    "RunControlEventSink",
    "RunControlTransition",
    "RunControlTransitionKind",
    "RunStopped",
    "RuntimeRunControlChannel",
    "SteerDelivery",
    "SteerDeliveryState",
    "SteerTurn",
]
