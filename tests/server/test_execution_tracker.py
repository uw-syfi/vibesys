"""Contracts for live execution tracking and lifecycle projection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from tests.server.support import build_server_parts

if TYPE_CHECKING:
    from pathlib import Path

from server.events import (
    AgentExecutionActivityData,
    AgentExecutionStartedData,
    EventStatus,
    EventStore,
    EventType,
    InvocationFinishedData,
    InvocationStartedData,
    RunEvent,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
)


def test_explicit_executions_are_independent_and_finish_idempotently(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    first = parts.controller.start_agent_execution("implementer", "round-1", "first")
    second = parts.controller.start_agent_execution("implementer", "round-1-retry-2", "second")

    assert {item.execution_id for item in parts.api.snapshot().active_executions} == {
        first.execution_id,
        second.execution_id,
    }
    parts.controller.after_agent(
        "implementer", "round-1", result="done", execution_id=first.execution_id
    )
    parts.controller.after_agent(
        "implementer", "round-1", result="ignored", execution_id=first.execution_id
    )
    assert [item.execution_id for item in parts.api.snapshot().active_executions] == [
        second.execution_id
    ]

    events = parts.journal.read()
    assert sum(event.type is EventType.AGENT_EXECUTION_STARTED for event in events) == 2
    assert sum(event.type is EventType.AGENT_EXECUTION_FINISHED for event in events) == 1
    assert not {EventType.INVOCATION_STARTED, EventType.INVOCATION_FINISHED}.intersection(
        event.type for event in events
    )
    assert sum(event.type is EventType.PHASE_STARTED for event in events) == 2


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        (
            {
                "driver": "agentshim",
                "provider": "codex",
                "model": "gpt-5.1-codex-max",
            },
            ("agentshim", "codex", "gpt-5.1-codex-max"),
        ),
        ({}, (None, None, None)),
    ],
)
def test_execution_identity_is_recorded_in_events_and_checkpoints(
    tmp_path: Path,
    identity: dict[str, str],
    expected: tuple[str | None, str | None, str | None],
) -> None:
    parts = build_server_parts(tmp_path)
    parts.controller.start_agent_execution(
        "implementer",
        "round-1",
        "work",
        driver=identity.get("driver"),
        provider=identity.get("provider"),
        model=identity.get("model"),
    )

    started = next(
        event for event in parts.journal.read() if event.type is EventType.AGENT_EXECUTION_STARTED
    )
    assert isinstance(started.data, AgentExecutionStartedData)
    assert (started.data.driver, started.data.provider, started.data.model) == expected
    active = parts.api.snapshot().active_executions
    assert (active[0].driver, active[0].provider, active[0].model) == expected
    checkpointed = parts.api.subscription_checkpoint(0).active_executions
    assert (checkpointed[0].driver, checkpointed[0].provider, checkpointed[0].model) == expected


def test_activity_tracks_todos_and_parallel_tools(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round-1", "work")
    publish = parts.executions.publish_presentation
    publish(
        EventType.TODO_UPDATE,
        TodoUpdateData(todos=[TodoItemData(content="Run queue tests", status="in_progress")]),
        invocation_id=execution.execution_id,
    )
    publish(
        EventType.TOOL_CALL,
        ToolCallData(tool="Bash", args={}),
        invocation_id=execution.execution_id,
    )
    publish(
        EventType.TOOL_CALL,
        ToolCallData(tool="Read", args={}),
        invocation_id=execution.execution_id,
    )
    publish(
        EventType.TOOL_RESULT,
        ToolResultData(tool="Read", content="ok"),
        invocation_id=execution.execution_id,
    )
    assert parts.api.snapshot().active_executions[0].activity.tool == "Bash"

    publish(
        EventType.TOOL_RESULT,
        ToolResultData(tool="Bash", content="ok"),
        invocation_id=execution.execution_id,
    )
    assert parts.api.snapshot().active_executions[0].activity == AgentExecutionActivityData(
        mode="thinking", summary="Run queue tests"
    )


@pytest.mark.parametrize("terminal_todo_status", ["pending", "completed"])
def test_terminal_todo_clears_stale_summary(tmp_path, terminal_todo_status):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round-1", "work")
    parts.executions.publish_presentation(
        EventType.TODO_UPDATE,
        TodoUpdateData(todos=[TodoItemData(content="Run tests", status="in_progress")]),
        invocation_id=execution.execution_id,
    )
    parts.executions.publish_presentation(
        EventType.TODO_UPDATE,
        TodoUpdateData(todos=[TodoItemData(content="Run tests", status=terminal_todo_status)]),
        invocation_id=execution.execution_id,
    )
    assert parts.api.snapshot().active_executions[0].activity == AgentExecutionActivityData(
        mode="thinking", summary="Thinking"
    )


def test_terminal_todo_preserves_active_tool(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round-1", "work")
    parts.executions.publish_presentation(
        EventType.TOOL_CALL,
        ToolCallData(tool="Bash", args={}),
        invocation_id=execution.execution_id,
    )
    parts.executions.publish_presentation(
        EventType.TODO_UPDATE,
        TodoUpdateData(todos=[TodoItemData(content="Run tests", status="completed")]),
        invocation_id=execution.execution_id,
    )
    assert parts.api.snapshot().active_executions[0].activity == AgentExecutionActivityData(
        mode="tool", summary="Using Bash", tool="Bash"
    )


def test_checkpoint_watermark_and_active_state_are_consistent(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("judge", "round-2", "review")

    checkpoint = parts.api.subscription_checkpoint(0)
    assert all(event.sequence <= checkpoint.through_sequence for event in checkpoint.events)
    active = checkpoint.active_executions
    assert [item.execution_id for item in active] == [execution.execution_id]
    assert active[0].activity.summary == "Reviewing"
    started = next(
        event for event in checkpoint.events if event.type is EventType.AGENT_EXECUTION_STARTED
    )
    assert isinstance(started.data, AgentExecutionStartedData)
    assert started.data.activity == active[0].activity

    parts.controller.after_agent("judge", "round-2", execution_id=execution.execution_id)
    checkpoint = parts.api.subscription_checkpoint(checkpoint.through_sequence)
    assert checkpoint.events[-1].sequence == checkpoint.through_sequence
    assert checkpoint.active_executions == []


def test_attach_merges_bootstrap_and_durable_execution_history(tmp_path):  # noqa: ANN001, ANN201
    durable_dir = tmp_path / "durable"
    durable_dir.mkdir()
    execution_id = "a" * 32
    durable = EventStore(durable_dir / "run-events.jsonl", "run-1")
    durable.append(
        RunEvent(
            timestamp=datetime.now(UTC),
            type=EventType.INVOCATION_STARTED,
            status=EventStatus.ACTIVE,
            agent_kind="implementer",
            round_label="round-1-implementer",
            invocation_id=execution_id,
            data=InvocationStartedData(system_prompt="system", user_prompt="prior work"),
        )
    )

    parts = build_server_parts(tmp_path / "bootstrap")
    parts.executions.publish_agent_output(
        "bootstrap work", agent_kind="orchestrator", round_label="round-2-plan"
    )
    parts.attach(durable_dir)
    parts.executions.publish_agent_output(
        "current work", agent_kind="implementer", round_label="round-2-implementer"
    )

    checkpoint = parts.api.subscription_checkpoint(0)
    events = checkpoint.events
    assert [event.sequence for event in events] == list(range(1, checkpoint.through_sequence + 1))
    assert any(
        event.type is EventType.AGENT_EXECUTION_STARTED and event.execution_id == execution_id
        for event in events
    )
    assert [
        event.data.content
        for event in events
        if event.data is not None and event.data.kind == "agent_output_chunk"
    ] == ["bootstrap work", "current work"]


def test_streamed_text_does_not_override_active_tool(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    execution = parts.controller.start_agent_execution("implementer", "round-1", "work")
    parts.executions.publish_presentation(
        EventType.TOOL_CALL,
        ToolCallData(tool="Bash", args={}),
        invocation_id=execution.execution_id,
    )
    parts.executions.publish_agent_output("still working", invocation_id=execution.execution_id)
    assert parts.api.snapshot().active_executions[0].activity == AgentExecutionActivityData(
        mode="tool", summary="Using Bash", tool="Bash"
    )


def test_chat_execution_is_isolated_from_run_control(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    main = parts.controller.start_agent_execution("implementer", "round-1", "work")
    parts.controller.pause_after_call()
    chat = parts.controller.start_agent_execution(
        "chat",
        "experiment-chat",
        "status?",
        consume_steering=False,
        participates_in_run_control=False,
    )

    assert parts.api.snapshot().agent_kind == "implementer"
    parts.controller.after_agent("chat", "experiment-chat", execution_id=chat.execution_id)
    # A presentation-only execution is not a run-control boundary, so the
    # pending pause is still pending rather than applied.
    assert parts.api.snapshot().status == "pausing"
    parts.controller.after_agent("implementer", "round-1", execution_id=main.execution_id)
    assert parts.api.snapshot().status == "paused"

    paused_chat = parts.controller.start_agent_execution(
        "chat",
        "experiment-chat",
        "status?",
        consume_steering=False,
        participates_in_run_control=False,
    )
    parts.controller.after_agent("chat", "experiment-chat", execution_id=paused_chat.execution_id)
    assert parts.api.snapshot().status == "paused"


def test_cancellation_and_run_finish_terminalize_activity(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    cancelled = parts.controller.start_agent_execution("implementer", "round-1", "work")
    parts.controller.after_agent(
        "implementer",
        "round-1",
        error=asyncio.CancelledError(),
        execution_id=cancelled.execution_id,
    )
    dangling = parts.controller.start_agent_execution("judge", "round-1", "review")
    parts.controller.finish()

    terminal = {
        event.execution_id: event.status
        for event in parts.journal.read()
        if event.type is EventType.AGENT_EXECUTION_FINISHED
    }
    assert terminal[cancelled.execution_id] is EventStatus.CANCELLED
    assert terminal[dangling.execution_id] is EventStatus.INTERRUPTED
    assert parts.api.snapshot().active_executions == []


def test_legacy_invocations_project_without_becoming_live(tmp_path):  # noqa: ANN001, ANN201
    execution_id = "a" * 32
    store = EventStore(tmp_path / "run-events.jsonl", "legacy")
    store.append(
        RunEvent(
            timestamp=datetime.now(UTC),
            type=EventType.INVOCATION_STARTED,
            status=EventStatus.ACTIVE,
            agent_kind="implementer",
            round_label="round-1",
            invocation_id=execution_id,
            data=InvocationStartedData(system_prompt="system", user_prompt="work"),
        )
    )
    parts = build_server_parts(tmp_path)

    event = next(
        event for event in parts.journal.read() if event.type is EventType.AGENT_EXECUTION_STARTED
    )
    assert event.execution_id == execution_id
    assert parts.api.snapshot().active_executions == []

    assert parts.journal._store is not None  # noqa: SLF001
    parts.journal._store.append(  # noqa: SLF001
        RunEvent(
            timestamp=datetime.now(UTC),
            type=EventType.INVOCATION_FINISHED,
            status=EventStatus.COMPLETED,
            agent_kind="implementer",
            round_label="round-1",
            invocation_id=execution_id,
            data=InvocationFinishedData(result="done"),
        )
    )
    assert parts.journal.read()[-1].type is EventType.AGENT_EXECUTION_FINISHED


def test_failed_lifecycle_append_does_not_advance_active_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = build_server_parts(tmp_path)
    store = parts.journal._store  # noqa: SLF001
    assert store is not None
    append = store.append

    def fail_start(event):  # noqa: ANN001, ANN202
        if event.type is EventType.AGENT_EXECUTION_STARTED:
            raise OSError("disk full")  # noqa: TRY003
        return append(event)

    monkeypatch.setattr(store, "append", fail_start)
    with pytest.raises(OSError, match="disk full"):
        parts.controller.start_agent_execution("implementer", "round-1", "work")
    assert parts.api.snapshot().active_executions == []

    monkeypatch.setattr(store, "append", append)
    execution = parts.controller.start_agent_execution("implementer", "round-1", "work")

    def fail_finish(event):  # noqa: ANN001, ANN202
        if event.type is EventType.AGENT_EXECUTION_FINISHED:
            raise OSError("disk full")  # noqa: TRY003
        return append(event)

    monkeypatch.setattr(store, "append", fail_finish)
    with pytest.raises(OSError, match="disk full"):
        parts.controller.after_agent("implementer", "round-1", execution_id=execution.execution_id)
    assert [item.execution_id for item in parts.api.snapshot().active_executions] == [
        execution.execution_id
    ]
