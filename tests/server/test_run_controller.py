"""Pause, stop, steering, and terminal-state tests for the run controller."""

from pathlib import Path
from typing import Literal

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.server.support import (
    DEADLOCK_GUARD_S,
    REQUEST_TIME,
    ServerParts,
    build_server_parts,
    run_at_paused_boundary,
)

from server.api.protocol import (
    ControlCommand,
    PauseCommand,
    Response,
    ResumeCommand,
    SteerCommand,
    StopCommand,
    decode_request,
)
from server.api.service import (
    CapacityWaitUnavailableError,
    ControlIdConflictError,
    ControlProtocolCommand,
    RunControlSlot,
    RunStoppingError,
    control_request,
)
from server.events import (
    EventStatus,
    EventType,
    RunEvent,
    RunStatusChangedData,
)
from server.run_lifecycle import RunStatus
from vibesys.api import (
    CancelAttemptRequest,
    CancelItemRequest,
    ControlAck,
    ControlAction,
    ControlRequest,
    ControlStatus,
    PauseNowRequest,
    PauseRequest,
    RefusalReason,
    RunStopped,
    SetMaxParallelRequest,
    SteerDeliveryData,
    SteerDeliveryState,
    SteerMode,
    SteerRequest,
    SteerTarget,
    SteerTargetKind,
    SteerTurn,
    WithdrawRequest,
)
from vibesys.events import CoreEventType
from vibesys.events import EventStatus as CoreEventStatus
from vs_core.api import StopMode
from vs_runtime.api.infrastructure import (
    ChannelRunControl,
    RunControlTransition,
    create_run_control_channel,
)
from vs_sim.api.testing import ManualClock, SimThreads


def _status_changes(parts: ServerParts) -> list[tuple[RunStatus, RunStatus]]:
    """Return every published transition as ``(previous, status)``."""
    return [
        (event.data.previous, event.data.status)
        for event in parts.journal.read()
        if isinstance(event.data, RunStatusChangedData)
    ]


def _folded_status(events: list[RunEvent], through_sequence: int) -> RunStatus | None:
    """Fold the published transitions the way a client does."""
    folded: RunStatus | None = None
    for event in events:
        if event.sequence > through_sequence:
            break
        if isinstance(event.data, RunStatusChangedData):
            folded = event.data.status
    return folded


def _enter_boundary(parts: ServerParts, kind: str, round_label: str, user_prompt: str) -> str:
    """Simulate core's entry-side agent turn control gating for a test.

    Production gating lives in the core run loop, not in the server's
    `RunController.before_agent`/`start_agent_execution`. Tests that need pause-blocking or
    stop-raising at an invocation boundary replicate that call sequence here, against the
    same `RunControlChannel` the real loop reads. Steering is not simulated: on the core
    path it is a session input that the next turn receives, and
    `tests/system/steer/test_steer_reaches_the_next_turn.py` drives that for real.
    """
    control = parts.control
    control.raise_if_stopped()
    control.wait_while_paused()
    return parts.start_execution(kind, round_label, user_prompt).user_prompt


def test_pause_takes_effect_at_next_safe_point(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    execution = parts.start_execution("implementer", "round 1", "work")
    parts.control.request_pause()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)

    result: list[str] = []
    waiter = run_at_paused_boundary(
        parts, lambda: result.append(_enter_boundary(parts, "judge", "round 1", "prompt"))
    )
    assert result == []  # a paused boundary cannot be passed
    parts.control.resume()
    waiter.join(timeout=DEADLOCK_GUARD_S)
    assert result == ["prompt"]


def test_steering_queued_while_paused_survives_resume_for_core_to_take(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    execution = parts.start_execution("implementer", "round 1", "work")
    parts.control.request_pause()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)

    result: list[str] = []
    waiter = run_at_paused_boundary(
        parts, lambda: result.append(_enter_boundary(parts, "judge", "round 1", "Review"))
    )
    parts.control.queue_steer("check for reward hacking")
    parts.control.resume()
    waiter.join(timeout=DEADLOCK_GUARD_S)

    assert result == ["Review"]
    # Steering queued while paused waits for core to take it; resuming neither drops nor
    # delivers it, and it reaches a turn only as session input (see the composition test).
    assert [steer.text for steer in parts.control.take_pending_steer()] == [
        "check for reward hacking"
    ]


