"""Fixed issue-board MCP server driven by plugin-owned policy state.

Product composition launches this module with stable workspace-relative store,
policy, and tracker-config paths. The orchestration updates their contents, so
role declarations and tool bindings remain fixed for each session.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from vibesys.orchestrations.issue_queue.models import IssueToolPolicy
from vs_issue_tracker.api import (
    CreateIssuePolicy,
    IssueStatus,
    IssueTracker,
    IssueTrackerConfig,
    IssueType,
    create_issue_under_policy,
    format_issue_full,
    format_issue_short,
    open_issue_tracker,
)


def _policy(path: Path) -> CreateIssuePolicy:
    configured = IssueToolPolicy.model_validate_json(path.read_text(encoding="utf-8"))
    return CreateIssuePolicy(
        creator=configured.creator,
        iteration=configured.iteration,
        cap=configured.cap,
        allowed_types=frozenset(IssueType(value) for value in configured.allowed_types),
    )


def build_server(store_path: Path, policy_path: Path, tracker_config_path: Path) -> FastMCP:
    """Expose issue queries and policy-checked creation over stdio."""
    server = FastMCP("issue-board")

    def board() -> IssueTracker:
        config = IssueTrackerConfig.model_validate_json(
            tracker_config_path.read_text(encoding="utf-8")
        )
        return open_issue_tracker(
            config.backend,
            local_path=store_path,
            repository=config.repository,
        )

    @server.tool()
    def list_issues(status: str | None = None) -> str:
        """List issues, optionally filtered by lifecycle status."""
        try:
            selected = IssueStatus(status) if status else None
        except ValueError:
            return f"error: invalid status {status!r}"
        issues = board().list(status=selected)
        return "\n".join(format_issue_short(issue) for issue in issues) or "(no issues)"

    @server.tool()
    def get_issue(issue_id: int) -> str:
        """Return the complete issue with its history."""
        issue = board().get(issue_id)
        return format_issue_full(issue) if issue is not None else f"(no issue #{issue_id})"

    @server.tool()
    def search_issues(query: str) -> str:
        """Find issues containing every comma-separated, case-insensitive term."""
        issues = board().search(query)
        return "\n".join(format_issue_short(issue) for issue in issues) or "(no matches)"

    @server.tool()
    def create_issue(
        type: str,  # noqa: A002  # lint-waiver: MCP schema uses the domain field name.
        title: str,
        description: str,
    ) -> str:
        """Create an issue when the current turn policy permits it."""
        _, message = create_issue_under_policy(
            board(),
            type_str=type,
            title=title,
            description=description,
            policy=_policy(policy_path),
        )
        return message

    return server


def main(argv: list[str] | None = None) -> None:
    """Run the fixed stdio server."""
    parser = argparse.ArgumentParser(prog="vibesys-issue-board")
    parser.add_argument("store_path", type=Path)
    parser.add_argument("policy_path", type=Path)
    parser.add_argument("tracker_config_path", type=Path)
    args = parser.parse_args(argv)
    build_server(args.store_path, args.policy_path, args.tracker_config_path).run(transport="stdio")


if __name__ == "__main__":
    main()
