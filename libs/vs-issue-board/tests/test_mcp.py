"""Tests for the standalone issue-board MCP server.

We test argparse parsing and tool registration directly via ``build_parser``
and ``build_server``, plus an end-to-end smoke that calls registered tools
through ``FastMCP.call_tool``. We do NOT test the stdio JSON-RPC framing —
that is the ``mcp`` package's responsibility.
"""

import asyncio

import pytest

from vs_issue_board import IssueType
from vs_issue_board.mcp import build_parser, build_server


def _ns(tmp_path, *extra: str):
    return build_parser().parse_args([str(tmp_path / "issues.json"), *extra])


async def _list_tool_names(server) -> set[str]:
    tools = await server.list_tools()
    return {t.name for t in tools}


async def _call_tool(server, name: str, **kwargs) -> str:
    """Invoke an MCP tool and return its string result.

    FastMCP.call_tool returns ``(content_blocks, structured_dict)``; for our
    string-returning tools the structured dict is ``{"result": "..."}``.
    """
    _, structured = await server.call_tool(name, kwargs)
    return structured["result"]


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


class TestArgparse:
    def test_defaults(self, tmp_path):
        args = _ns(tmp_path)
        assert args.store_path == tmp_path / "issues.json"
        assert args.creator == "agent"
        assert args.iteration == 1
        assert args.cap is None
        assert args.allowed_types == frozenset({IssueType.BUG, IssueType.FEATURE, IssueType.PERF})
        assert args.read_only is False

    def test_full_judge_policy(self, tmp_path):
        args = _ns(
            tmp_path,
            "--creator",
            "judge",
            "--iteration",
            "3",
            "--cap",
            "1",
            "--allowed-types",
            "bug",
        )
        assert args.creator == "judge"
        assert args.iteration == 3
        assert args.cap == 1
        assert args.allowed_types == frozenset({IssueType.BUG})

    def test_allowed_types_subset(self, tmp_path):
        args = _ns(tmp_path, "--allowed-types", "bug,perf")
        assert args.allowed_types == frozenset({IssueType.BUG, IssueType.PERF})

    def test_allowed_types_with_whitespace(self, tmp_path):
        args = _ns(tmp_path, "--allowed-types", "bug, perf , feature")
        assert args.allowed_types == frozenset({IssueType.BUG, IssueType.FEATURE, IssueType.PERF})

    def test_allowed_types_rejects_garbage(self, tmp_path):
        with pytest.raises(SystemExit):
            _ns(tmp_path, "--allowed-types", "bug,nonsense")

    def test_allowed_types_rejects_empty(self, tmp_path):
        with pytest.raises(SystemExit):
            _ns(tmp_path, "--allowed-types", " , ")

    def test_read_only_flag(self, tmp_path):
        args = _ns(tmp_path, "--read-only")
        assert args.read_only is True


# ---------------------------------------------------------------------------
# tool registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_writable_registers_all_four(self, tmp_path):
        server = build_server(_ns(tmp_path))
        names = asyncio.run(_list_tool_names(server))
        assert names == {
            "list_issues",
            "get_issue",
            "search_issues",
            "create_issue",
        }

    def test_read_only_omits_create_issue(self, tmp_path):
        server = build_server(_ns(tmp_path, "--read-only"))
        names = asyncio.run(_list_tool_names(server))
        assert names == {"list_issues", "get_issue", "search_issues"}
        assert "create_issue" not in names


# ---------------------------------------------------------------------------
# end-to-end via call_tool
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_list_empty_store(self, tmp_path):
        server = build_server(_ns(tmp_path))
        out = asyncio.run(_call_tool(server, "list_issues"))
        assert out == "(no issues)"

    def test_create_then_list(self, tmp_path):
        server = build_server(_ns(tmp_path, "--creator", "perf_eval"))
        created = asyncio.run(
            _call_tool(
                server,
                "create_issue",
                type="perf",
                title="paged kv",
                description="reduce frag",
            )
        )
        assert created == "created issue #1"
        listed = asyncio.run(_call_tool(server, "list_issues"))
        assert "#1" in listed
        assert "[perf]" in listed
        assert "paged kv" in listed

    def test_judge_policy_caps_at_one(self, tmp_path):
        server = build_server(
            _ns(
                tmp_path,
                "--creator",
                "judge",
                "--cap",
                "1",
                "--allowed-types",
                "bug",
            )
        )
        msg1 = asyncio.run(
            _call_tool(
                server,
                "create_issue",
                type="bug",
                title="t1",
                description="d",
            )
        )
        assert msg1 == "created issue #1"
        msg2 = asyncio.run(
            _call_tool(
                server,
                "create_issue",
                type="bug",
                title="t2",
                description="d",
            )
        )
        assert "cap reached" in msg2

    def test_judge_policy_rejects_disallowed_type(self, tmp_path):
        server = build_server(
            _ns(
                tmp_path,
                "--creator",
                "judge",
                "--cap",
                "1",
                "--allowed-types",
                "bug",
            )
        )
        msg = asyncio.run(
            _call_tool(
                server,
                "create_issue",
                type="perf",
                title="p",
                description="d",
            )
        )
        assert "may only file types" in msg
        assert "'judge'" in msg

    def test_search_returns_short_lines(self, tmp_path):
        server = build_server(_ns(tmp_path, "--creator", "perf_eval"))
        asyncio.run(
            _call_tool(
                server,
                "create_issue",
                type="perf",
                title="Add paged KV",
                description="d",
            )
        )
        asyncio.run(
            _call_tool(
                server,
                "create_issue",
                type="bug",
                title="unrelated",
                description="d",
            )
        )
        out = asyncio.run(_call_tool(server, "search_issues", query="paged"))
        assert "Add paged KV" in out
        assert "unrelated" not in out

    def test_get_issue_returns_full_body(self, tmp_path):
        server = build_server(_ns(tmp_path, "--creator", "perf_eval"))
        asyncio.run(
            _call_tool(
                server,
                "create_issue",
                type="perf",
                title="paged kv",
                description="reduce frag",
            )
        )
        out = asyncio.run(_call_tool(server, "get_issue", issue_id=1))
        assert "## Issue #1" in out
        assert "type: perf" in out
        assert "paged kv" in out
        assert "reduce frag" in out

    def test_get_issue_unknown_id(self, tmp_path):
        server = build_server(_ns(tmp_path))
        out = asyncio.run(_call_tool(server, "get_issue", issue_id=999))
        assert out == "(no issue #999)"

    def test_list_invalid_status_returns_error(self, tmp_path):
        server = build_server(_ns(tmp_path))
        out = asyncio.run(_call_tool(server, "list_issues", status="banana"))
        assert out.startswith("error:")
        assert "invalid status" in out
