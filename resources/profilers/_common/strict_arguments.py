"""A FastMCP server whose every tool refuses arguments it does not declare.

FastMCP validates a call's arguments with a model built from the handler's
signature, and that model drops a key it does not know: an agent's misspelled
optional argument (``top_n`` for ``limit``) would run the tool with the default
instead, and the agent would never learn it. Each profiler server builds a
:class:`StrictFastMCP`, which refuses such a call by name and advertises
``additionalProperties: false``. This is the rule ``vs_mcp`` applies to its own
tools; profiler servers run on the agent's interpreter, which need not have
``vs_mcp``, so the bundle carries the same rule.

Only FastMCP's public ``list_tools`` and ``call_tool`` are overridden, so the
rule holds across ``mcp`` versions the agent's interpreter may carry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcp.types import ContentBlock
    from mcp.types import Tool as MCPTool


class StrictFastMCP(FastMCP):
    """FastMCP whose tools refuse undeclared argument keys and say so in their schemas."""

    async def list_tools(self) -> list[MCPTool]:
        """Every tool, its input schema closed to keys it does not declare."""
        return [
            tool.model_copy(
                update={"inputSchema": {**tool.inputSchema, "additionalProperties": False}}
            )
            for tool in await super().list_tools()
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        """Refuse a call that carries a key its tool does not declare; else run it."""
        for tool in await super().list_tools():
            if tool.name != name:
                continue
            declared = set(tool.inputSchema.get("properties", {}))
            undeclared = sorted(set(arguments) - declared)
            if undeclared:
                message = (
                    f"{name} does not take {', '.join(undeclared)}; "
                    f"it takes {', '.join(sorted(declared)) or 'no arguments'}"
                )
                raise ToolError(message)
        return await super().call_tool(name, arguments)
