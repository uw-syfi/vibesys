"""Tests for the shared tool implementation in vibeserve_agent.loops.plain.tool_impl.

These tests cover the validators, the create-issue policy enforcement, and
the formatting helpers that both the LangChain ``@tool`` wrappers and the
FastMCP server delegate into. The byte-for-byte format assertions act as a
contract gate against accidental wording drift, since the issue-loop's
prompt templates document these exact strings.
"""

import pytest

from vibeserve_agent.loops.plain.issue_board import IssueBoard, IssueType
from vibeserve_agent.loops.plain.tool_impl import (
    CreateIssuePolicy,
    check_create_allowed,
    create_issue_under_policy,
    format_issue_full,
    format_issue_short,
    parse_type,
)


def _make_store(tmp_path) -> IssueBoard:
    return IssueBoard(tmp_path / "issues.json")


def _all_types() -> frozenset[IssueType]:
    return frozenset({IssueType.BUG, IssueType.FEATURE, IssueType.PERF})


# ---------------------------------------------------------------------------
# parse_type
# ---------------------------------------------------------------------------


class TestParseType:
    def test_parse_each_value(self):
        assert parse_type("bug") is IssueType.BUG
        assert parse_type("feature") is IssueType.FEATURE
        assert parse_type("perf") is IssueType.PERF

    def test_garbage_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_type("enhancement")


# ---------------------------------------------------------------------------
# check_create_allowed
# ---------------------------------------------------------------------------


class TestCheckCreateAllowed:
    def test_in_allowlist_no_cap_returns_none(self, tmp_path):
        store = _make_store(tmp_path)
        policy = CreateIssuePolicy(
            creator="x", iteration=1, cap=None, allowed_types=_all_types(),
        )
        assert (
            check_create_allowed(store, type_enum=IssueType.BUG, policy=policy)
            is None
        )

    def test_out_of_allowlist_returns_error(self, tmp_path):
        store = _make_store(tmp_path)
        policy = CreateIssuePolicy(
            creator="judge",
            iteration=1,
            cap=1,
            allowed_types=frozenset({IssueType.BUG}),
        )
        err = check_create_allowed(
            store, type_enum=IssueType.PERF, policy=policy
        )
        assert err is not None
        assert err.startswith("error:")
        assert "may only file types" in err
        assert "'judge'" in err
        assert "'perf'" in err

    def test_cap_enforced_against_persisted_store(self, tmp_path):
        store = _make_store(tmp_path)
        policy = CreateIssuePolicy(
            creator="judge",
            iteration=1,
            cap=1,
            allowed_types=frozenset({IssueType.BUG}),
        )
        # First creation must be allowed.
        assert (
            check_create_allowed(store, type_enum=IssueType.BUG, policy=policy)
            is None
        )
        # Pre-populate the store with one judge-authored issue in iteration 1.
        store.create(
            type=IssueType.BUG,
            title="t",
            description="d",
            created_by="judge",
            iteration=1,
        )
        # Now the cap is reached.
        err = check_create_allowed(
            store, type_enum=IssueType.BUG, policy=policy
        )
        assert err is not None
        assert "cap reached" in err
        assert "(1/1)" in err

    def test_cap_scoped_per_iteration(self, tmp_path):
        store = _make_store(tmp_path)
        store.create(
            type=IssueType.BUG,
            title="iter1",
            description="d",
            created_by="judge",
            iteration=1,
        )
        # Same creator, *different* iteration → cap not yet hit.
        policy = CreateIssuePolicy(
            creator="judge",
            iteration=2,
            cap=1,
            allowed_types=frozenset({IssueType.BUG}),
        )
        assert (
            check_create_allowed(store, type_enum=IssueType.BUG, policy=policy)
            is None
        )

    def test_cap_scoped_per_creator(self, tmp_path):
        store = _make_store(tmp_path)
        store.create(
            type=IssueType.BUG,
            title="t",
            description="d",
            created_by="perf_eval",
            iteration=1,
        )
        # Different creator in the same iteration is unaffected.
        policy = CreateIssuePolicy(
            creator="judge",
            iteration=1,
            cap=1,
            allowed_types=frozenset({IssueType.BUG}),
        )
        assert (
            check_create_allowed(store, type_enum=IssueType.BUG, policy=policy)
            is None
        )


