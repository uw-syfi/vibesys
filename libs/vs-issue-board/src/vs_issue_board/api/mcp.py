"""Python helpers for the issue board stdio MCP server.

``build_parser`` defines the standalone server's command line options.
``build_server`` creates the standalone JSON-backed server, while
``build_tracker_server`` exposes any :class:`IssueTracker` implementation.
``main`` parses options and runs the JSON server over stdio. The executable
module remains :mod:`vs_issue_board.mcp`.
"""

from vs_issue_board.mcp import build_parser, build_server, build_tracker_server, main

__all__ = ["build_parser", "build_server", "build_tracker_server", "main"]
