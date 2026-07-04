"""Tests for the issue-tracker tools (list/get/search/create)."""

from vibe_serve.loops.plain.tools import build_issue_tools
from vs_issue_board import IssueBoard, IssueStatus, IssueType


def _make_store(tmp_path) -> IssueBoard:
    return IssueBoard(tmp_path / "issues.json")


def _tool_by_name(tools, name: str):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not in {[t.name for t in tools]}")


def _invoke(tool, **kwargs) -> str:
    return tool.invoke(kwargs)


# ---------------------------------------------------------------------------
# read tools
# ---------------------------------------------------------------------------


class TestReadTools:
    def test_list_issues_empty_returns_placeholder(self, tmp_path):
        store = _make_store(tmp_path)
        tools = build_issue_tools(store, iteration=1)
        out = _invoke(_tool_by_name(tools, "list_issues"))
        assert out == "(no issues)"

    def test_list_issues_renders_short_format(self, tmp_path):
        store = _make_store(tmp_path)
        store.create(
            type=IssueType.BUG, title="hello", description="d", created_by="x", iteration=1
        )
        tools = build_issue_tools(store, iteration=1)
        out = _invoke(_tool_by_name(tools, "list_issues"))
        assert "#1" in out
        assert "[bug]" in out
        assert "[open]" in out
        assert "hello" in out

    def test_list_issues_filters_by_status(self, tmp_path):
        store = _make_store(tmp_path)
        store.create(
            type=IssueType.BUG, title="open one", description="d", created_by="x", iteration=1
        )
        b = store.create(
            type=IssueType.BUG, title="closed one", description="d", created_by="x", iteration=1
        )
        store.update_status(b.id, IssueStatus.CLOSED, actor="x", iteration=1)
        tools = build_issue_tools(store, iteration=1)
        list_tool = _tool_by_name(tools, "list_issues")
        opens = _invoke(list_tool, status="open")
        assert "open one" in opens
        assert "closed one" not in opens

    def test_list_issues_invalid_status_returns_error(self, tmp_path):
        store = _make_store(tmp_path)
        tools = build_issue_tools(store, iteration=1)
        out = _invoke(_tool_by_name(tools, "list_issues"), status="banana")
        assert out.startswith("error:")

    def test_get_issue_by_id(self, tmp_path):
        store = _make_store(tmp_path)
        store.create(
            type=IssueType.PERF,
            title="paged kv",
            description="reduce frag",
            created_by="perf_eval",
            iteration=1,
        )
        tools = build_issue_tools(store, iteration=1)
        out = _invoke(_tool_by_name(tools, "get_issue"), issue_id=1)
        assert "## Issue #1" in out
        assert "paged kv" in out
        assert "reduce frag" in out
        assert "type: perf" in out

    def test_get_issue_unknown_id_returns_placeholder(self, tmp_path):
        store = _make_store(tmp_path)
        tools = build_issue_tools(store, iteration=1)
        out = _invoke(_tool_by_name(tools, "get_issue"), issue_id=999)
        assert out == "(no issue #999)"

    def test_search_issues_returns_matches(self, tmp_path):
        store = _make_store(tmp_path)
        store.create(
            type=IssueType.PERF, title="Add paged KV", description="d", created_by="x", iteration=1
        )
        store.create(
            type=IssueType.BUG, title="unrelated", description="d", created_by="x", iteration=1
        )
        tools = build_issue_tools(store, iteration=1)
        out = _invoke(_tool_by_name(tools, "search_issues"), query="paged")
        assert "Add paged KV" in out
        assert "unrelated" not in out

    def test_search_issues_no_matches(self, tmp_path):
        store = _make_store(tmp_path)
        store.create(type=IssueType.BUG, title="hi", description="d", created_by="x", iteration=1)
        tools = build_issue_tools(store, iteration=1)
        out = _invoke(_tool_by_name(tools, "search_issues"), query="xyzzy")
        assert out == "(no matches)"


# ---------------------------------------------------------------------------
# subset routing
# ---------------------------------------------------------------------------


class TestSubsetRouting:
    def test_implementer_subset_excludes_create(self, tmp_path):
        store = _make_store(tmp_path)
        tools = build_issue_tools(store, iteration=1, can_create=False)
        names = {t.name for t in tools}
        assert names == {"list_issues", "get_issue", "search_issues"}

    def test_judge_subset_includes_create(self, tmp_path):
        store = _make_store(tmp_path)
        tools = build_issue_tools(
            store,
            iteration=1,
            can_create=True,
            creator="judge",
            create_cap=1,
            allowed_create_types={IssueType.BUG},
        )
        names = {t.name for t in tools}
        assert "create_issue" in names

    def test_perf_eval_subset_includes_create(self, tmp_path):
        store = _make_store(tmp_path)
        tools = build_issue_tools(
            store,
            iteration=1,
            can_create=True,
            creator="perf_eval",
            create_cap=3,
        )
        names = {t.name for t in tools}
        assert "create_issue" in names


