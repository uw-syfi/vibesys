"""Built-in tool bindings selected by the VibeSys launch catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vs_evaluation.api.queue_status import queue_status_mcp_descriptor
from vs_mcp.api import StdioServerDescriptor, ToolServerDescriptor

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.api.wiring import AgentToolContext


def profiler_server(tool_context: object) -> ToolServerDescriptor | None:
    """The selected profiler's analysis server, or None when the run has no profiler.

    It reads captured profiles and writes nothing to run state, so a core run may offer it
    to a role that declares the profiler tool `stateless`.
    """
    context = cast("AgentToolContext", tool_context)
    if context.profiler_id == "none":
        return None
    support_name = f"{context.profiler_id}_profiler"
    return StdioServerDescriptor(
        name=f"vibesys-{context.profiler_id.replace('_', '-')}-profiler",
        command="python",
        args=(f"{support_name}/server.py",),
        env=context.profiler_env,
    )


def queue_status_server(tool_context: object) -> ToolServerDescriptor | None:
    """The read-only queue status server, or None when the host serves no queue status.

    It reads the host's queue status service and writes nothing, so a core run may offer
    it to a role that declares the ``queue_status`` tool ``stateless``.
    """
    context = cast("AgentToolContext", tool_context)
    if context.queue_status_socket is None:
        return None
    return queue_status_mcp_descriptor(context.queue_status_socket)


STATELESS_TOOL_SERVERS: Mapping[str, Callable[[object], ToolServerDescriptor | None]] = {
    "profiler": profiler_server,
    "queue_status": queue_status_server,
}
"""Agent tools that hold no run state, by tool id: a core run may offer them to a role."""
