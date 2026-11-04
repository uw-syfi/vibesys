"""Published surface of ``vs_mcp``: declaring, describing and serving MCP tools.

``ToolSpec`` declares one tool, ``tool_server`` builds the server for a list of
them and ``serve_stdio`` serves it from a subprocess, and ``expose_as_tools`` builds the ``ToolServerDescriptor`` that a
host uses to launch such a subprocess. Translating a descriptor into a
particular agent transport belongs to the host library, not here.
"""

from __future__ import annotations

from vs_mcp.server import serve_stdio, tool_server
from vs_mcp.tools import StdioServerDescriptor, ToolServerDescriptor, ToolSpec, expose_as_tools

__all__ = [
    "StdioServerDescriptor",
    "ToolServerDescriptor",
    "ToolSpec",
    "expose_as_tools",
    "serve_stdio",
    "tool_server",
]
