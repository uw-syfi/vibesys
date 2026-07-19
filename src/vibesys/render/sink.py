"""The single emission point for presentation events.

Emission sites (agent callbacks, runner helpers, ``lprint``) call the
process-global :class:`OutputSink` unconditionally — headless or TUI. The
sink forwards each event to the active :class:`~vibesys.server.supervisor.
RunSupervisor` (when a TUI/supervision client is attached) and to any
in-process subscribers (the headless renderer). This is the only place
allowed to consult :func:`~vibesys.server.registry.active_supervisor`;
call sites never branch on presentation mode.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from vibesys.server.events import (
    AgentOutputChannel,
    AgentOutputChunkData,
    AgentStatusData,
    EventData,
    EventType,
    RunEvent,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
    make_event,
)

EventHandler = Callable[[RunEvent], None]


def _json_safe(args: dict[str, Any]) -> dict[str, Any]:
    """Coerce tool args to a JSON-serializable dict (events must serialize)."""
    return json.loads(json.dumps(args, default=repr))


class OutputSink:
    """Fan presentation events out to the supervisor and local subscribers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: tuple[EventHandler, ...] = ()

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Register *handler* for every emitted event; returns an unsubscriber."""
        with self._lock:
            self._subscribers = (*self._subscribers, handler)

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers = tuple(h for h in self._subscribers if h is not handler)

        return unsubscribe

    # -- semantic emitters ---------------------------------------------------

    def agent_output(
        self,
        content: str,
        *,
        channel: AgentOutputChannel = "assistant",
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
    ) -> None:
        if not content:
            return
        self._emit(
            EventType.AGENT_OUTPUT_CHUNK,
            AgentOutputChunkData(channel=channel, content=content, status=status),
            agent_kind=agent_kind,
        )

    def tool_call(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        status: AgentStatusData | None = None,
    ) -> None:
        self._emit(
            EventType.TOOL_CALL,
            ToolCallData(tool=tool, args=_json_safe(args), status=status),
        )

    def tool_result(self, tool: str, content: str, *, is_error: bool = False) -> None:
        self._emit(
            EventType.TOOL_RESULT,
            ToolResultData(tool=tool, content=content, is_error=is_error),
        )

    def todo_update(self, todos: list[TodoItemData]) -> None:
        if not todos:
            return
        self._emit(EventType.TODO_UPDATE, TodoUpdateData(todos=todos))

    def usage_update(
        self,
        input_tokens: int,
        *,
        context_window: int | None = None,
        model: str | None = None,
    ) -> None:
        self._emit(
            EventType.USAGE_UPDATE,
            UsageUpdateData(input_tokens=input_tokens, context_window=context_window, model=model),
        )

    # -- dispatch ------------------------------------------------------------

    def _emit(
        self,
        event_type: EventType,
        data: EventData,
        *,
        agent_kind: str | None = None,
    ) -> None:
        from vibesys.server.registry import active_supervisor

        supervisor = active_supervisor()
        if supervisor is not None:
            supervisor.publish_presentation(event_type, data, agent_kind=agent_kind)
        with self._lock:
            subscribers = self._subscribers
        if not subscribers:
            return
        event = make_event(event_type, agent_kind=agent_kind, data=data)
        for handler in subscribers:
            handler(event)


_SINK = OutputSink()


def output_sink() -> OutputSink:
    """Return the process-global presentation sink."""
    return _SINK
