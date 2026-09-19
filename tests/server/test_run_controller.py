"""Pause, stop, steering, and terminal-state tests for the run controller."""

import threading
import time

import pytest
from tests.server.support import ServerParts, build_server_parts

from server.controller import RunStopped
from server.run_lifecycle import RunStatus
from server.wire import enums, messages
from server.wire.v2 import common_pb2, events_pb2, responses_pb2
from vibesys.events import CoreEventType
from vibesys.events import EventStatus as CoreEventStatus

EventType = events_pb2.EventType
EventStatus = events_pb2.EventStatus


def _status(value: int) -> RunStatus:
    return enums.member(RunStatus, common_pb2.RunStatus, value)


def _snapshot_status(parts: ServerParts) -> RunStatus:
    return _status(parts.api.snapshot().status)


def _status_changes(parts: ServerParts) -> list[tuple[RunStatus, RunStatus]]:
    """Return every published transition as ``(previous, status)``."""
    return [
        (_status(event.run_status_changed.previous), _status(event.run_status_changed.status))
        for event in parts.journal.read()
        if event.WhichOneof("data") == "run_status_changed"
    ]


def _folded_status(events: list[events_pb2.RunEvent], through_sequence: int) -> RunStatus | None:
    """Fold the published transitions the way a client does."""
    folded: RunStatus | None = None
    for event in events:
        if event.sequence > through_sequence:
            break
        if event.WhichOneof("data") == "run_status_changed":
            folded = _status(event.run_status_changed.status)
    return folded


def test_pause_takes_effect_at_next_safe_point(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round 1", "work")
    parts.controller.pause_after_call()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)

    result: list[str] = []
    waiter = threading.Thread(
        target=lambda: result.append(parts.controller.before_agent("judge", "round 1", "prompt"))
    )
    waiter.start()
    time.sleep(0.02)
    assert waiter.is_alive()
    parts.controller.resume()
    waiter.join(timeout=1)
    assert result == ["prompt"]


def test_steering_is_injected_once(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.controller.steer("focus on the KV cache")

    effective = parts.controller.before_agent("implementer", "round 1", "Do the work")

    assert "Do the work" in effective
    assert "focus on the KV cache" in effective
    assert "Operator steering" in effective
    started = next(
        event
        for event in parts.journal.read()
        if event.type == EventType.EVENT_TYPE_AGENT_EXECUTION_STARTED
    )
    assert started.agent_execution_started.user_prompt == effective

    parts.controller.after_agent("implementer", "round 1")
    assert parts.controller.before_agent("judge", "round 1", "Review it") == "Review it"


def test_steering_queued_while_paused_applies_on_resume(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round 1", "work")
    parts.controller.pause_after_call()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)

    result: list[str] = []
    waiter = threading.Thread(
        target=lambda: result.append(parts.controller.before_agent("judge", "round 1", "Review"))
    )
    waiter.start()
    time.sleep(0.02)
    parts.controller.steer("check for reward hacking")
    parts.controller.resume()
    waiter.join(timeout=1)

    assert len(result) == 1
    assert "Review" in result[0]
    assert "check for reward hacking" in result[0]


def test_api_control_commands_ack_and_reach_controller(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)

    pause = parts.api.execute(messages.make_request("pause"))
    resume = parts.api.execute(messages.make_request("resume"))
    steer = parts.api.execute(messages.make_request("steer", text="prioritize latency"))

    assert pause.HasField("ack")
    assert resume.HasField("ack")
    assert steer.HasField("ack")
    action = responses_pb2.CommandAction
    ack_status = responses_pb2.CommandAckStatus
    assert (pause.ack.action, pause.ack.status) == (
        action.COMMAND_ACTION_PAUSE,
        ack_status.COMMAND_ACK_STATUS_PENDING,
    )
    assert (resume.ack.action, resume.ack.status) == (
        action.COMMAND_ACTION_RESUME,
        ack_status.COMMAND_ACK_STATUS_CONSUMED,
    )
    assert (steer.ack.action, steer.ack.status) == (
        action.COMMAND_ACTION_STEER,
        ack_status.COMMAND_ACK_STATUS_PENDING,
    )
    assert "prioritize latency" in parts.controller.before_agent("implementer", "round 1", "Work")


def test_finish_is_idempotent_and_interrupts_controlled_executions(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round 1", "work")

    parts.controller.finish(RuntimeError("first failure"))
    parts.controller.finish(RuntimeError("second failure"))

    failed = [
        event for event in parts.journal.read() if event.type == EventType.EVENT_TYPE_RUN_FAILED
    ]
    assert len(failed) == 1
    assert failed[0].HasField("diagnostic")
    assert failed[0].diagnostic.detail == "RuntimeError: first failure"
    finished = next(
        event
        for event in parts.journal.read()
        if event.type == EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED
        and event.execution_id == execution.execution_id
    )
    assert finished.status == EventStatus.EVENT_STATUS_INTERRUPTED


def test_pause_is_pending_until_the_invocation_boundary(tmp_path):  # noqa: ANN001, ANN201
    """`/pause` is a request: the call in flight keeps running until it ends."""
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round 1", "work")

    parts.api.execute(messages.make_request("pause"))

    assert _snapshot_status(parts) is RunStatus.PAUSING
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)
    assert _snapshot_status(parts) is RunStatus.PAUSED


def test_every_transition_publishes_exactly_one_status_event(tmp_path):  # noqa: ANN001, ANN201
    """The status a client folds is the status the controller holds."""
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round 1", "work")
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
        if event.type == EventType.EVENT_TYPE_CONTROL
    ]
    assert controls == [
        ("/pause", EventStatus.EVENT_STATUS_PENDING),
        ("/pause", EventStatus.EVENT_STATUS_PENDING),
        ("/pause", EventStatus.EVENT_STATUS_CONSUMED),
        ("/resume", EventStatus.EVENT_STATUS_CONSUMED),
    ]


