"""Tool servers a core session may be offered besides the bridged evaluation tool.

A core run has one writer of run state: core. An agent tool whose server records into run
state would be a second writer, so composition refuses it. A tool that only reads or analyses
(the profiler diagnostics server holds no run state) is safe, but only when the host says so
(`AgentTool.stateless`) *and* provides the server; a role that names a stateless tool nobody
serves is refused rather than silently given no tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_core.api import Scope
    from vs_mcp.api import ToolServerDescriptor
    from vs_runtime._session_resolver import ToolServerSource
    from vs_runtime.contracts import AgentRole


class StatelessToolServers:
    """Serves each declared stateless tool its fixed server, to every role that declares it."""

    def __init__(self, servers: Mapping[str, ToolServerDescriptor]) -> None:
        """Bind tool ids to the servers that provide them."""
        self._servers = dict(servers)

    @property
    def tool_ids(self) -> frozenset[str]:
        """The tools this source can serve."""
        return frozenset(self._servers)

    def servers(self, role: AgentRole, scope: Scope) -> tuple[ToolServerDescriptor, ...]:
        """The servers of the stateless tools the role declares, in the role's order."""
        del scope
        return tuple(
            self._servers[tool.id]
            for tool in role.extra_tools
            if tool.stateless and tool.id in self._servers
        )


class CombinedToolServers:
    """Offers a role the servers of several sources, in order."""

    def __init__(self, *sources: ToolServerSource) -> None:
        """Combine ``sources``; each is asked for every role and scope."""
        self._sources = sources

    def servers(self, role: AgentRole, scope: Scope) -> tuple[ToolServerDescriptor, ...]:
        """Every source's servers for this role and scope."""
        return tuple(item for source in self._sources for item in source.servers(role, scope))
