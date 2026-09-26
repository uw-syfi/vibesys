"""Host-level adapter from framework tool descriptors to MCP specs.

A strategy describes an MCP tool server as primitives (``framework.api.expose_as_tools``
builds a :class:`~framework.api.StdioServerDescriptor` from a module and argv, the
same shape the agent framework serves generically inside the subprocess).
This module is the one place that turns that descriptor into the concrete
:class:`~framework.api.MCPServerSpec` a driver launches, so no strategy hand-builds
an ``MCPServerSpec`` itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from framework.api import MCPServerSpec

if TYPE_CHECKING:
    from framework.api import StdioServerDescriptor


def mcp_spec_from_descriptor(descriptor: StdioServerDescriptor) -> MCPServerSpec:
    """Turn one generic stdio tool descriptor into a driver-facing MCP spec."""
    return MCPServerSpec(
        name=descriptor.name,
        command=descriptor.command,
        args=descriptor.args,
        env=descriptor.env,
    )