def test_resume_before_the_boundary_cancels_the_pending_pause(tmp_path):  # noqa: ANN001, ANN201
    """A resume that beats the boundary leaves no pause to apply later."""
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round 1", "work")
    parts.controller.pause_after_call()
    parts.controller.resume()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)

    assert _snapshot_status(parts) is RunStatus.RUNNING
    assert _status_changes(parts) == [
        (RunStatus.STARTING, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.PAUSING),
        (RunStatus.PAUSING, RunStatus.RUNNING),
    ]


def test_finish_ends_a_paused_run_and_releases_the_pause_wait(tmp_path):  # noqa: ANN001, ANN201
    """A run that ends while paused reports the ended status, not `paused`."""
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round 1", "work")
    parts.controller.pause_after_call()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)
    assert _snapshot_status(parts) is RunStatus.PAUSED

    entered: list[str] = []
    waiter = threading.Thread(
        target=lambda: entered.append(parts.controller.before_agent("judge", "round 1", "review"))
    )
    waiter.start()
    time.sleep(0.02)
    assert waiter.is_alive()

    parts.controller.finish()

    waiter.join(timeout=1)
    assert entered == ["review"]
    assert _snapshot_status(parts) is RunStatus.COMPLETED
    assert _status_changes(parts)[-1] == (RunStatus.PAUSED, RunStatus.COMPLETED)


def test_pause_applies_without_a_matching_execution(tmp_path):  # noqa: ANN001, ANN201
    """The compatibility boundary is still a boundary, and still publishes."""
    parts = build_server_parts(tmp_path)
    parts.controller.pause_after_call()

    parts.controller.after_agent("implementer", "round 1")

    assert _snapshot_status(parts) is RunStatus.PAUSED
    assert _status_changes(parts)[-1] == (RunStatus.PAUSING, RunStatus.PAUSED)


def test_snapshot_status_agrees_with_the_fold_at_the_terminal_event(tmp_path):  # noqa: ANN001, ANN201
    """No sequence containing the terminal event can still read as running."""
    parts = build_server_parts(tmp_path)
    parts.integration.events.emit(CoreEventType.RUN_FINISHED, status=CoreEventStatus.COMPLETED)

    snapshot = parts.api.snapshot()
    assert _status(snapshot.status) is RunStatus.COMPLETED
    events = parts.journal.read()
    terminal = next(event for event in events if event.type == EventType.EVENT_TYPE_RUN_FINISHED)
    assert _folded_status(events, terminal.sequence) is RunStatus.COMPLETED
    assert _folded_status(events, snapshot.sequence) is _status(snapshot.status)
    # The run ended once: the later `finish` from the runtime adds nothing.
    parts.controller.finish()
    assert [event.type for event in parts.journal.read()].count(
        EventType.EVENT_TYPE_RUN_FINISHED
    ) == 1


def test_stop_is_pending_until_the_invocation_boundary(tmp_path):  # noqa: ANN001, ANN201
    """`/stop` is a request: the call in flight keeps running until it ends."""
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round 1", "work")

    stop = parts.api.execute(messages.make_request("stop"))

    assert stop.HasField("ack")
    assert (stop.ack.action, stop.ack.status) == (
        responses_pb2.CommandAction.COMMAND_ACTION_STOP,
        responses_pb2.CommandAckStatus.COMMAND_ACK_STATUS_PENDING,
    )
    assert _snapshot_status(parts) is RunStatus.STOPPING
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)
    assert _snapshot_status(parts) is RunStatus.STOPPED
    assert _status_changes(parts) == [
        (RunStatus.STARTING, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.STOPPING),
        (RunStatus.STOPPING, RunStatus.STOPPED),
    ]
    controls = [
        (event.text, event.status)
        for event in parts.journal.read()
        if event.type == EventType.EVENT_TYPE_CONTROL
    ]
    assert controls == [
        ("/stop", EventStatus.EVENT_STATUS_PENDING),
        ("/stop", EventStatus.EVENT_STATUS_CONSUMED),
    ]