# ---------------------------------------------------------------------------
# create_issue
# ---------------------------------------------------------------------------


class TestCreateIssue:
    def test_create_issue_happy_path_returns_id(self, tmp_path):
        store = _make_store(tmp_path)
        tools = build_issue_tools(
            store,
            iteration=1,
            can_create=True,
            creator="perf_eval",
            create_cap=3,
        )
        out = _invoke(
            _tool_by_name(tools, "create_issue"),
            type="perf",
            title="t",
            description="d",
        )
        assert out == "created issue #1"
        assert store.get(1) is not None
        assert store.get(1).created_by == "perf_eval"
        assert store.get(1).created_iter == 1

    def test_create_issue_invalid_type_returns_error_string(self, tmp_path):
        store = _make_store(tmp_path)
        tools = build_issue_tools(
            store,
            iteration=1,
            can_create=True,
            creator="perf_eval",
            create_cap=3,
        )
        out = _invoke(
            _tool_by_name(tools, "create_issue"),
            type="enhancement",
            title="t",
            description="d",
        )
        assert out.startswith("error:")
        assert "must be one of" in out
        # No issue should have been created
        assert len(store.list()) == 0

    def test_create_issue_caps_at_three_per_iteration(self, tmp_path):
        store = _make_store(tmp_path)
        tools = build_issue_tools(
            store,
            iteration=1,
            can_create=True,
            creator="perf_eval",
            create_cap=3,
        )
        create = _tool_by_name(tools, "create_issue")
        for i in range(3):
            out = _invoke(create, type="perf", title=f"t{i}", description="d")
            assert out.startswith("created issue #")
        # Fourth call must be rejected
        rejected = _invoke(create, type="perf", title="t4", description="d")
        assert rejected.startswith("error:")
        assert "cap reached" in rejected
        # Store still has only 3
        assert len(store.list()) == 3

    def test_create_issue_cap_resets_per_iteration(self, tmp_path):
        store = _make_store(tmp_path)
        # iteration 1 — fill cap
        iter1_tools = build_issue_tools(
            store,
            iteration=1,
            can_create=True,
            creator="perf_eval",
            create_cap=3,
        )
        create_iter1 = _tool_by_name(iter1_tools, "create_issue")
        for i in range(3):
            _invoke(create_iter1, type="perf", title=f"i1-{i}", description="d")
        # iteration 2 — fresh cap
        iter2_tools = build_issue_tools(
            store,
            iteration=2,
            can_create=True,
            creator="perf_eval",
            create_cap=3,
        )
        create_iter2 = _tool_by_name(iter2_tools, "create_issue")
        out = _invoke(create_iter2, type="perf", title="i2-0", description="d")
        assert out.startswith("created issue #")
        assert len(store.list()) == 4

    def test_create_issue_allowed_types_filter(self, tmp_path):
        # Judge subset: only bug type allowed.
        store = _make_store(tmp_path)
        tools = build_issue_tools(
            store,
            iteration=1,
            can_create=True,
            creator="judge",
            create_cap=1,
            allowed_create_types={IssueType.BUG},
        )
        create = _tool_by_name(tools, "create_issue")
        rejected_perf = _invoke(create, type="perf", title="p", description="d")
        assert rejected_perf.startswith("error:")
        assert "may only file types" in rejected_perf
        rejected_feature = _invoke(create, type="feature", title="f", description="d")
        assert rejected_feature.startswith("error:")
        ok = _invoke(create, type="bug", title="b", description="d")
        assert ok.startswith("created issue #")

    def test_judge_create_cap_is_one(self, tmp_path):
        store = _make_store(tmp_path)
        tools = build_issue_tools(
            store,
            iteration=1,
            can_create=True,
            creator="judge",
            create_cap=1,
            allowed_create_types={IssueType.BUG},
        )
        create = _tool_by_name(tools, "create_issue")
        ok = _invoke(create, type="bug", title="first", description="d")
        assert ok.startswith("created issue #")
        rejected = _invoke(create, type="bug", title="second", description="d")
        assert rejected.startswith("error:")
        assert "cap reached" in rejected

    def test_create_issue_unlimited_cap_when_none(self, tmp_path):
        store = _make_store(tmp_path)
        tools = build_issue_tools(
            store,
            iteration=1,
            can_create=True,
            creator="x",
            create_cap=None,
        )
        create = _tool_by_name(tools, "create_issue")
        for i in range(10):
            out = _invoke(create, type="bug", title=f"t{i}", description="d")
            assert out.startswith("created issue #")
        assert len(store.list()) == 10
