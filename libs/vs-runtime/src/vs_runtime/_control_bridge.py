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
settled. A committed intent decision also restores its inbox marker before any reapplication.
A control without an id (a session's
``RunControl`` call) is recorded as ``control-<n>``.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Protocol

from vs_core.api import (
    ENVIRONMENT_STOP,
    ArtifactId,
    ArtifactRef,
    ControlAction,
    ControlId,
    ControlInput,
    CoreState,
    InputMode,
    InvocationId,
    PauseOwner,
    RunControlEvent,
    RunPause,
    RunStatus,
    SessionInputReceived,
    SessionPhase,
    StopMode,
    TurnHeld,
    capacity_wait_stands,
    control_refusal,
    escalate_stop,
    pause_after,
    requested_stop_mode,
    run_pause,
    stands_after,
    step,
)
from vs_decode.api import Unreadable, decode_json
from vs_project.api import ProjectStateError, ended_unapplied
from vs_runtime._action_controls import action_control, notice_text, records_action
from vs_runtime._operator_control import (
    CONTROL_REQUEST,
    REFUSAL_TEXT,
    CancelAttemptRequest,
    CancelItemRequest,
    ChannelRunControl,
    ControlStatus,
    ParkRequest,
    PauseRequest,
    ResumeRequest,
    SetMaxParallelRequest,
    SteerRequest,
    StopRequest,
    WithdrawRequest,
    action_summary,
)
from vs_runtime._operator_control import (
    ControlAction as OperatorAction,
)
from vs_runtime._run_end import ending as run_ending
from vs_runtime._steer_delivery import (
    SteerConsumption,
    TargetRefusal,
    input_mode,
    names_target,
    request_refusal,
    standing_in_force,
    standing_input_id,
    steer_input,
    steer_target,
    unrecorded_steers,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from vs_core.api import (
        ControlRefusedError,
        CoreEvent,
        InputId,
        InputTarget,
        ResumeWhen,
    )
    from vs_project.api import ControlInbox, InboxEntry
    from vs_runtime._operator_control import ActionRequest, ControlRequest
    from vs_runtime._run_control import QueuedWithdraw, RefusedPart, RunControlChannel


_LOG = logging.getLogger(__name__)

_HOLDABLE = frozenset({SessionPhase.EXECUTING, SessionPhase.UNKNOWN})
"""The phases of a turn the capacity gate may be holding: dispatched and not yet ended."""

_MAX_REQUEST_BYTES = 1 << 20
"""The largest inbox request the run reads: a steer's text with ample room."""


def _capacity_holds(core: CoreState, channel: RunControlChannel) -> tuple[TurnHeld, ...]:
    """The capacity gate's hold reports for the turns core runs, which credit their limits.

    A report for a turn core does not run (it already ended it) would change nothing, so it
    is dropped here rather than committed.
    """
    running = {
        row.invocation.invocation_id.root
        for row in core.sessions.invocations
        if row.phase in _HOLDABLE
    }
    return tuple(
        TurnHeld(invocation_id=InvocationId(root=invocation), hold=hold)
        for invocation, hold in channel.take_capacity_holds().items()
        if invocation in running
    )


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
    adopts its last choice and ends the run itself. The first poll also records the run's
    deadline on the channel, for work on other threads that must not wait past it, and seeds
    the channel with the pause core recorded (``RunControlChannel.seed``), so a run the
    operator paused stays paused across a restart, and with the stop core recorded, so a
    run the operator stopped stays stopping, as strongly.

    With an ``inbox``, each poll also applies the inbox's requests, in the order they were
    posted, each as one control ``op-<control id>``; see the module docstring for how each
    applies once. The
    channel's stop and pause are recorded first, and again before each inbox request, so a
    request is judged against the pause the run is in. Core's ``control_refusal`` alone
    decides whether a control is accepted, asked of the run as this bridge has asked core to
    record it; a refused inbox request is rejected with core's reason, and reaches neither
    the channel nor core.

    A control the operator already sent to the channel can still be refused: by this bridge
    when it judges the channel's change (the run is finishing), or by core when it commits
    (an input committed first moved the run on; ``settled`` hears of it). Each such refusal
    takes one path, ``_refuse``: the channel undoes what the control asked of it, so its
    pause stays core's, and the operator hears core's reason, through the inbox's rejection
    for an inbox request and as ``control_refused`` on the channel for any other.

    Every inbox request the run sets aside takes one path, ``_reject``, which reports it
    once (``notify_request_rejected``). Once the run has ended, ``end`` refuses what the
    channel still holds and settles or rejects what is left in the inbox.

    ``control`` (by default the inbox's) is where operators submit controls; each poll tells
    it which ids core already records, so a control resent after a restart replays its ack
    and a different control under a recorded id conflicts, as within one process, and which
    steer targets core can still reach, so a steer to any other is refused in its ack.
    """

    def __init__(
        self,
        channel: RunControlChannel,
        steer: SteerArtifacts,
        inbox: OperatorInbox | None = None,
        *,
        control: ChannelRunControl | None = None,
        lifts_standing_stop: bool = False,
    ) -> None:
        """Bind the mailbox, the steer text store, a headless run's inbox, operators' controls.

        ``lifts_standing_stop`` says this process was started by an explicit resume of the
        run, which lifts a stop core recorded before (its first poll records the lift
        instead of arming the stop), if core takes the lift: core refuses to lift one of the
        host's own stops (the deadline's, the run environment's), which stays armed. No other
        start lifts a stop.
        """
        self._channel = channel
        self._steer = steer
        self._inbox = inbox
        self._control = control if control is not None or inbox is None else inbox.control
        self._submitted: set[str] = set()
        """Inbox requests emitted since the shell last committed; ``settled`` clears it."""
        self._asked: dict[ControlId, _Change] = {}
        """Each control emitted since the shell last committed, by identity, for ``settled``."""
        self._told: dict[str, str] = {}
        """The refused controls the operator was told of, with core's reason: a change the
        channel still holds (a resume cannot be undone) is refused again at each poll, but
        told once, and an inbox request under the same id gets the same answer."""
        # Inbox withdrawals submitted in this poll: one that ends nothing is set aside.
        self._inbox_withdrawals: set[str] = set()
        self._warned_damaged = False
        self._count = 0
        self._position = 0
        self._consumption = SteerConsumption()
        self._started = False
        self._lifts_standing_stop = lifts_standing_stop
        self._lift_pending = False
        self._pause: RunPause | None = None
        """Core's pause when it last committed, folded with every pause control emitted since.

        Emitted events may wait in the shell's queue past the next poll, so this, not the
        state a poll is given, is what the bridge has asked core to record."""
        self._stands = False
        """Whether the operator's wait for capacity stands (``capacity_wait_stands``), likewise."""
        self._stop: StopMode | None = None
        """The strongest stop core records or the bridge has asked it to record since.

        A stop only escalates (``escalate_stop``): the channel's stop reaches core again
        only when it is stronger than this."""
        self._park_landed = False
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
        self._consumption.newly_settled(core, self._channel)
        if not self._started:
            self._started = True
            self._channel.set_run_deadline(core.run.deadline_at)
            self._pause = run_pause(core.run)
            self._stands = capacity_wait_stands(core.run)
            self._channel.seed(self._pause, capacity_wait_stands=self._stands)
            self._count = len(core.run.controls)
            self._stop = requested_stop_mode(core.run)
            if self._stop is not None and self._lifts(core):
                # The operator resumed the run: it lifts the stop core recorded before.
                self._lift_pending = True
                self._stop = None
            elif self._stop is not None:
                # A stop core recorded before a restart stays in force: the run's
                # stop bound arms, a resume is refused, and only a cancel escalates it.
                self._channel.request_stop(self._stop)
            self._serve_inbox()
        self._position = len(core.run.controls)
        if self._control is not None:
            self._control.recall(partial(_held_as, core))
            self._control.check_requests(partial(request_refusal, core))
        # Each batch of withdrawals is taken before the steers that follow it: an operator who
        # sends a standing steer and then its withdrawal through the socket has the steer
        # queued first, so this poll takes it too and records it before the withdrawal.
        withdrawals = self._channel.take_pending_withdrawals()
        events: list[CoreEvent] = list(_capacity_holds(core, self._channel))
        events.extend(unrecorded_steers(core, now_at))
        if self._lift_pending:
            self._lift_pending = False
            lift = self._event(core, now_at, _Change("resume", when="now"))
            events += () if lift is None else (lift,)
        self._take_steers(core, now_at, events)
        self._take_actions(core, now_at, events)
        # The pause the run is in reaches core before any request is judged against it.
        self._record_channel(core, now_at, events)
        if self._inbox is not None:
            # A damaged inbox (a symlinked or misplaced directory) leaves its requests waiting
            # and the run going on, with one warning in the run's log.
            try:
                self._read_inbox(self._inbox, core, now_at, events)
            except ProjectStateError as error:
                if not self._warned_damaged:
                    self._warned_damaged = True
                    _LOG.warning(
                        "the run's control inbox cannot be read; its requests wait: %s", error
                    )
        # The inbox's withdrawals are taken only now, after all of its steers, so a withdrawal
        # posted before its own steer still finds it in this poll's events.
        withdrawals += self._channel.take_pending_withdrawals()
        self._take_steers(core, now_at, events)
        self._take_actions(core, now_at, events)
        self._apply_withdrawals(core, now_at, withdrawals, events)
        self._record_channel(core, now_at, events)
        return tuple(events)

    def _lifts(self, core: CoreState) -> bool:
        """Whether this start lifts the stop core holds: it was asked to, and core takes the lift.

        Core takes it until the run's ending commits, so a run already closing on a stop
        stays stopping.
        """
        if not self._lifts_standing_stop:
            return False
        lift = ControlInput(control_id=ControlId(root="pending-lift"), action="resume", when="now")
        return control_refusal(core, lift) is None

    def _record_channel(self, core: CoreState, now_at: float, events: list[CoreEvent]) -> None:
        """Emit the stop and the pause changes the channel holds that core has not recorded.

        A pause change core would refuse (the run is finishing) is refused (``_refuse``):
        the channel undoes it, and the operator hears why.
        """
        if self._channel.environment_stopped() and core.run.status != RunStatus.TERMINAL:
            # The environment's stop is the host's, under core's own id (never ``op-``), so it
            # ends the run for good; it carries the cancel the channel now holds.
            stop = ControlInput(
                control_id=ControlId(root=ENVIRONMENT_STOP), action="stop", mode=StopMode.CANCEL
            )
            if stop.control_id not in _held(core) and stop.control_id not in self._asked:
                events.append(self._recorded(stop, _Change("stop", mode=StopMode.CANCEL), now_at))
            self._stop = escalate_stop(self._stop, StopMode.CANCEL)
        live = self._channel.stop_mode()
        if live is not None and escalate_stop(self._stop, live) is not self._stop:
            self._stop = live
            by = self._channel.requested_by("stop")
            stop = self._event(core, now_at, _Change("stop", mode=live, control_id=by))
            events += () if stop is None else (stop,)
        for change in self._pause_changes(_held(core)):
            refusal = self._record_pause(core, now_at, change, events)
            if refusal is not None:
                self._refuse(change, refusal, request_id=None)

    def _pause_changes(self, held: Collection[ControlId]) -> list[_Change]:
        """The pause and resume controls that bring core's record of the pause to the channel's.

        The channel holds only its current pause, so changes between two polls that left
        the pause as it was are not recorded: core only needs the pause the run is in, and
        whether the operator's wait for capacity stands. A control re-recording a pause the
        operator already holds has no operator id: its id names the first record, which core
        may hold (``held``) even when its pause is gone since (a stop lifts it).
        """
        recorded, live = self._pause, self._channel.pause_state()
        stands = self._channel.capacity_wait_stands()
        by = self._channel.requested_by("pause")
        fallback = self._channel.resumed_with_fallback()
        if live is None:
            if recorded is None and self._stands == stands:
                return []
            return [
                _Change(
                    "resume",
                    when="now",
                    fallback=fallback,
                    capacity_wait_stands=stands,
                    control_id=by,
                    origin=by,
                )
            ]
        changes: list[_Change] = []
        if (
            recorded is not None
            and recorded.owner is PauseOwner.OPERATOR
            and live.owner is not PauseOwner.OPERATOR
        ):
            # Only a resume lifts the operator's hold; a quota stop paused the run again since.
            changes.append(
                _Change("resume", when="now", fallback=fallback, control_id=by, origin=by)
            )
            recorded = None
        operator = live.owner is PauseOwner.OPERATOR
        if (
            recorded is None
            or recorded.owner is not live.owner
            or (recorded.capacity_wait and not live.capacity_wait)
        ):
            first = (recorded is None or recorded.owner is not live.owner) and (
                by is None or _identity(by) not in held
            )
            owner_id = by if operator and first else None
            changes.append(
                _Change(
                    "pause",
                    owner=live.owner,
                    control_id=owner_id,
                    origin=by if operator else None,
                    rerecords=operator and not first,
                )
            )
            recorded = RunPause(owner=live.owner)
        if live.capacity_wait and not recorded.capacity_wait:
            by_capacity = self._channel.requested_by("capacity")
            changes.append(
                _Change(
                    "resume", when="capacity_returns", control_id=by_capacity, origin=by_capacity
                )
            )
        return changes

    def _record_pause(
        self, core: CoreState, now_at: float, change: _Change, events: list[CoreEvent]
    ) -> str | None:
        """Emit one pause or resume control and fold it into the recorded pause.

        Returns why core would refuse it, without emitting it, when core would.
        """
        pending = change.control(ControlId(root="pending"))
        refusal = control_refusal(self._as_asked(core), pending)
        if refusal is not None:
            return refusal.detail
        control = self._event(core, now_at, change)
        events += () if control is None else (control,)
        self._pause = pause_after(self._pause, pending)
        self._stands = stands_after(pending, stood=self._stands)
        return None

    def _as_asked(self, core: CoreState) -> CoreState:
        """``core`` as it will stand once it records every control emitted so far.

        It differs from ``core`` in its status, which the pause controls emitted pause or
        resume, and in a stop emitted, which it holds too.
        """
        run = core.run
        if run.status in (RunStatus.RUNNING, RunStatus.PAUSED):
            status = RunStatus.RUNNING if self._pause is None else RunStatus.PAUSED
            run = run.model_copy(update={"status": status})
        if self._stop is not None and not any(c.action == "stop" for c in run.controls):
            stop = ControlInput(
                control_id=ControlId(root="pending-stop"), action="stop", mode=self._stop
            )
            run = run.model_copy(update={"controls": (*run.controls, stop)})
        return core.model_copy(update={"run": run})

    def _take_steers(self, core: CoreState, now_at: float, events: list[CoreEvent]) -> None:
        """Emit the queued steers, each under its control's id if it had one.

        A steer whose target the run cannot resolve becomes no control: the channel hears why.
        """
        for steer in self._channel.take_pending_steer():
            target = None if steer.target is None else steer_target(core, steer.target)
            if isinstance(target, TargetRefusal):
                # Its target ended between the ack and this poll.
                name = steer.control_id or "steer"
                self._channel.notify_steer_dropped(
                    f"steer {name} reached no turn: {target.reason}",
                    why=target.reason,
                    control_id=steer.control_id,
                )
                continue
            control = self._event(
                core,
                now_at,
                _Change(
                    "steer",
                    text=steer.text,
                    control_id=steer.control_id,
                    target=target,
                    input_mode=input_mode(steer.mode),
                ),
            )
            if control is not None:
                events += (control, steer_input(core, control.control, now_at, self._position))

    def _apply_withdrawals(
        self,
        core: CoreState,
        now_at: float,
        withdrawals: list[QueuedWithdraw],
        events: list[CoreEvent],
    ) -> None:
        """Emit this poll's withdrawals; set aside each inbox one that ends nothing."""
        for withdrawal in withdrawals:
            dropped = self._withdraw(
                core, now_at, withdrawal.withdraws, withdrawal.control_id, events
            )
            if dropped is not None and withdrawal.control_id in self._inbox_withdrawals:
                self._reject_withdrawal(withdrawal.control_id, dropped)
        self._inbox_withdrawals.clear()

    def _withdraw(
        self,
        core: CoreState,
        now_at: float,
        withdraws: str,
        control_id: str,
        events: list[CoreEvent],
    ) -> str | None:
        """Emit the withdrawal of the standing steer sent under ``withdraws``.

        Its guidance must be in force in ``core`` or received in this poll; a withdrawal of
        anything else becomes no control, and the channel hears why. A withdrawal of a steer
        the run has not taken by this poll (one sent later) is dropped the same way. Returns
        why it was dropped, or None when it was emitted.
        """
        input_id = standing_input_id(withdraws)
        if not _standing_now(core, events, withdraws):
            reason = f"steer {withdraws} is not standing guidance in force"
            self._channel.notify_steer_dropped(
                f"withdraw {control_id} ended nothing: {reason}",
                why=reason,
                control_id=control_id,
                withdraws=withdraws,
            )
            return reason
        control = self._event(
            core, now_at, _Change("withdraw", control_id=control_id, withdraws=input_id)
        )
        events += () if control is None else (control,)
        return None

    def _take_actions(
        self,
        core: CoreState,
        now_at: float,
        events: list[CoreEvent],
        *,
        inbox_id: str | None = None,
    ) -> str | None:
        """Emit the queued operator actions, in the order they were queued.

        Core's ``control_refusal`` decides whether each is taken, asked of the state this
        poll's earlier events lead to, so an action follows what was posted before it; the
        notice says what the action does in that state. A refused one becomes no control;
        the operator hears core's reason (``_refuse``). The refusal of the inbox request
        ``inbox_id`` is returned for its caller to reject.
        """
        refused_inbox = None
        for request in self._channel.take_pending_actions():
            state = self._as_asked(_after(core, events))
            change = (
                _Change("park", control_id=request.control_id)
                if isinstance(request, ParkRequest)
                else _Change(
                    request.action.value,
                    text=notice_text(request, state),
                    control_id=request.control_id,
                    action_request=request,
                )
            )
            refusal = control_refusal(state, change.control(_identity(request.control_id)))
            if refusal is None:
                emitted = self._event(core, now_at, change)
                events += () if emitted is None else (emitted,)
            elif request.control_id == inbox_id:
                refused_inbox = f"refused by the run: {refusal.detail}"
            else:
                self._refuse(change, refusal.detail, request_id=None)
        return refused_inbox

    def _reject_withdrawal(self, control_id: str, reason: str) -> None:
        """Set aside an inbox withdrawal that ended nothing: it must not apply after a restart.

        Ending nothing is final for its id, as the socket's one answer is: left pending, a new
        process would read it again and end guidance the operator sent after it.
        """
        try:
            self._reject(control_id, f"it ended nothing: {reason}")
        except ProjectStateError as error:
            _LOG.warning(
                "the run's control inbox cannot be marked; %s waits: %s", control_id, error
            )

    def _reject(self, request_id: str, reason: str) -> None:
        """Set the inbox request ``request_id`` aside with ``reason``: the one inbox rejection.

        The operator is told once, by a ``request_rejected`` run-control event.

        Raises ``ProjectStateError`` when the inbox cannot be marked.
        """
        self._report_outcome(request_id, reason)
        if self._inbox is not None and self._inbox.requests.reject(request_id, reason):
            # A request rejected already keeps its first reason, which it was reported with.
            self._channel.notify_request_rejected(request_id, reason)

    def _refuse(self, change: _Change, reason: str, *, request_id: str | None) -> None:
        """Refuse a control the operator sent and the channel took: the one path for it.

        The channel undoes what the control asked of it (``undo_refused``), so its pause is
        the one core records, and the operator hears ``reason``, core's: an inbox request
        (``request_id``) is rejected with it, and any other control an operator sent is
        reported on the channel. A control no operator can be told of (one sent without an
        id, or a pause change the channel made itself, a quota stop or a timed resume) is
        left unrecorded and logged. A re-recording of a hold core already accepted
        (``rerecords``) is left unrecorded with nothing undone: the operator's pause stands.
        """
        if change.rerecords:
            # Core accepted the operator's hold long ago; only this copy of it is refused.
            return
        if change.part is not None:
            self._channel.undo_refused(change.part, change.origin)
        if request_id is not None:
            try:
                self._reject(request_id, f"refused by the run: {reason}")
            except ProjectStateError as error:
                _LOG.warning(
                    "the run's control inbox cannot be marked; %s waits: %s", request_id, error
                )
        elif change.by_operator:
            name = change.control_id or change.origin
            label = change.action if name is None else f"{change.action} {name}"
            if name is None or name not in self._told:
                if name is not None:
                    self._told[name] = reason
                self._channel.notify_control_refused(
                    f"{label}: {reason}",
                    why=reason,
                    action=OperatorAction(change.action),
                    control_id=change.control_id,
                )
        else:
            # No operator to tell (a control sent without an id, or the channel's own quota
            # pause or timed resume): the run's log keeps the refusal.
            _LOG.warning("the run refused a %s control: %s", change.action, reason)

    def _read_inbox(
        self, inbox: OperatorInbox, core: CoreState, now_at: float, events: list[CoreEvent]
    ) -> None:
        """Settle the requests core holds and apply, once per process, the ones it does not.

        A request that cannot be applied is rejected with the reason, whatever it is.
        Raises ``ProjectStateError`` when the inbox itself cannot be read or marked.
        """
        held = _held(core)
        for entry in inbox.requests.pending():
            if self._settle_decided(inbox, entry.request_id):
                continue
            if self._settle_held(entry, held):
                continue
            if entry.request_id not in self._submitted:
                # Judged against the pause the run is in now, which core is asked to record.
                self._record_channel(core, now_at, events)
                reason = self._apply(inbox, entry, core, now_at, events)
                if reason is None:
                    self._submitted.add(entry.request_id)
                    if inbox.journal is not None:
                        # Commit this decision before judging a later request, so
                        # a later refusal cannot overtake its applied outcome.
                        break
                else:
                    self._reject(entry.request_id, reason)

    def _serve_inbox(self) -> None:
        """Declare this run's inbox format, so ``vibesys control`` knows posting order holds."""
        if self._inbox is None:
            return
        try:
            self._inbox.requests.serve()
        except ProjectStateError as error:
            _LOG.warning("the run's control inbox cannot declare its format: %s", error)

    def end(self, core: CoreState) -> None:
        """Answer every control left once the run has ended: none is left waiting.

        Only core's TERMINAL status says the run ended (``_run_end``); a run that may resume
        keeps its controls waiting. The operators' controls first stop taking new ones
        (``ChannelRunControl.end``): each later one is refused in its ack. Then nothing more
        reaches the channel, and this answers all it holds. A pause or resume is refused
        (``_refuse``) with core's reason, as one refused at commit is, each operator action is
        refused with the run's end, and each steer or withdrawal is reported dropped with it.
        Every inbox request core holds is settled and every other one rejected with the run's
        end, each rejection reported once. This runs after the end is durable, so it finds every request a poster wrote
        before seeing the end; one written after is refused to its poster, and reads as
        rejected whether or not this runs.
        """
        ending = run_ending(core)
        if ending is None:
            return
        if self._control is not None:
            self._control.end()
        # Guidance that ended with the run, and steers the run's end dropped, are reported now:
        # no poll follows the run's end.
        self._consumption.newly_settled(core, self._channel)
        # Core refuses every control of an ended run, so this records nothing.
        self._record_channel(core, core.run.now_at, [])
        for steer in self._channel.take_pending_steer():
            name = steer.control_id or "steer"
            self._channel.notify_steer_dropped(
                f"steer {name} reached no turn: {ending}", why=ending, control_id=steer.control_id
            )
        for withdrawal in self._channel.take_pending_withdrawals():
            self._channel.notify_steer_dropped(
                f"withdraw {withdrawal.control_id} ended nothing: {ending}",
                why=ending,
                control_id=withdrawal.control_id,
                withdraws=withdrawal.withdraws,
            )
        for request in self._channel.take_pending_actions():
            change = (
                _Change("park", control_id=request.control_id)
                if isinstance(request, ParkRequest)
                else _Change(
                    request.action.value, control_id=request.control_id, action_request=request
                )
            )
            self._refuse(change, ending, request_id=None)
        inbox = self._inbox
        if inbox is None:
            return
        try:
            for entry in inbox.requests.pending():
                if not self._settle_decided(inbox, entry.request_id) and not self._settle_held(
                    entry, _held(core)
                ):
                    self._reject(entry.request_id, ended_unapplied(ending))
        except ProjectStateError as error:
            _LOG.warning("the run's control inbox cannot be set aside at its end: %s", error)

    def _settle_held(self, entry: InboxEntry, held: dict[ControlId, ControlInput]) -> bool:
        """Settle or set aside an entry core already decided; whether it was one.

        An unreadable entry is set aside; one whose identity core holds is settled when it
        is that control, and set aside when core holds a different control under its id.
        Raises ``ProjectStateError`` when the inbox cannot be marked.
        """
        if entry.unreadable is not None:
            self._reject(entry.request_id, f"unreadable: {entry.unreadable}")
            return True
        control = held.get(_identity(entry.request_id))
        if control is None:
            return False
        if _is_held(entry, control):
            if self._inbox is not None:
                self._report_outcome(entry.request_id, None)
                self._inbox.requests.settle(entry.request_id)
        else:
            self._reject(entry.request_id, "its control_id already named a different control")
        return True

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
            case ParkRequest():
                return self._take_actions(core, now_at, events, inbox_id=identity)
            case PauseRequest() | ResumeRequest():
                # Core records a capacity wait too, so the entry settles once core holds it.
                # ``_submitted_request`` asked core's rule first, so this is never refused.
                self._record_pause(core, now_at, _pause_change(request), events)
            case StopRequest(mode=mode):
                # Every inbox stop is recorded, so its entry settles; it can still only
                # escalate the run's stop (core's ``stop_mode``).
                self._stop = escalate_stop(self._stop, mode)
                stop = self._event(core, now_at, _Change("stop", mode=mode, control_id=identity))
                events += () if stop is None else (stop,)
            case SteerRequest():
                # Applied now, or queued earlier through the socket under the same id.
                self._take_steers(core, now_at, events)
            case WithdrawRequest():
                # Queued on the channel; ``poll`` takes it after every steer of this poll, and
                # sets the entry aside if it then ends nothing.
                self._inbox_withdrawals.add(identity)
            case CancelAttemptRequest() | CancelItemRequest() | SetMaxParallelRequest():
                # Taken now, in posting order, with any the socket queued before it.
                return self._take_actions(core, now_at, events, inbox_id=identity)
        return None

    def _submitted_request(
        self, inbox: OperatorInbox, entry: InboxEntry, core: CoreState
    ) -> ControlRequest | str:
        """Parse and submit one inbox request; the reason it cannot apply, if it cannot.

        One control id gets one answer, whichever way it was sent: a request whose socket copy
        the run refused when it took it is set aside with that refusal's reason.
        """
        request = _parsed(entry)
        if isinstance(request, Unreadable):
            return f"not a control request: {request.details()}"
        if request.control_id != entry.request_id:
            return f"its control_id {request.control_id!r} is not its name in the inbox"
        # A control refused when the socket's copy of it was taken has its answer already.
        refused = self._told.get(request.control_id)
        refusal = None if refused is not None else self._refusal(request, core)
        if refusal is not None:
            return refusal
        ack = inbox.control.submit(request)
        if ack.status is ControlStatus.CONFLICT:
            return "its control_id already named a different control"
        if refused is None and ack.reason is not None:
            refused = REFUSAL_TEXT[ack.reason]
        return request if refused is None else f"refused by the run: {refused}"

    def _refusal(self, request: ControlRequest, core: CoreState) -> str | None:
        """Why the run refuses ``request`` now, or None; refused requests never reach core.

        Whether core takes a control is core's rule (``control_refusal``), asked of the run
        as this bridge has asked core to record it. A steer's target must also resolve; a
        withdrawal is refused here only by the channel's own check (``request_refusal``),
        which refuses only what no later state could accept; the rest is checked at take, as
        an operator action is, against the requests posted before it.
        """
        if isinstance(request, SteerRequest):
            target = steer_target(core, request.target)
            if isinstance(target, TargetRefusal):
                return target.reason
        pending = ControlId(root="pending")
        match request:
            case ParkRequest():
                check = _Change("park").control(pending)
            case StopRequest(mode=mode):
                check = _Change("stop", mode=mode).control(pending)
            case PauseRequest() | ResumeRequest():
                check = _pause_change(request).control(pending)
            case SteerRequest() | WithdrawRequest():
                # Core takes a steer and a withdrawal under the same rule.
                check = ControlInput(control_id=pending, action="steer")
            case CancelAttemptRequest() | CancelItemRequest() | SetMaxParallelRequest():
                # Judged when taken, against what the requests posted before it changed.
                return None
        refusal = control_refusal(self._as_asked(core), check)
        return None if refusal is None else f"refused by the run: {refusal.detail}"

    def settled(
        self,
        core: CoreState,
        *,
        running: bool,
        refused: Sequence[ControlRefusedError] = (),
    ) -> None:
        """Answer the controls core committed or ``refused``, and report a pause once it landed.

        Call it once the shell has committed, or refused, every event the polls before it
        emitted, ``refused`` being the controls core refused (``CoreRuntime.take_refused``),
        and once more before the run loop exits. What core holds is then what it recorded,
        so the bridge's record of the pause starts again from it. An inbox request core
        holds is settled now, an operator action core holds is reported applied, and every
        control core refused is refused (``_refuse``) with core's reason, so none waits for a
        poll that a run that just ended never makes.

        Core takes no new work while paused, but a request already running (an agent turn,
        a job poll) finishes first; ``running`` says one still is. The pause is reported once
        per stretch of committed ``PAUSED`` status, so the operator sees the run paused when
        it is, not only at an agent turn's end.
        """
        for error in refused:
            change = self._asked.get(error.control.control_id)
            if change is None:
                # Not an operator's: the run loop's own deadline or recovery stop.
                _LOG.warning(
                    "the run refused control %s: %s", error.control.control_id.root, error.detail
                )
                continue
            inbox = change.control_id is not None and change.control_id in self._submitted
            self._refuse(change, error.detail, request_id=change.control_id if inbox else None)
        held = {control.control_id for control in core.run.controls}
        self._settle_inbox(sorted(r for r in self._submitted if _identity(r) in held))
        for identity, change in self._asked.items():
            if change.action_request is not None and identity in held:
                self._channel.notify_action_applied(action_summary(change.action_request))
        self._submitted.clear()
        self._asked.clear()
        self._pause = run_pause(core.run)
        self._stands = capacity_wait_stands(core.run)
        if core.run.status in (RunStatus.DRAINING, RunStatus.PARKED) and not self._park_landed:
            self._park_landed = True
            self._channel.land_park()
        if core.run.status != RunStatus.PAUSED:
            self._landed = False
        elif not running and not self._landed:
            self._landed = True
            self._channel.land_pause()

    def _settle_inbox(self, request_ids: list[str]) -> None:
        """Mark the inbox requests core now holds as applied."""
        if self._inbox is None:
            return
        try:
            for entry in self._inbox.requests.pending():
                if entry.request_id in request_ids:
                    self._report_outcome(entry.request_id, None)
                    self._inbox.requests.settle(entry.request_id)
        except ProjectStateError as error:
            _LOG.warning("the run's control inbox cannot be marked; its requests wait: %s", error)

    def _settle_decided(self, inbox: OperatorInbox, request_id: str) -> bool:
        decision = None if inbox.journal is None else inbox.journal.intent_decision(request_id)
        if decision is None:
            return False
        applied, reason = decision
        if applied:
            inbox.requests.settle(request_id)
        elif reason is not None:
            self._reject(request_id, reason)
        else:
            message = "a recorded refusal must carry its reason"
            raise ValueError(message)
        return True

    def _report_outcome(self, request_id: str, reason: str | None) -> None:
        if self._inbox is None or self._inbox.journal is None:
            return
        entry = next(
            (e for e in self._inbox.requests.pending() if e.request_id == request_id), None
        )
        if entry is None:
            return
        request = _parsed(entry)
        if not isinstance(request, Unreadable):
            self._inbox.journal.intent_outcome(
                request.model_copy(update={"control_id": request_id}), reason
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
                return self._recorded(control, change, now_at)
            _LOG.warning(
                "control %s reuses an id the run recorded for another control; recorded as a new control",
                change.control_id,
            )
        self._count += 1
        control = change.control(ControlId(root=f"control-{self._count}"))
        return self._recorded(control, change, now_at)

    def _recorded(self, control: ControlInput, change: _Change, now_at: float) -> RunControlEvent:
        self._position += 1
        self._asked[control.control_id] = change
        if change.text is not None and control.artifact is not None:
            self._steer.put(control.artifact, change.text)
        return RunControlEvent(control=control, now_at=now_at)


class IntentJournal(Protocol):
    """Persist and recover decisions before the inbox acknowledges them."""

    def intent_decision(self, control_id: str) -> tuple[bool, str | None] | None:
        """Return (applied, reason), or None; a refusal must carry its reason."""
        ...

    def intent_outcome(self, request: ControlRequest, reason: str | None) -> None:
        """Persist one decision; None means applied, otherwise the refusal reason."""
        ...


@dataclass(frozen=True)
class OperatorInbox:
    """A run's durable control inbox and the run's controls it is applied through."""

    requests: ControlInbox
    control: ChannelRunControl
    journal: IntentJournal | None = None


def _held(core: CoreState) -> dict[ControlId, ControlInput]:
    """Each control core records, by its identity."""
    return {control.control_id: control for control in core.run.controls}


def _identity(control_id: str) -> ControlId:
    """The identity core records a control with an id under."""
    return ControlId(root=f"op-{control_id}")


@dataclass(frozen=True)
class _Change:
    """One control to record: its action, a steer's text and target, a stop's mode, its id."""

    action: ControlAction
    text: str | None = None
    mode: StopMode | None = None
    control_id: str | None = None
    target: InputTarget | None = None
    input_mode: InputMode | None = None
    withdraws: InputId | None = None
    action_request: ActionRequest | None = None
    """An operator action's request; ``text`` is then its notice."""
    owner: PauseOwner | None = None
    when: ResumeWhen | None = None
    fallback: bool = False
    capacity_wait_stands: bool = False
    origin: str | None = None
    """The id of the operator control the channel says asked for this pause change; it can
    differ from ``control_id``, the identity core records (a re-recorded hold has none)."""
    rerecords: bool = False
    """Whether this records again an operator hold core already accepted (to clear a capacity
    wait), rather than an operator's request: a refusal of it is not the operator's."""

    @property
    def part(self) -> RefusedPart | None:
        """What this control asked of the channel that a refusal undoes, if anything."""
        if self.action == "pause" and self.owner is PauseOwner.OPERATOR:
            return "hold"
        if self.action == "resume" and self.when == "capacity_returns":
            return "capacity"
        if self.action == "resume" and self.fallback:
            return "fallback"
        return None

    @property
    def by_operator(self) -> bool:
        """Whether an operator sent this control, so a refusal of it is theirs to hear."""
        operator_pause = self.action == "pause" and self.owner is PauseOwner.OPERATOR
        return operator_pause or self.control_id is not None or self.origin is not None

    def control(self, identity: ControlId) -> ControlInput:
        """The control core records; a steer carries its text's digest, not the text."""
        if self.action_request is not None:
            return action_control(identity, self.action_request, self.text or "")
        artifact = None
        if self.text is not None:
            artifact = ArtifactRef(
                artifact_id=ArtifactId(root=f"steer-{identity.root}"),
                digest=hashlib.sha256(self.text.encode()).hexdigest(),
            )
        return ControlInput(
            control_id=identity,
            action=self.action,
            mode=self.mode,
            artifact=artifact,
            target=self.target,
            input_mode=self.input_mode,
            withdraws=self.withdraws,
            owner=self.owner,
            when=self.when,
            fallback=self.fallback,
            capacity_wait_stands=self.capacity_wait_stands,
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

    Core records the action, a stop's mode, a resume's ``when`` and ``fallback``, a pause's
    owner, and the digest and target of a steer's text; a request that differs in any of
    them reuses the id for a different control.
    """
    match request:
        case SteerRequest(text=text, target=target, mode=mode):
            digest = hashlib.sha256(text.encode()).hexdigest()
            return (
                control.artifact is not None
                and control.artifact.digest == digest
                and names_target(target, control.target)
                and control.input_mode == input_mode(mode)
            )
        case WithdrawRequest(withdraws=withdraws):
            return control.action == "withdraw" and control.withdraws == standing_input_id(
                withdraws
            )
        case StopRequest(mode=mode):
            return control.action == "stop" and control.mode == mode
        case ResumeRequest(when=when, fallback=fallback):
            return control.action == "resume" and (control.when, control.fallback) == (
                when,
                fallback,
            )
        case ParkRequest() | PauseRequest():
            return control.action == request.action.value and (
                isinstance(request, ParkRequest) or control.owner is PauseOwner.OPERATOR
            )
        case CancelAttemptRequest() | CancelItemRequest() | SetMaxParallelRequest():
            return records_action(control, request)


def _after(core: CoreState, events: list[CoreEvent]) -> CoreState:
    """The state core reaches from ``core`` once it commits ``events``, in order."""
    for event in events:
        core = step(core, event).state
    return core


def _standing_now(core: CoreState, events: list[CoreEvent], withdraws: str) -> bool:
    """Whether the steer sent under ``withdraws`` is standing guidance in force.

    It is when ``core`` holds it in force, or this poll's ``events`` receive it.
    """
    input_id = standing_input_id(withdraws)
    return standing_in_force(core, withdraws) or any(
        isinstance(event, SessionInputReceived)
        and event.input.input_id == input_id
        and event.input.mode is InputMode.STANDING
        for event in events
    )


def _pause_change(request: PauseRequest | ResumeRequest) -> _Change:
    """The control an operator's pause or resume records."""
    if isinstance(request, PauseRequest):
        return _Change(
            "pause",
            owner=PauseOwner.OPERATOR,
            control_id=request.control_id,
            origin=request.control_id,
        )
    return _Change(
        "resume",
        when=request.when,
        fallback=request.fallback,
        control_id=request.control_id,
        origin=request.control_id,
    )


__all__ = ["OperatorInbox", "RunControlBridge", "SteerArtifacts"]
