"""Product-owned wiring between orchestration declarations and runtime adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from vibesys.profilers import tool_server as profiler_tool_server
from vs_agent.api import ToolServerDescriptor, expose_as_tools

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.profilers import ProfilerKind
    from vs_runtime.api import Workspace


@dataclass(frozen=True, slots=True)
class AgentToolContext:
    """Product facts available while binding one declared agent tool."""

    profiler_kind: ProfilerKind


def _profiler_tool(context: object, _workspace: Workspace) -> tuple[ToolServerDescriptor, ...]:
    """Bind the selected profiler's analysis server to one agent session."""
    resolved = cast("AgentToolContext", context)
    spec = profiler_tool_server(resolved.profiler_kind)
    return () if spec is None else (spec,)


def _issue_board_tool(_host: object, _workspace: Workspace) -> tuple[ToolServerDescriptor, ...]:
    """Bind the fixed issue-board server to workspace-relative policy artifacts."""
    return (
        expose_as_tools(
            name="vibesys-issue-board",
            entrypoint_module="vibesys.orchestration.issue_queue.tool_server",
            entrypoint_args=(
                "issues.json",
                ".vibesys/issue-tool-policy.json",
                ".vibesys/issue-tracker.json",
            ),
        ),
    )


AGENT_TOOL_BINDINGS: Mapping[
    str, Callable[[object, Workspace], tuple[ToolServerDescriptor, ...]]
] = {
    "issue-board": _issue_board_tool,
    "profiler": _profiler_tool,
}
"""Built-in agent tools bound by product composition, not orchestration policy."""
