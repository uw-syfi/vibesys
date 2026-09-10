"""A one-tool stdio MCP server, for proving an agent really called a tool.

The end-to-end driver test needs a tool whose answer the model cannot know
and cannot guess, so the tool adds two large numbers. Run as a script; the
agent's CLI spawns it as a stdio child.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

server = FastMCP("vibesys-e2e-calc")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b


if __name__ == "__main__":
    server.run()