# ---------------------------------------------------------------------------
# create_issue_under_policy
# ---------------------------------------------------------------------------


class TestCreateIssueUnderPolicy:
    def test_happy_path_returns_issue_and_message(self, tmp_path):
        store = _make_store(tmp_path)
        policy = CreateIssuePolicy(
            creator="perf_eval",
            iteration=1,
            cap=3,
            allowed_types=_all_types(),
        )
        issue, msg = create_issue_under_policy(
            store,
            type_str="perf",
            title="t",
            description="d",
            policy=policy,
        )
        assert issue is not None
        assert issue.id == 1
        assert issue.created_by == "perf_eval"
        assert issue.created_iter == 1
        assert msg == "created issue #1"

    def test_invalid_type_returns_none_and_error(self, tmp_path):
        store = _make_store(tmp_path)
        policy = CreateIssuePolicy(
            creator="perf_eval",
            iteration=1,
            cap=3,
            allowed_types=_all_types(),
        )
        issue, msg = create_issue_under_policy(
            store,
            type_str="enhancement",
            title="t",
            description="d",
            policy=policy,
        )
        assert issue is None
        assert msg.startswith("error:")
        assert "must be one of" in msg
        assert "'enhancement'" in msg
        # Nothing was written.
        assert store.list() == []

    def test_cap_rejects_after_full(self, tmp_path):
        store = _make_store(tmp_path)
        policy = CreateIssuePolicy(
            creator="perf_eval",
            iteration=1,
            cap=2,
            allowed_types=_all_types(),
        )
        for i in range(2):
            issue, msg = create_issue_under_policy(
                store,
                type_str="perf",
                title=f"t{i}",
                description="d",
                policy=policy,
            )
            assert issue is not None
            assert msg == f"created issue #{i + 1}"
        # Third call must be rejected.
        issue, msg = create_issue_under_policy(
            store,
            type_str="perf",
            title="t2",
            description="d",
            policy=policy,
        )
        assert issue is None
        assert "cap reached" in msg
        # Store still has only 2.
        assert len(store.list()) == 2

    def test_unlimited_cap(self, tmp_path):
        store = _make_store(tmp_path)
        policy = CreateIssuePolicy(
            creator="x", iteration=1, cap=None, allowed_types=_all_types(),
        )
        for i in range(5):
            issue, msg = create_issue_under_policy(
                store,
                type_str="bug",
                title=f"t{i}",
                description="d",
                policy=policy,
            )
            assert issue is not None
            assert msg == f"created issue #{i + 1}"


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


class TestFormatHelpers:
    def test_format_issue_short_byte_for_byte(self, tmp_path):
        store = _make_store(tmp_path)
        issue = store.create(
            type=IssueType.PERF,
            title="paged kv",
            description="reduce frag",
            created_by="perf_eval",
            iteration=2,
        )
        assert (
            format_issue_short(issue)
            == "#1 [perf] [open] paged kv"
        )

    def test_format_issue_full_byte_for_byte(self, tmp_path):
        store = _make_store(tmp_path)
        issue = store.create(
            type=IssueType.BUG,
            title="oops",
            description="something is wrong",
            created_by="judge",
            iteration=3,
        )
        # attempts is 0 for a freshly-created issue.
        expected = (
            "## Issue #1\n"
            "- type: bug\n"
            "- status: open\n"
            "- created_by: judge (iter 3)\n"
            "- attempts: 0\n"
            "\n"
            "### Title\noops\n"
            "\n"
            "### Description\nsomething is wrong\n"
        )
        assert format_issue_full(issue) == expected