def test_api_control_commands_ack_and_reach_controller(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)

    pause = parts.api.execute(PauseCommand(timestamp=REQUEST_TIME))
    resume = parts.api.execute(ResumeCommand(timestamp=REQUEST_TIME))
    steer = parts.api.execute(SteerCommand(timestamp=REQUEST_TIME, text="prioritize latency"))

    assert pause.ack is not None
    assert resume.ack is not None
    assert steer.ack is not None
    assert (pause.ack.action, pause.ack.status) == ("pause", "pending")
    assert (resume.ack.action, resume.ack.status) == ("resume", "consumed")
    assert (steer.ack.action, steer.ack.status) == ("steer", "pending")
    assert [steer.text for steer in parts.control.take_pending_steer()] == ["prioritize latency"]


def test_a_resume_command_can_ask_for_the_fallback_and_a_plain_one_cannot(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)

    parts.control.pause_for_quota()
    parts.api.execute(ResumeCommand(timestamp=REQUEST_TIME))
    assert parts.control.consume_fallback_request() is False

    parts.control.enter_capacity(timed=True)  # a stopped turn waits to take the switch
    parts.control.pause_for_quota()
    ack = parts.api.execute(ResumeCommand(timestamp=REQUEST_TIME, fallback=True)).ack

    assert ack is not None
    assert (ack.action, ack.status) == ("resume", "consumed")
    assert parts.control.consume_fallback_request() is True
    assert parts.control.consume_fallback_request() is False  # taken once


def test_waiting_for_capacity_is_refused_for_a_run_not_paused_on_a_capacity_limit(
    tmp_path: Path,
) -> None:
    parts = build_server_parts(tmp_path)
    parts.api.execute(PauseCommand(timestamp=REQUEST_TIME))

    with pytest.raises(CapacityWaitUnavailableError) as refused:
        parts.api.execute(ResumeCommand(timestamp=REQUEST_TIME, when="capacity_returns"))

    assert refused.value.diagnostic_code == "capacity_wait_unavailable"
    assert len(parts.control.take_pending_actions()) == 1


def test_the_fallback_is_refused_for_a_run_not_paused_on_a_capacity_limit(
    tmp_path: Path,
) -> None:
    parts = build_server_parts(tmp_path)
    parts.api.execute(PauseCommand(timestamp=REQUEST_TIME))

    with pytest.raises(CapacityWaitUnavailableError):
        parts.api.execute(ResumeCommand(timestamp=REQUEST_TIME, fallback=True))

    assert len(parts.control.take_pending_actions()) == 1
    assert parts.control.consume_fallback_request() is False


@pytest.mark.parametrize("when", ["now", "capacity_returns"])
def test_a_capacity_resume_for_a_stopping_run_answers_that_it_is_stopping(
    tmp_path: Path, when: Literal["now", "capacity_returns"]
) -> None:
    parts = build_server_parts(tmp_path)
    parts.control.enter_capacity()
    parts.control.pause_for_quota()
    parts.control.request_stop(StopMode.CANCEL)

    with pytest.raises(RunStoppingError) as refused:
        parts.api.execute(ResumeCommand(timestamp=REQUEST_TIME, fallback=when == "now", when=when))

    assert refused.value.diagnostic_code == "run_stopping"
    assert parts.control.stop_requested()
    assert parts.control.consume_fallback_request() is False


def test_a_resume_cannot_both_switch_to_the_fallback_and_wait_for_capacity() -> None:
    with pytest.raises(ValidationError, match="fallback"):
        ResumeCommand(timestamp=REQUEST_TIME, fallback=True, when="capacity_returns")


