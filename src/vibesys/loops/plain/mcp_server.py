"""Compatibility wrapper for the reusable ``vs-issue-board`` MCP server.

New code should invoke :mod:`vs_issue_board.mcp` directly.
"""

from vs_issue_board.mcp import build_parser, build_server, main

__all__ = ["build_parser", "build_server", "main"]
