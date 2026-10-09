"""Stdio MCP server for the issue-queue plugin's issue board.

Product composition (`launch.composition`) launches `python -m
entrypoints.issue_board_tools_server <store> <policy> <tracker-config>` with
stable workspace-relative paths. The orchestration rewrites the files' contents
between turns, so the tool declarations stay fixed for a session while every
call reads the current policy and tracker configuration. The tools themselves
come from `vibesys.api.issue_queue`.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from vibesys.api.issue_queue import IssueBoardContext, IssueToolPolicy, issue_board_tools
from vs_issue_tracker.api import IssueTracker, IssueTrackerConfig, open_issue_tracker
from vs_mcp.api import ToolSpec, register_tool, serve_stdio

if TYPE_CHECKING:
    from collections.abc import Sequence

SERVER_NAME = "issue-board"


def build_tools(store_path: Path, policy_path: Path, tracker_config_path: Path) -> list[ToolSpec]:
    """Declare the issue-board tools over the given store, policy and tracker files."""

    def board() -> IssueTracker:
        config = IssueTrackerConfig.model_validate_json(
            tracker_config_path.read_text(encoding="utf-8")
        )
        return open_issue_tracker(
            config.backend, local_path=store_path, repository=config.repository
        )

    def policy() -> IssueToolPolicy:
        return IssueToolPolicy.model_validate_json(policy_path.read_text(encoding="utf-8"))

    return [
        ToolSpec(tool.name, tool.description, tool.input_schema, tool.handler)
        for tool in issue_board_tools(IssueBoardContext(board=board, policy=policy))
    ]


def build_server(store_path: Path, policy_path: Path, tracker_config_path: Path) -> FastMCP:
    """Return the issue-board tools registered on an in-process FastMCP server."""
    server = FastMCP(SERVER_NAME)
    for spec in build_tools(store_path, policy_path, tracker_config_path):
        register_tool(server, spec)
    return server


def main(argv: Sequence[str] | None = None) -> None:
    """Serve the issue-board tools over stdio."""
    parser = argparse.ArgumentParser(prog="vibesys-issue-board")
    parser.add_argument("store_path", type=Path)
    parser.add_argument("policy_path", type=Path)
    parser.add_argument("tracker_config_path", type=Path)
    args = parser.parse_args(argv)
    serve_stdio(
        build_tools(args.store_path, args.policy_path, args.tracker_config_path),
        server_name=SERVER_NAME,
    )


if __name__ == "__main__":
    main()
