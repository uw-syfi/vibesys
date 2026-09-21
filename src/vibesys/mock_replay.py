"""Convert a recorded run's core-event log into a mock driver replay playbook.

:mod:`vs_agent.drivers.mock` is a leaf module: it knows only the
driver's own ``AgentEvent`` vocabulary, never core's event envelope. Turning
a recorded ``run-events.jsonl`` (a stream of :class:`vibesys.events.CoreEvent`
records) back into driver events is therefore core's job, not the driver's.
This module owns that conversion and hands the driver a
:class:`~vs_agent.drivers.mock.ReplayPlaybook` that is already fully
converted and timed, so the driver itself never has to import
:mod:`vibesys.events`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from vibesys.events import (
    AgentOutputChunkData,
    CoreEvent,
    CoreEventType,
    EventPayload,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
)
from vs_agent.api import (
    AgentEvent,
    AgentEventKind,
    AgentUsage,
    MockDriverError,
    ReplayPlaybook,
    todos_from_tool_call,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

# Mirrors vs_agent.drivers.mock._TODO_TOOL: replay re-emits a recorded
# plan snapshot as the provider tool call behind it, so it needs the same
# tool name the driver's own scripted mode uses.
_TODO_TOOL = "TodoWrite"

_REPLAYABLE_EVENT_TYPES = frozenset(
    {
        CoreEventType.AGENT_OUTPUT_CHUNK,
        CoreEventType.TOOL_CALL,
        CoreEventType.TOOL_RESULT,
        CoreEventType.TODO_UPDATE,
        CoreEventType.USAGE_UPDATE,
    }
)

_REPLAYABLE_OUTPUT_CHANNELS: dict[str, AgentEventKind] = {
    "assistant": AgentEventKind.TEXT,
    "analysis": AgentEventKind.THINKING,
}


def build_replay_playbook(
    events_path: Path,
    *,
    speed: float = 0.0,
    max_gap_seconds: float = 1.0,
) -> ReplayPlaybook:
    """Read ``events_path`` and build a fully converted, fully timed playbook.

    ``speed`` multiplies the recorded inter-event gaps: ``0`` (the default)
    drops the gaps entirely and replays as fast as the consumer accepts
    events, ``1`` reproduces the original pacing, ``2`` runs twice as fast.
    ``max_gap_seconds`` caps any single reproduced gap, so one idle stretch
    cannot stall a test.
    """
    if speed < 0:
        raise MockDriverError(  # noqa: TRY003  # tracked: #288
            f"replay speed must be non-negative, got {speed}"
        )
    timed: list[tuple[AgentEvent, float]] = []
    previous: float | None = None
    todos_already_carried = False
    for event in _recorded_events(events_path):
        # A recorded todo snapshot that a recorded tool call already carries
        # is derived state: re-emitting both would duplicate it downstream,
        # because the tool call regenerates the snapshot on its own.
        if event.type is CoreEventType.TODO_UPDATE and todos_already_carried:
            todos_already_carried = False
            continue
        driver_event = _as_driver_event(event)
        if driver_event is None:
            continue
        todos_already_carried = _carries_todos(driver_event)
        recorded = event.timestamp.timestamp()
        gap = 0.0
        if speed > 0 and previous is not None:
            gap = min((recorded - previous) / speed, max_gap_seconds)
        previous = recorded
        timed.append((driver_event, gap))
    return ReplayPlaybook(events=tuple(timed))


def _carries_todos(event: AgentEvent) -> bool:
    """Whether downstream will derive a todo snapshot from this driver event."""
    if event.kind is not AgentEventKind.TOOL_CALL:
        return False
    args = event.payload.get("args")
    if not isinstance(args, dict):
        return False
    return todos_from_tool_call(str(event.payload.get("tool", "")), args) is not None


def _recorded_events(events_path: Path) -> Iterator[CoreEvent]:
    """Read one recorded run log, skipping records this fixture cannot use.

    A recording is test input, not a live contract: a truncated final line
    (the usual shape of a log captured from a killed run) is skipped rather
    than failing the replay.
    """
    if not events_path.is_file():
        raise MockDriverError(f"replay event log not found: {events_path}")  # noqa: TRY003  # tracked: #288
    with events_path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                event_type = CoreEventType(raw["type"])
                if event_type not in _REPLAYABLE_EVENT_TYPES:
                    continue
                yield CoreEvent.model_validate(
                    {
                        "sequence": raw.get("sequence", 0),
                        "run_id": raw.get("run_id", ""),
                        "timestamp": raw["timestamp"],
                        "type": event_type,
                        "text": raw.get("text", ""),
                        "status": raw.get("status"),
                        "round_label": raw.get("round_label"),
                        "agent_kind": raw.get("agent_kind"),
                        "execution_id": raw.get("execution_id") or raw.get("invocation_id"),
                        "data": raw.get("data"),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue


def _as_driver_event(event: CoreEvent) -> AgentEvent | None:
    """Project one recorded run event back onto the driver's event vocabulary."""
    if event.type not in _REPLAYABLE_EVENT_TYPES:
        return None
    data = event.data
    for payload_type, convert in _REPLAY_CONVERTERS:
        if isinstance(data, payload_type):
            return convert(data)
    return None


def _replay_output_chunk(data: AgentOutputChunkData) -> AgentEvent | None:
    """Only two channels come from a driver.

    ``diagnostic``, ``prompt``, and ``tool`` chunks in a recording were
    written by the client and runner above the driver; replaying them would
    push the client's own narration back through the driver contract.
    """
    kind = _REPLAYABLE_OUTPUT_CHANNELS.get(data.channel)
    return None if kind is None else AgentEvent(kind=kind, text=data.content)


def _replay_tool_call(data: ToolCallData) -> AgentEvent:
    return AgentEvent(
        kind=AgentEventKind.TOOL_CALL,
        payload={"tool": data.tool, "args": dict(data.args)},
    )


def _replay_todo_update(data: TodoUpdateData) -> AgentEvent:
    """Re-emit a recorded todo snapshot as the provider tool call behind it.

    The driver contract has no todo event; snapshots are derived downstream
    from a plan tool call, so that is the form replay has to use.
    """
    todos: list[dict[str, Any]] = [
        {"content": todo.content, "status": todo.status} for todo in data.todos
    ]
    return AgentEvent(
        kind=AgentEventKind.TOOL_CALL,
        payload={"tool": _TODO_TOOL, "args": {"todos": todos}},
    )


def _replay_tool_result(data: ToolResultData) -> AgentEvent:
    stdout = "" if data.is_error else data.content
    stderr = data.content if data.is_error else ""
    return AgentEvent(
        kind=AgentEventKind.TOOL_RESULT,
        text=data.content,
        payload={
            "tool": data.tool,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": 1 if data.is_error else 0,
            "duration": None,
            "result_payload": data.payload,
        },
    )


def _replay_usage_update(data: UsageUpdateData) -> AgentEvent:
    return AgentEvent(kind=AgentEventKind.USAGE, usage=AgentUsage(input_tokens=data.input_tokens))


_REPLAY_CONVERTERS: tuple[tuple[type[EventPayload], Callable[[Any], AgentEvent | None]], ...] = (
    (AgentOutputChunkData, _replay_output_chunk),
    (ToolCallData, _replay_tool_call),
    (TodoUpdateData, _replay_todo_update),
    (ToolResultData, _replay_tool_result),
    (UsageUpdateData, _replay_usage_update),
)
"""Recorded payload type to the driver event it was projected from."""