def test_finish_is_idempotent_and_interrupts_controlled_executions(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    execution = parts.start_execution("implementer", "round 1", "work")

    parts.controller.finish(RuntimeError("first failure"))
    parts.controller.finish(RuntimeError("second failure"))

    failed = [event for event in parts.journal.read() if event.type is EventType.RUN_FAILED]
    assert len(failed) == 1
    assert failed[0].diagnostic is not None
    assert failed[0].diagnostic.detail == "RuntimeError: first failure"
    finished = next(
        event
        for event in parts.journal.read()
        if event.type is EventType.AGENT_EXECUTION_FINISHED
        and event.execution_id == execution.execution_id
    )
    assert finished.status is EventStatus.INTERRUPTED


def test_pause_waits_for_the_core_drain_boundary(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    parts.start_execution("implementer", "round 1", "work")
    reply = parts.api.execute(PauseCommand(timestamp=REQUEST_TIME))
    assert reply.ack is not None
    assert (reply.ack.action, reply.ack.status) == ("pause", "pending")
    (request,) = parts.control.take_pending_actions()
    assert request.action == "pause"
    assert parts.api.snapshot().status is RunStatus.RUNNING


def test_every_transition_publishes_exactly_one_status_event(tmp_path: Path) -> None:
    """The status a client folds is the status the controller holds."""
    parts = build_server_parts(tmp_path)
    execution = parts.start_execution("implementer", "round 1", "work")
    parts.controller.pause_after_call()
    # A repeated request changes nothing, so it publishes nothing.
    parts.controller.pause_after_call()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)
    parts.controller.resume()
    parts.controller.finish()

    assert _status_changes(parts) == [
        (RunStatus.STARTING, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.PAUSING),
        (RunStatus.PAUSING, RunStatus.PAUSED),
        (RunStatus.PAUSED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.COMPLETED),
    ]
    # The human-readable audit record survives alongside the typed one.
    controls = [
        (event.text, event.status)
        for event in parts.journal.read()
        if event.type is EventType.CONTROL
    ]
    assert controls == [
        ("/pause", EventStatus.PENDING),
        ("/pause", EventStatus.PENDING),
        ("/pause", EventStatus.CONSUMED),
        ("/resume", EventStatus.CONSUMED),
    ]


def test_resume_before_the_boundary_cancels_the_pending_pause(tmp_path: Path) -> None:
    """A resume that beats the boundary leaves no pause to apply later."""
    parts = build_server_parts(tmp_path)
    execution = parts.start_execution("implementer", "round 1", "work")
    parts.controller.pause_after_call()
    parts.controller.resume()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)

    assert parts.api.snapshot().status is RunStatus.RUNNING
    assert _status_changes(parts) == [
        (RunStatus.STARTING, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.PAUSING),
        (RunStatus.PAUSING, RunStatus.RUNNING),
    ]


def test_pause_applies_without_a_matching_execution(tmp_path: Path) -> None:
    """The compatibility boundary is still a boundary, and still publishes."""
    parts = build_server_parts(tmp_path)
    parts.controller.pause_after_call()

    parts.controller.after_agent("implementer", "round 1")

    assert parts.api.snapshot().status is RunStatus.PAUSED
    assert _status_changes(parts)[-1] == (RunStatus.PAUSING, RunStatus.PAUSED)


def test_snapshot_status_agrees_with_the_fold_at_the_terminal_event(tmp_path: Path) -> None:
    """No sequence containing the terminal event can still read as running."""
    parts = build_server_parts(tmp_path)
    parts.core_events.emit(CoreEventType.RUN_FINISHED, status=CoreEventStatus.COMPLETED)

    snapshot = parts.api.snapshot()
    assert snapshot.status is RunStatus.COMPLETED
    events = parts.journal.read()
    terminal = next(event for event in events if event.type is EventType.RUN_FINISHED)
    assert _folded_status(events, terminal.sequence) is RunStatus.COMPLETED
    assert _folded_status(events, snapshot.sequence) is snapshot.status
    # The run ended once: the later `finish` from the runtime adds nothing.
    parts.controller.finish()
    assert [event.type for event in parts.journal.read()].count(EventType.RUN_FINISHED) == 1


