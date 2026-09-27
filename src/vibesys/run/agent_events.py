"""Translate agent callbacks into presentation-neutral core events."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from vibesys.events import (
    AgentOutputChannel,
    AgentOutputChunkData,
    AgentStatusData,
    CoreEvent,
    CoreEventType,
    JsonResultPayload,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    ToolResultPayload,
    UsageUpdateData,
    make_core_event,
)

EventSink = Callable[[CoreEvent], object]


def _json_safe(args: dict[str, Any]) -> dict[str, Any]:
    """Coerce tool arguments to a JSON-serializable dictionary."""
    return json.loads(json.dumps(args, default=repr))


def _classify_tool_result(content: str) -> ToolResultPayload | None:
    """Preserve JSON-shaped tool results alongside their raw text."""
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(value, (dict, list)):
        return JsonResultPayload(value=value)
    return None


class CoreAgentEventSink:
    """Map one run's public agent callbacks to typed core events.

    The injected sink owns delivery, persistence, and lifecycle. This adapter
    only translates one callback into one event and retains no subscribers or
    process-global state.
    """

    def __init__(self, sink: EventSink) -> None:
        """Create an adapter that sends every translated event to ``sink``."""
        self._sink = sink

    def _emit(
        self,
        event_type: CoreEventType,
        data: AgentOutputChunkData
        | ToolCallData
        | ToolResultData
        | TodoUpdateData
        | UsageUpdateData,
        *,
        agent_kind: str | None,
        round_label: str | None,
        invocation_id: str | None,
    ) -> None:
        self._sink(
            make_core_event(
                event_type,
                data=data,
                agent_kind=agent_kind,
                round_label=round_label,
                execution_id=invocation_id,
            )
        )

    def agent_output(  # noqa: PLR0913  # lint-waiver: LW-011214 [PLR0913]; This implements AgentEventSink's public named-argument contract exactly.
        self,
        content: str,
        *,
        channel: AgentOutputChannel = "assistant",
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Emit a non-empty agent output chunk with routing metadata."""
        if not content:
            return
        self._emit(
            CoreEventType.AGENT_OUTPUT_CHUNK,
            AgentOutputChunkData(channel=channel, content=content, status=status),
            agent_kind=agent_kind,
            round_label=round_label,
            invocation_id=invocation_id,
        )

    def tool_call(  # noqa: PLR0913  # lint-waiver: LW-011215 [PLR0913]; This implements AgentEventSink's public named-argument contract exactly.
        self,
        tool: str,
        args: dict[str, Any],
        *,
        call_id: str | None = None,
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Emit a tool call with JSON-safe arguments and routing metadata."""
        self._emit(
            CoreEventType.TOOL_CALL,
            ToolCallData(tool=tool, call_id=call_id, args=_json_safe(args), status=status),
            agent_kind=agent_kind,
            round_label=round_label,
            invocation_id=invocation_id,
        )

    def tool_result(  # noqa: PLR0913  # lint-waiver: LW-011216 [PLR0913]; This implements AgentEventSink's public named-argument contract exactly.
        self,
        tool: str,
        content: str,
        *,
        call_id: str | None = None,
        is_error: bool = False,
        payload: ToolResultPayload | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Emit a tool result, classifying JSON content without a payload."""
        resolved_payload = payload if payload is not None else _classify_tool_result(content)
        self._emit(
            CoreEventType.TOOL_RESULT,
            ToolResultData(
                tool=tool,
                call_id=call_id,
                content=content,
                is_error=is_error,
                payload=resolved_payload,
            ),
            agent_kind=agent_kind,
            round_label=round_label,
            invocation_id=invocation_id,
        )

    def todo_update(
        self,
        todos: list[TodoItemData],
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Emit a non-empty todo update with routing metadata."""
        if not todos:
            return
        self._emit(
            CoreEventType.TODO_UPDATE,
            TodoUpdateData(todos=todos),
            agent_kind=agent_kind,
            round_label=round_label,
            invocation_id=invocation_id,
        )

    def usage_update(  # noqa: PLR0913  # lint-waiver: LW-011217 [PLR0913]; This implements AgentEventSink's public named-argument contract exactly.
        self,
        input_tokens: int,
        *,
        context_window: int | None = None,
        model: str | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Emit token usage and optional model context metadata."""
        self._emit(
            CoreEventType.USAGE_UPDATE,
            UsageUpdateData(
                input_tokens=input_tokens,
                context_window=context_window,
                model=model,
            ),
            agent_kind=agent_kind,
            round_label=round_label,
            invocation_id=invocation_id,
        )