def test_stopped_run_refuses_the_next_controlled_invocation(tmp_path):  # noqa: ANN001, ANN201
    """After the stop lands, entering the next boundary unwinds the run."""
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round 1", "work")
    parts.controller.stop_after_call()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)

    with pytest.raises(RunStopped):
        parts.controller.before_agent("judge", "round 1", "review")


def test_stop_requested_between_invocations_starts_no_further_call(tmp_path):  # noqa: ANN001, ANN201
    """The entry side of the boundary is a boundary: nothing else starts."""
    parts = build_server_parts(tmp_path)
    parts.controller.stop_after_call()

    with pytest.raises(RunStopped):
        parts.controller.before_agent("implementer", "round 1", "work")

    assert _snapshot_status(parts) is RunStatus.STOPPED
    assert _status_changes(parts)[-1] == (RunStatus.STOPPING, RunStatus.STOPPED)
    assert not any(
        event.type == EventType.EVENT_TYPE_AGENT_EXECUTION_STARTED for event in parts.journal.read()
    )


def test_stop_releases_the_pause_wait_and_ends_the_run(tmp_path):  # noqa: ANN001, ANN201
    """A stop from `paused` wakes the parked thread and ends without a call."""
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round 1", "work")
    parts.controller.pause_after_call()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)
    assert _snapshot_status(parts) is RunStatus.PAUSED

    raised: list[BaseException] = []

    def wait_at_boundary() -> None:
        try:
            parts.controller.before_agent("judge", "round 1", "review")
        except BaseException as error:  # noqa: BLE001  # The unwind signal is the assertion.
            raised.append(error)

    waiter = threading.Thread(target=wait_at_boundary)
    waiter.start()
    time.sleep(0.02)
    assert waiter.is_alive()

    parts.controller.stop_after_call()

    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert [type(error) for error in raised] == [RunStopped]
    assert _snapshot_status(parts) is RunStatus.STOPPED
    assert _status_changes(parts)[-2:] == [
        (RunStatus.PAUSED, RunStatus.STOPPING),
        (RunStatus.STOPPING, RunStatus.STOPPED),
    ]


def test_resume_before_the_boundary_cancels_the_pending_stop(tmp_path):  # noqa: ANN001, ANN201
    """A resume that beats the stop boundary keeps the run going."""
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round 1", "work")
    parts.controller.stop_after_call()
    parts.controller.resume()
    parts.controller.after_agent("implementer", "round 1", execution_id=execution.execution_id)

    assert _snapshot_status(parts) is RunStatus.RUNNING
    assert parts.controller.before_agent("judge", "round 1", "review") == "review"


def test_finish_after_a_landed_stop_records_no_terminal_event(tmp_path):  # noqa: ANN001, ANN201
    """The terminal status change is the stop's terminal record."""
    parts = build_server_parts(tmp_path)
    parts.controller.stop_after_call()
    with pytest.raises(RunStopped):
        parts.controller.before_agent("implementer", "round 1", "work")

    parts.controller.finish()

    assert not any(
        event.type in (EventType.EVENT_TYPE_RUN_FINISHED, EventType.EVENT_TYPE_RUN_FAILED)
        for event in parts.journal.read()
    )
    assert _status_changes(parts)[-1] == (RunStatus.STOPPING, RunStatus.STOPPED)
    assert _snapshot_status(parts) is RunStatus.STOPPED


def test_stop_does_not_block_presentation_only_chat(tmp_path):  # noqa: ANN001, ANN201
    """Chat stays available on a stopped run, exactly as on a finished one."""
    parts = build_server_parts(tmp_path)
    parts.controller.stop_after_call()
    with pytest.raises(RunStopped):
        parts.controller.before_agent("implementer", "round 1", "work")

    execution = parts.controller.start_agent_execution(
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
        if event.type == EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED
        and event.execution_id == execution.execution_id
    ]
    assert len(finished) == 1
    assert finished[0].status == EventStatus.EVENT_STATUS_COMPLETED
    assert _snapshot_status(parts) is RunStatus.STOPPED


def test_finish_does_not_interrupt_presentation_only_chat(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution(
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
        if event.type == EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED
        and event.execution_id == execution.execution_id
    ]
    assert len(finished) == 1
    assert finished[0].status == EventStatus.EVENT_STATUS_COMPLETED