def test_legacy_host_shutdown_reaches_the_process_stop_boundary(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    parts.start_execution("implementer", "round 1", "work")
    reply = parts.api.execute(StopCommand(timestamp=REQUEST_TIME))
    assert reply.ack is not None
    assert (reply.ack.action, reply.ack.status) == ("pause_now", "pending")
    assert parts.control.stop_mode() is StopMode.DRAIN
    assert parts.control.take_pending_actions() == []
    assert parts.api.snapshot().status is RunStatus.STOPPING


def test_stopped_run_refuses_the_next_controlled_invocation(tmp_path: Path) -> None:
    """After the stop lands, entering the next boundary unwinds the run."""
    parts = build_server_parts(tmp_path)
    execution = parts.start_execution("implementer", "round 1", "work")
    parts.control.request_stop(StopMode.DRAIN)
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)

    with pytest.raises(RunStopped):
        _enter_boundary(parts, "judge", "round 1", "review")


def test_stop_requested_between_invocations_starts_no_further_call(tmp_path: Path) -> None:
    """The entry side of the boundary is a boundary: nothing else starts."""
    parts = build_server_parts(tmp_path)
    parts.control.request_stop(StopMode.DRAIN)

    with pytest.raises(RunStopped):
        _enter_boundary(parts, "implementer", "round 1", "work")

    assert parts.api.snapshot().status is RunStatus.STOPPED
    assert _status_changes(parts)[-1] == (RunStatus.STOPPING, RunStatus.STOPPED)
    assert not any(
        event.type is EventType.AGENT_EXECUTION_STARTED for event in parts.journal.read()
    )


def test_stop_releases_the_pause_wait_and_ends_the_run(tmp_path: Path) -> None:
    """A stop from `paused` wakes the parked thread and ends without a call."""
    parts = build_server_parts(tmp_path)
    execution = parts.start_execution("implementer", "round 1", "work")
    parts.control.request_pause()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)
    assert parts.api.snapshot().status is RunStatus.PAUSED

    raised: list[BaseException] = []

    def wait_at_boundary() -> None:
        try:
            _enter_boundary(parts, "judge", "round 1", "review")
        except RunStopped as error:
            raised.append(error)

    waiter = run_at_paused_boundary(parts, wait_at_boundary)
    assert raised == []  # a paused boundary cannot be passed

    parts.control.request_stop(StopMode.DRAIN)

    waiter.join(timeout=DEADLOCK_GUARD_S)
    assert not waiter.is_alive()
    assert [type(error) for error in raised] == [RunStopped]
    assert parts.api.snapshot().status is RunStatus.STOPPED
    assert _status_changes(parts)[-2:] == [
        (RunStatus.PAUSED, RunStatus.STOPPING),
        (RunStatus.STOPPING, RunStatus.STOPPED),
    ]


def test_a_resume_before_the_boundary_leaves_the_stop_pending(tmp_path: Path) -> None:
    """A stop only escalates: a resume that beats the stop boundary does not cancel it."""
    parts = build_server_parts(tmp_path)
    execution = parts.start_execution("implementer", "round 1", "work")
    parts.controller.stop_after_call()
    parts.controller.resume()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)

    assert parts.api.snapshot().status is RunStatus.STOPPED


def test_finish_after_a_landed_stop_records_no_terminal_event(tmp_path: Path) -> None:
    """The terminal status change is the stop's terminal record."""
    parts = build_server_parts(tmp_path)
    parts.control.request_stop(StopMode.DRAIN)
    with pytest.raises(RunStopped):
        _enter_boundary(parts, "implementer", "round 1", "work")

    parts.controller.finish()

    assert not any(
        event.type in (EventType.RUN_FINISHED, EventType.RUN_FAILED)
        for event in parts.journal.read()
    )
    assert _status_changes(parts)[-1] == (RunStatus.STOPPING, RunStatus.STOPPED)
    assert parts.api.snapshot().status is RunStatus.STOPPED


def test_stop_does_not_block_presentation_only_chat(tmp_path: Path) -> None:
    """Chat stays available on a stopped run, exactly as on a finished one."""
    parts = build_server_parts(tmp_path)
    parts.control.request_stop(StopMode.DRAIN)
    with pytest.raises(RunStopped):
        _enter_boundary(parts, "implementer", "round 1", "work")

    execution = parts.start_execution(
        "chat",
        "experiment-chat",
        "what happened?",
        participates_in_run_control=False,
    )
    parts.controller.after_agent(
        "chat", "experiment-chat", result="answer", execution_id=execution.execution_id
    )

    finished = [
        event
        for event in parts.journal.read()
        if event.type is EventType.AGENT_EXECUTION_FINISHED
        and event.execution_id == execution.execution_id
    ]
    assert len(finished) == 1
    assert finished[0].status is EventStatus.COMPLETED
    assert parts.api.snapshot().status is RunStatus.STOPPED


