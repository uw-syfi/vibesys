from __future__ import annotations

from vs_issue_board.core import Issue


def format_issue_short(issue: Issue) -> str:
    """One-line summary used by issue-listing tools."""
    return f"#{issue.id} [{issue.type.value}] [{issue.status.value}] {issue.title}"


def format_issue_full(issue: Issue) -> str:
    """Full markdown body used by issue-reading tools."""
    return (
        f"## Issue #{issue.id}\n"
        f"- type: {issue.type.value}\n"
        f"- status: {issue.status.value}\n"
        f"- created_by: {issue.created_by} (iter {issue.created_iter})\n"
        f"- attempts: {issue.attempts}\n"
        f"\n"
        f"### Title\n{issue.title}\n"
        f"\n"
        f"### Description\n{issue.description}\n"
    )
