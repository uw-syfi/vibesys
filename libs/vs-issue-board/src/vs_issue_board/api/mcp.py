"""Python helpers for the issue board stdio MCP server.

``build_parser`` defines the standalone server's command line options.
``build_server`` creates a configured FastMCP server; ``main`` parses options
and runs it over stdio. The executable module remains :mod:`vs_issue_board.mcp`.
"""

from vs_issue_board.mcp import build_parser, build_server, main

__all__ = ["build_parser", "build_server", "main"]