def test_finish_does_not_interrupt_presentation_only_chat(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    execution = parts.start_execution(
        "chat",
        "experiment-chat",
        "what happened?",
        participates_in_run_control=False,
    )

    parts.controller.finish()

    assert [active.execution_id for active in parts.api.snapshot().active_executions] == [
        execution.execution_id
    ]
    parts.controller.after_agent(
        "chat", "experiment-chat", result="answer", execution_id=execution.execution_id
    )
    finished = [
        event
        for event in parts.journal.read()
        if event.type is EventType.AGENT_EXECUTION_FINISHED
        and event.execution_id == execution.execution_id
    ]
    assert len(finished) == 1
    assert finished[0].status is EventStatus.COMPLETED


_SINGLE_ACTION_COMMANDS = st.one_of(
    st.builds(PauseCommand, timestamp=st.just(REQUEST_TIME)),
    st.builds(ResumeCommand, timestamp=st.just(REQUEST_TIME), fallback=st.booleans()),
    st.builds(
        SteerCommand,
        timestamp=st.just(REQUEST_TIME),
        text=st.text(min_size=1, max_size=20).filter(str.strip),
    ),
)


def _channel_state(parts: ServerParts) -> tuple[object, ...]:
    return (
        parts.control.take_pending_steer(),
        parts.control.pause_requested(),
        parts.control.stop_mode(),
        parts.control.consume_fallback_request(),
        parts.control.take_pending_actions(),
    )


def _answer(parts: ServerParts, command: ControlProtocolCommand) -> object:
    """The command's ack, or the type of the protocol error it was refused with."""
    try:
        return parts.api.execute(command).ack
    except CapacityWaitUnavailableError as refused:
        return type(refused)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(commands=st.lists(_SINGLE_ACTION_COMMANDS, max_size=6), quota_paused=st.booleans())
def test_a_control_command_does_what_the_single_action_command_it_carries_does(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    commands: list[ControlProtocolCommand],
    quota_paused: bool,
) -> None:
    """``command.control`` and the single-action commands reach the run through one entry point."""
    single = build_server_parts(tmp_path_factory.mktemp("single"))
    carried = build_server_parts(tmp_path_factory.mktemp("carried"))
    if quota_paused:
        single.control.pause_for_quota()
        carried.control.pause_for_quota()
    for command in commands:
        wrapped = ControlCommand(timestamp=REQUEST_TIME, control=control_request(command))
        assert _answer(single, command) == _answer(carried, wrapped)
    assert _channel_state(single) == _channel_state(carried)


def test_a_control_command_with_a_key_its_action_does_not_declare_is_refused_naming_it() -> None:
    frame = (
        '{"type": "command.control", "timestamp": "2026-01-01T00:00:00Z",'
        ' "control": {"control_id": "c1", "action": "steer", "text": "go",'
        ' "checker": "always-pass"}}'
    )

    response = decode_request(frame, clock=ManualClock())

    assert isinstance(response, Response)
    assert not response.ok
    assert response.diagnostic is not None
    assert response.diagnostic.validation_paths is not None
    assert any("checker" in path for path in response.diagnostic.validation_paths)


def test_a_resent_control_is_one_control_and_a_reused_id_is_a_conflict(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    steer = ControlCommand(
        timestamp=REQUEST_TIME, control=SteerRequest(control_id="c1", text="prefer latency")
    )

    first = parts.api.execute(steer).ack
    again = parts.api.execute(steer).ack
    with pytest.raises(ControlIdConflictError) as conflict:
        parts.api.execute(
            ControlCommand(timestamp=REQUEST_TIME, control=PauseRequest(control_id="c1"))
        )

    assert first == again
    assert conflict.value.diagnostic_code == "control_id_conflict"
    assert [steer.text for steer in parts.control.take_pending_steer()] == ["prefer latency"]
    assert not parts.control.pause_requested()


def _ignore(transition: RunControlTransition) -> None:
    del transition


class _RefusingControl(ChannelRunControl):
    """The run's control, refusing every request for one reason."""

    def __init__(self, reason: RefusalReason, threads: SimThreads) -> None:
        super().__init__(create_run_control_channel(_ignore, threads=threads), threads)
        self.reason = reason

    def submit(self, request: ControlRequest) -> ControlAck:
        return ControlAck(
            control_id=request.control_id,
            action=request.action,
            status=ControlStatus.REFUSED,
            reason=self.reason,
        )


@pytest.mark.regression
@example(before=[StopMode.CANCEL, StopMode.DRAIN], after=[])
@example(before=[StopMode.DRAIN], after=[StopMode.CANCEL, StopMode.DRAIN])
@example(before=[], after=[StopMode.CANCEL, "resume"])
@given(
    before=st.lists(st.sampled_from(StopMode), max_size=3),
    after=st.lists(st.one_of(st.sampled_from(StopMode), st.just("resume")), max_size=4),
)
def test_host_stop_remains_strongest_after_installing_pending_pause_now(
    before: list[StopMode], after: list[StopMode | Literal["resume"]]
) -> None:
    """periodic-1330 MED-2: the slot kept the last stop sent before the run started, so a
    drain after a cancel weakened it; and a Ctrl-C reached the run by no single path.

    Stops sent before the run's control is installed (operator commands) and after it
    (operator commands or the server's Ctrl-C, ``stop``) leave the run's stop at the
    strongest asked for: a cancel anywhere is a cancel.
    """
    threads = SimThreads()
    channel = create_run_control_channel(_ignore, threads=threads)
    slot = RunControlSlot(threads.condition(threads.rlock()))
    assert not slot.stop(StopMode.CANCEL)
    for index, _mode in enumerate(before):
        stop = PauseNowRequest(control_id=f"before-{index}")
        slot.execute(ControlCommand(timestamp=REQUEST_TIME, control=stop))
    slot.install(ChannelRunControl(channel, threads))
    for item in after:
        if item == "resume":
            # PR #3209 MED-3: a resume never weakens a pending stop; it is refused.
            if channel.stop_mode() is None:
                slot.execute(ResumeCommand(timestamp=REQUEST_TIME))
            else:
                with pytest.raises(RunStoppingError):
                    slot.execute(ResumeCommand(timestamp=REQUEST_TIME))
        else:
            assert slot.stop(item)

    asked = [item for item in after if item != "resume"]
    expected = (
        None if not asked else StopMode.CANCEL if StopMode.CANCEL in asked else StopMode.DRAIN
    )
    assert channel.stop_mode() == expected


@pytest.mark.parametrize("reason", list(RefusalReason))
def test_every_refusal_reason_answers_with_a_protocol_error(reason: RefusalReason) -> None:
    """A new refusal reason without its protocol error fails here, not as a server crash."""
    threads = SimThreads()
    slot = RunControlSlot(threads.condition(threads.rlock()))
    slot.install(_RefusingControl(reason, threads))

    with pytest.raises(RuntimeError) as refused:
        slot.execute(PauseCommand(timestamp=REQUEST_TIME))

    assert isinstance(getattr(refused.value, "diagnostic_code", None), str)


def test_a_refused_steer_is_journaled_as_a_failed_control(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)

    parts.control.notify_steer_dropped(
        "role 'judge' is not declared", why="role 'judge' is not declared", control_id=None
    )

    (refused,) = [
        event
        for event in parts.journal.read()
        if event.type is EventType.CONTROL and event.status is EventStatus.FAILED
    ]
    assert "role 'judge' is not declared" in refused.text


def test_a_control_the_run_refuses_is_journaled_as_a_failed_control(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)

    parts.control.notify_control_refused(
        "pause p1: cleanup must finish before pause or resume",
        why="cleanup must finish before pause or resume",
        action=ControlAction.PAUSE,
        control_id="p1",
    )

    (refused,) = [
        event
        for event in parts.journal.read()
        if event.type is EventType.CONTROL and event.status is EventStatus.FAILED
    ]
    assert refused.text == "/control refused: pause p1: cleanup must finish before pause or resume"


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        (RefusalReason.UNKNOWN_TARGET, "steer_target_unknown"),
        (RefusalReason.ENDED_TARGET, "steer_target_ended"),
    ],
)
def test_a_steer_the_run_refuses_answers_its_protocol_error(
    tmp_path: Path, reason: RefusalReason, code: str
) -> None:
    parts = build_server_parts(tmp_path)
    parts.operator_control.check_requests(lambda _request: reason)
    steer = SteerRequest(
        control_id="s1",
        text="look again",
        target=SteerTarget(kind=SteerTargetKind.ROLE, id="fake-judge"),
    )

    with pytest.raises(RuntimeError) as refused:
        parts.api.execute(ControlCommand(timestamp=REQUEST_TIME, control=steer))

    assert getattr(refused.value, "diagnostic_code", None) == code
    assert parts.control.take_pending_steer() == []


