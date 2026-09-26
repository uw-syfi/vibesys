"""Product-owned wiring between orchestration declarations and runtime adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from vibesys.profilers import mcp_spec as profiler_mcp_spec

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.profilers import ProfilerKind
    from vs_agent.api import MCPServerSpec


class _ProfilerEnvironment(Protocol):
    @property
    def profiler_kind(self) -> ProfilerKind:
        """Return the profiler selected after runtime preflight."""
        ...


class _ProfilerToolHost(Protocol):
    @property
    def environment(self) -> _ProfilerEnvironment:
        """Return the resolved run environment facts."""
        ...


def _profiler_tool(host: object, _workspace: object) -> tuple[MCPServerSpec, ...]:
    """Bind the selected profiler's analysis server to one agent session."""
    resolved = cast("_ProfilerToolHost", host)
    spec = profiler_mcp_spec(resolved.environment.profiler_kind)
    return () if spec is None else (spec,)


AGENT_TOOL_BINDINGS: Mapping[str, Callable[[object, object], tuple[MCPServerSpec, ...]]] = {
    "profiler": _profiler_tool
}
"""Built-in agent tools bound by product composition, not orchestration policy."""
