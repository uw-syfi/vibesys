"""Standalone stdio MCP server exposing an IssueBoard as MCP tools."""

from __future__ import annotations

import argparse
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from vs_issue_tracker.core import IssueStatus, IssueTracker, IssueType
from vs_issue_tracker.format import format_issue_full, format_issue_short
from vs_issue_tracker.github import open_issue_tracker
from vs_issue_tracker.policy import CreateIssuePolicy, create_issue_under_policy

_ALL_TYPES: frozenset[IssueType] = frozenset({IssueType.BUG, IssueType.FEATURE, IssueType.PERF})


def _parse_allowed_types(value: str) -> frozenset[IssueType]:
    """Argparse type= callable for ``--allowed-types``."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        message = (
            "--allowed-types may not be empty; pass a comma-separated subset of {bug,feature,perf}"
        )
        raise argparse.ArgumentTypeError(message)
    try:
        return frozenset(IssueType(p) for p in parts)
    except ValueError as exc:
        message = (
            f"--allowed-types must be a comma-separated subset of "
            f"{{bug,feature,perf}}; got {value!r}"
        )
        raise argparse.ArgumentTypeError(message) from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the issue-board MCP server."""
    parser = argparse.ArgumentParser(
        prog="vibesys-issue-mcp",
        description=(
            "Stdio MCP server exposing an IssueBoard as four "
            "tools: list_issues, get_issue, search_issues, create_issue."
        ),
    )
    parser.add_argument(
        "store_path",
        type=Path,
        nargs="?",
        help="Path to the issues JSON file to expose (omit for GitHub mode).",
    )
    parser.add_argument(
        "--github-repository",
        help="Use GitHub Issues in OWNER/REPOSITORY instead of local JSON.",
    )
    parser.add_argument(
        "--creator",
        default="agent",
        help="Identity recorded on issues created via this server (default: 'agent').",
    )
    parser.add_argument(
        "--iteration",
        type=int,
        default=1,
        help="1-based iteration number used for per-iteration cap accounting (default: 1).",
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=None,
        help="Max issues this creator may file in this iteration. Omit for no cap.",
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
        help="Do not register create_issue. Server exposes only list/get/search.",
    )
    return parser


def build_server(args: argparse.Namespace) -> FastMCP:
    """Build the standalone MCP server for local JSON or GitHub storage."""
    if args.github_repository is not None:
        if args.store_path is not None:
            raise ValueError("choose either store_path or --github-repository")  # noqa: TRY003  # tracked: #288
        local_path = Path("issues.json")
        tracker = open_issue_tracker(
            "github",
            local_path=local_path,
            repository=args.github_repository,
        )
    else:
        if args.store_path is None:
            raise ValueError("store_path is required unless --github-repository is set")  # noqa: TRY003  # tracked: #288
        tracker = open_issue_tracker("local", local_path=args.store_path)
    policy = CreateIssuePolicy(
        creator=args.creator,
        iteration=args.iteration,
        cap=args.cap,
        allowed_types=frozenset(args.allowed_types),
    )
    return build_tracker_server(tracker, policy=policy, read_only=args.read_only)


def build_tracker_server(
    tracker: IssueTracker,
    *,
    policy: CreateIssuePolicy,
    read_only: bool = False,
) -> FastMCP:
    """Expose a storage-neutral tracker through the issue-board MCP tools."""
    mcp = FastMCP("issue-board")

    @mcp.tool()
    def list_issues(status: str | None = None) -> str:
        """List issues. Optional status filter: 'open', 'in_progress', 'closed', 'blocked'."""
        try:
            status_enum = IssueStatus(status) if status else None
        except ValueError:
            return (
                f"error: invalid status '{status}'. "
                f"Use one of: {[s.value for s in IssueStatus]} or omit."
            )
        issues = tracker.list(status=status_enum)
        if not issues:
            return "(no issues)"
        return "\n".join(format_issue_short(i) for i in issues)

    @mcp.tool()
    def get_issue(issue_id: int) -> str:
        """Return the full body of an issue by id."""
        issue = tracker.get(issue_id)
        if issue is None:
            return f"(no issue #{issue_id})"
        return format_issue_full(issue)

    @mcp.tool()
    def search_issues(query: str) -> str:
        """Substring search across all issues' title+description.

        Use comma-separated keywords for AND-matching, e.g. 'kv cache, paged'.
        Matching is case-insensitive.
        """
        hits = tracker.search(query)
        if not hits:
            return "(no matches)"
        return "\n".join(format_issue_short(i) for i in hits)

    if not read_only:

        @mcp.tool()
        def create_issue(
            type: str,  # noqa: A002  # lint-waiver: LW-010103 [A002]; MCP schema must retain its established `type` argument.
            title: str,
            description: str,
        ) -> str:
            """Create a new issue.

            Args:
                type: One of 'bug', 'feature', 'perf'.
                title: Short summary (one line).
                description: Markdown body.
            """
            _, msg = create_issue_under_policy(
                tracker,
                type_str=type,
                title=title,
                description=description,
                policy=policy,
            )
            return msg

    return mcp


def main(argv: list[str] | None = None) -> None:
    """Run the issue-board MCP server using parsed command-line arguments."""
    args = build_parser().parse_args(argv)
    mcp = build_server(args)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
