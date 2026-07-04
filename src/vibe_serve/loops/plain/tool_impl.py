"""Compatibility exports for issue-board tool helpers.

New code should import these generic helpers directly from :mod:`vs_issue_board`.
"""

from vs_issue_board import (
    CreateIssuePolicy,
    check_create_allowed,
    create_issue_under_policy,
    format_issue_full,
    format_issue_short,
    parse_type,
)

__all__ = [
    "CreateIssuePolicy",
    "check_create_allowed",
    "create_issue_under_policy",
    "format_issue_full",
    "format_issue_short",
    "parse_type",
]