def test_a_withdrawal_of_no_standing_guidance_answers_its_protocol_error(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    parts.operator_control.check_requests(lambda _request: RefusalReason.NOT_STANDING)
    withdraw = WithdrawRequest(control_id="w1", withdraws="s1")

    with pytest.raises(RuntimeError) as refused:
        parts.api.execute(ControlCommand(timestamp=REQUEST_TIME, control=withdraw))

    assert getattr(refused.value, "diagnostic_code", None) == "withdraw_not_standing"
    assert parts.control.take_pending_withdrawals() == []


def test_a_control_sent_after_the_run_ended_answers_that_run_control_is_unavailable(
    tmp_path: Path,
) -> None:
    """The run ended before its session did: the same answer as a control after the session."""
    parts = build_server_parts(tmp_path)
    parts.operator_control.end()

    with pytest.raises(RuntimeError) as refused:
        parts.api.execute(
            ControlCommand(timestamp=REQUEST_TIME, control=PauseRequest(control_id="p1"))
        )

    assert getattr(refused.value, "diagnostic_code", None) == "run_control_unavailable"
    assert str(refused.value) == "Run control is unavailable after the run session ends"
    assert not parts.control.pause_requested()


@pytest.mark.parametrize(
    ("request_", "reason", "code"),
    [
        (
            CancelAttemptRequest(control_id="c1", attempt="A-1"),
            RefusalReason.ATTEMPT_ENDED,
            "action_attempt_ended",
        ),
        (
            CancelItemRequest(control_id="c1", item="I-1"),
            RefusalReason.ALREADY_CANCELLED,
            "action_already_cancelled",
        ),
    ],
)
def test_an_action_the_run_refuses_answers_its_protocol_error(
    tmp_path: Path,
    request_: CancelAttemptRequest | CancelItemRequest,
    reason: RefusalReason,
    code: str,
) -> None:
    parts = build_server_parts(tmp_path)
    parts.operator_control.check_requests(lambda _request: reason)

    with pytest.raises(RuntimeError) as refused:
        parts.api.execute(ControlCommand(timestamp=REQUEST_TIME, control=request_))

    assert getattr(refused.value, "diagnostic_code", None) == code
    assert parts.control.take_pending_actions() == []


def test_an_action_is_journaled_pending_then_applied(tmp_path: Path) -> None:
    """An accepted action is journaled pending; once core records it, applied, for good.

    A socket action whose run died before taking it keeps only its pending entry, so the
    journal tells it from one the run applied.
    """
    parts = build_server_parts(tmp_path)
    limit = SetMaxParallelRequest(control_id="l1", max_parallel=2)
    parts.api.execute(ControlCommand(timestamp=REQUEST_TIME, control=limit))
    parts.control.notify_action_applied("set_max_parallel 2")

    controls = [
        (event.text, event.status)
        for event in parts.journal.read()
        if event.type is EventType.CONTROL
    ]
    assert controls == [
        ("/set_max_parallel 2", EventStatus.PENDING),
        ("/set_max_parallel 2 applied", EventStatus.COMPLETED),
    ]
    assert parts.control.take_pending_actions() == [limit]


def test_standing_guidance_is_journaled_in_force_then_ended(tmp_path: Path) -> None:
    """Guidance is journaled active, not waiting for a turn; its withdrawal pending, its end done.

    The withdrawal's entry stays pending: the end of the guidance is journaled as its own entry.
    """
    parts = build_server_parts(tmp_path)
    steer = SteerRequest(control_id="keep", text="keep bf16", mode=SteerMode.STANDING)
    parts.api.execute(ControlCommand(timestamp=REQUEST_TIME, control=steer))
    withdraw = WithdrawRequest(control_id="w1", withdraws="keep")
    parts.api.execute(ControlCommand(timestamp=REQUEST_TIME, control=withdraw))
    parts.control.notify_guidance_ended(
        "standing guidance keep ended: withdrawn", why="withdrawn", control_id="keep"
    )

    controls = [
        (event.text, event.status)
        for event in parts.journal.read()
        if event.type is EventType.CONTROL
    ]
    assert controls == [
        ("/steer --standing: keep bf16", EventStatus.ACTIVE),
        ("/withdraw: keep", EventStatus.PENDING),
        ("/guidance ended: standing guidance keep ended: withdrawn", EventStatus.COMPLETED),
    ]


def test_an_inbox_request_the_run_set_aside_is_journaled_as_failed(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)

    parts.control.notify_request_rejected("w-1", "it ended nothing: no standing steer w-0")

    controls = [
        (event.text, event.status)
        for event in parts.journal.read()
        if event.type is EventType.CONTROL
    ]
    assert controls == [
        (
            "/control request w-1 rejected: it ended nothing: no standing steer w-0",
            EventStatus.FAILED,
        )
    ]


def test_each_steer_step_reaches_the_wire_as_a_typed_event_with_the_control_id(
    tmp_path: Path,
) -> None:
    parts = build_server_parts(tmp_path)
    steer = SteerRequest(control_id="keep", text="keep bf16", mode=SteerMode.STANDING)
    parts.api.execute(ControlCommand(timestamp=REQUEST_TIME, control=steer))
    withdraw = WithdrawRequest(control_id="w1", withdraws="keep")
    parts.api.execute(ControlCommand(timestamp=REQUEST_TIME, control=withdraw))
    parts.control.notify_steer_consumed(
        SteerTurn(
            role="implementer",
            round_label="attempt-3",
            execution_id="exec-1",
            attempt_id="attempt-3",
        ),
        control_id="s2",
    )
    parts.control.notify_guidance_ended(
        "standing guidance keep ended: withdrawn", why="withdrawn", control_id="keep"
    )
    parts.control.notify_control_refused(
        "steer s9: the run is ending",
        why="the run is ending",
        action=ControlAction.STEER,
        control_id="s9",
    )
    parts.control.notify_control_refused(
        "pause p1: cleanup must finish",
        why="cleanup must finish",
        action=ControlAction.PAUSE,
        control_id="p1",
    )

    steps = [
        (event.data.delivery.state, event.data.delivery.control_id)
        for event in parts.journal.read()
        if event.type is EventType.STEER_DELIVERY and isinstance(event.data, SteerDeliveryData)
    ]
    # The refused pause is no steer step; every other transition is one, in order.
    assert steps == [
        (SteerDeliveryState.QUEUED, "keep"),
        (SteerDeliveryState.WITHDRAW_QUEUED, "w1"),
        (SteerDeliveryState.REACHED, "s2"),
        (SteerDeliveryState.ENDED, "keep"),
        (SteerDeliveryState.REFUSED, "s9"),
    ]
    (reached,) = [
        event
        for event in parts.journal.read()
        if isinstance(event.data, SteerDeliveryData)
        and event.data.delivery.state is SteerDeliveryState.REACHED
    ]
    assert isinstance(reached.data, SteerDeliveryData)
    assert (reached.agent_kind, reached.execution_id) == ("implementer", "exec-1")
    assert reached.data.delivery.attempt_id == "attempt-3"


def test_pause_now_uses_the_core_queue_instead_of_the_host_shutdown_flag(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    request = PauseNowRequest(control_id="immediate-pause")
    response = parts.api.execute(ControlCommand(timestamp=REQUEST_TIME, control=request))
    assert response.ack is not None
    assert response.ack.action is ControlAction.PAUSE_NOW
    assert not parts.control.stop_requested()
    assert parts.control.take_pending_actions() == [request]
