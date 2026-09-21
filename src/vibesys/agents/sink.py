"""Injected event-emission seam for agent-package code.

Agent code (callbacks, the CLI client, the stub client) must not reach the
process-global :func:`vibesys.render.sink.output_sink` directly: that would
tie a leaf-bound package to the application's rendering composition. Instead,
callers construct agent services with an :class:`AgentEventSink` and agent
code publishes through the injected instance.

``vibesys.render.sink.OutputSink`` satisfies this protocol structurally (its
producer methods are mirrored here exactly), so wiring code can pass
``output_sink()`` without either module importing the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from vibesys.events import (
        AgentOutputChannel,
        AgentStatusData,
        TodoItemData,
        ToolResultPayload,
    )


@runtime_checkable
class AgentEventSink(Protocol):
    """The subset of ``OutputSink`` that agent-package code may call."""

    def agent_output(  # noqa: D102, PLR0913
        self,
        content: str,
        *,
        channel: AgentOutputChannel = "assistant",
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None: ...

    def tool_call(  # noqa: D102, PLR0913
        self,
        tool: str,
        args: dict[str, Any],
        *,
        call_id: str | None = None,
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None: ...

    def tool_result(  # noqa: D102, PLR0913
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
    ) -> None: ...

    def todo_update(  # noqa: D102
        self,
        todos: list[TodoItemData],
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None: ...

    def usage_update(  # noqa: D102, PLR0913
        self,
        input_tokens: int,
        *,
        context_window: int | None = None,
        model: str | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None: ...


class NullAgentEventSink:
    """No-op :class:`AgentEventSink` — the default when no sink is injected.

    Stateless and side-effect free, so one process-wide instance is safe to
    share as a default argument.
    """

    __slots__ = ()

    def agent_output(  # noqa: D102, PLR0913
        self,
        content: str,
        *,
        channel: AgentOutputChannel = "assistant",
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del content, channel, status, agent_kind, round_label, invocation_id

    def tool_call(  # noqa: D102, PLR0913
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
        del tool, args, call_id, status, agent_kind, round_label, invocation_id

    def tool_result(  # noqa: D102, PLR0913
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
        del tool, content, call_id, is_error, payload, agent_kind, round_label, invocation_id

    def todo_update(  # noqa: D102
        self,
        todos: list[TodoItemData],
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del todos, agent_kind, round_label, invocation_id

    def usage_update(  # noqa: D102, PLR0913
        self,
        input_tokens: int,
        *,
        context_window: int | None = None,
        model: str | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del input_tokens, context_window, model, agent_kind, round_label, invocation_id


NULL_AGENT_EVENT_SINK = NullAgentEventSink()
"""Shared no-op sink instance — the default wherever ``AgentEventSink`` is injected.

A single frozen instance (rather than calling the constructor at each call
site) satisfies the "no function calls in argument defaults" lint rule and
makes the shared default explicit.
"""
