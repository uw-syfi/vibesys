"""Standalone stdio MCP server exposing an IssueBoard as MCP tools.

Usage::

    vibeserve-issue-mcp /path/to/issues.json [options]
    python -m vibeserve_agent.loops.plain.mcp_server /path/to/issues.json [options]

Mounts four tools (``list_issues``, ``get_issue``, ``search_issues``,
``create_issue``) against the given ``issues.json`` file. Any MCP client can
mount this server: Claude Desktop, Cursor, custom MCP clients, or the
vibeserve issue-loop's own ``.mcp.json`` sandwich (added in a separate PR).

The four tools delegate into :mod:`vibeserve_agent.loops.plain.tool_impl` so that
the deepagents (LangChain ``@tool``) path and the MCP path enforce identical
per-iteration cap and type-allowlist semantics with byte-identical error
strings.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from vibeserve_agent.loops.plain.issue_board import IssueStatus, IssueBoard, IssueType
from vibeserve_agent.loops.plain.tool_impl import (
    CreateIssuePolicy,
    create_issue_under_policy,
    format_issue_full,
    format_issue_short,
)

_ALL_TYPES: frozenset[IssueType] = frozenset(
    {IssueType.BUG, IssueType.FEATURE, IssueType.PERF}
)


def _parse_allowed_types(value: str) -> frozenset[IssueType]:
    """argparse type= callable for ``--allowed-types``."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError(
            "--allowed-types may not be empty; pass a comma-separated subset "
            "of {bug,feature,perf}"
        )
    try:
        return frozenset(IssueType(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--allowed-types must be a comma-separated subset of "
            f"{{bug,feature,perf}}; got {value!r}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibeserve-issue-mcp",
        description=(
            "Stdio MCP server exposing an IssueBoard (issues.json) as four "
            "tools: list_issues, get_issue, search_issues, create_issue."
        ),
    )
    parser.add_argument(
        "store_path",
        type=Path,
        help="Path to the issues.json file to expose.",
    )
    parser.add_argument(
        "--creator",
        default="agent",
        help="Identity recorded on issues created via this server "
             "(default: 'agent').",
    )
    parser.add_argument(
        "--iteration",
        type=int,
        default=1,
        help="1-based iteration number used for per-iteration cap "
             "accounting (default: 1).",
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=None,
        help="Max issues this creator may file in this iteration. "
             "Omit for no cap.",
    )
    parser.add_argument(
        "--allowed-types",
        type=_parse_allowed_types,
        default=_ALL_TYPES,
        help="Comma-separated subset of {bug,feature,perf} (default: all).",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Do not register create_issue. Server exposes only "
             "list/get/search.",
    )
    return parser


def build_server(args: argparse.Namespace) -> FastMCP:
    """Build a configured FastMCP instance from parsed args.

    Factored out from :func:`main` so unit tests can introspect the server's
    registered tools (via ``await mcp.list_tools()``) and call them
    (via ``await mcp.call_tool(name, args)``) without spinning up a real
    stdio loop.
    """
    store = IssueBoard(args.store_path)
    policy = CreateIssuePolicy(
        creator=args.creator,
        iteration=args.iteration,
        cap=args.cap,
        allowed_types=frozenset(args.allowed_types),
    )
    mcp = FastMCP("vibeserve-issues")

    @mcp.tool()
    def list_issues(status: str | None = None) -> str:
        """List issues. Optional status filter: 'open', 'in_progress', 'closed', 'blocked'."""
        store.reload()
        try:
            status_enum = IssueStatus(status) if status else None
        except ValueError:
            return (
                f"error: invalid status '{status}'. "
                f"Use one of: {[s.value for s in IssueStatus]} or omit."
            )
        issues = store.list(status=status_enum)
        if not issues:
            return "(no issues)"
        return "\n".join(format_issue_short(i) for i in issues)

    @mcp.tool()
    def get_issue(issue_id: int) -> str:
        """Return the full body of an issue by id."""
        store.reload()
        issue = store.get(issue_id)
        if issue is None:
            return f"(no issue #{issue_id})"
        return format_issue_full(issue)

    @mcp.tool()
    def search_issues(query: str) -> str:
        """Substring search across all issues' title+description.

        Use comma-separated keywords for AND-matching, e.g. 'kv cache, paged'.
        Matching is case-insensitive.
        """
        store.reload()
        hits = store.search(query)
        if not hits:
            return "(no matches)"
        return "\n".join(format_issue_short(i) for i in hits)

    if not args.read_only:
        @mcp.tool()
        def create_issue(type: str, title: str, description: str) -> str:
            """Create a new issue.

            Args:
                type: One of 'bug', 'feature', 'perf'.
                title: Short summary (one line).
                description: Markdown body. Should follow this template:
                  ## Background
                  ## Acceptance criteria
                  ## Notes
            """
            _, msg = create_issue_under_policy(
                store,
                type_str=type,
                title=title,
                description=description,
                policy=policy,
            )
            return msg

    return mcp


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    mcp = build_server(args)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
