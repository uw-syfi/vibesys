"""Public API of the ``vs_agent`` library.

Pure value types describing agent identity and selection, shared by core and
the server without either owning the other's types, plus a generic
subprocess-hosted MCP tool bridge (:mod:`vs_agent.tools`,
:mod:`vs_agent.mcp_server`).
"""

from vs_agent.mcp_server import register_tool, serve_stdio
from vs_agent.selection import AgentSelection
from vs_agent.session_key import AgentSessionKey, SessionScope
from vs_agent.tools import StdioServerDescriptor, ToolSpec, expose_as_tools

__all__ = [
    "AgentSelection",
    "AgentSessionKey",
    "SessionScope",
    "StdioServerDescriptor",
    "ToolSpec",
    "expose_as_tools",
    "register_tool",
    "serve_stdio",
]
