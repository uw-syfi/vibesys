"""Compatibility tests for old VibeServe issue-board import paths."""

from vibe_serve.loops.plain import issue_board as compat
from vibe_serve.loops.plain import mcp_server, tool_impl
from vs_issue_board import (
    CreateIssuePolicy,
    Issue,
    IssueBoard,
    IssueEvent,
    IssueStatus,
    IssueType,
    check_create_allowed,
    create_issue_under_policy,
    format_issue_full,
    format_issue_short,
    parse_type,
)
from vs_issue_board import mcp as issue_board_mcp


def test_issue_board_compat_exports_reusable_package_api():
    assert compat.Issue is Issue
    assert compat.IssueBoard is IssueBoard
    assert compat.IssueEvent is IssueEvent
    assert compat.IssueStatus is IssueStatus
    assert compat.IssueType is IssueType


def test_tool_impl_compat_exports_reusable_package_helpers():
    assert tool_impl.CreateIssuePolicy is CreateIssuePolicy
    assert tool_impl.check_create_allowed is check_create_allowed
    assert tool_impl.create_issue_under_policy is create_issue_under_policy
    assert tool_impl.format_issue_full is format_issue_full
    assert tool_impl.format_issue_short is format_issue_short
    assert tool_impl.parse_type is parse_type


def test_mcp_server_compat_exports_reusable_package_server():
    assert mcp_server.build_parser is issue_board_mcp.build_parser
    assert mcp_server.build_server is issue_board_mcp.build_server
    assert mcp_server.main is issue_board_mcp.main
