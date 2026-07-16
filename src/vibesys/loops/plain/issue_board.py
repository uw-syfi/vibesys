"""Compatibility exports for the reusable ``vs-issue-board`` package.

New code should import directly from :mod:`vs_issue_board`.
"""

from vs_issue_board import Issue, IssueBoard, IssueEvent, IssueStatus, IssueType

__all__ = [
    "Issue",
    "IssueBoard",
    "IssueEvent",
    "IssueStatus",
    "IssueType",
]
