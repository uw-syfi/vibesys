"""Public behavior tests for the issue-queue plugin's fixed MCP server."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from vibesys.orchestrations.issue_queue.models import IssueToolPolicy
from vibesys.orchestrations.issue_queue.tool_server import build_server
from vs_issue_tracker.api import IssueBoard, IssueStatus, IssueTrackerConfig

if TYPE_CHECKING:
    from pathlib import Path

    from mcp.server.fastmcp import FastMCP


async def _call_tool(server: FastMCP, name: str, **kwargs: object) -> str:
    """Invoke a registered string-returning tool through FastMCP's public API."""
    response = await server.call_tool(name, kwargs)
    if isinstance(response, dict):
        structured = response
    else:
        _, structured = cast("tuple[object, dict[str, Any]]", response)
    result = structured.get("result")
    if not isinstance(result, str):
        raise TypeError
    return result


def _server(tmp_path: Path, policy: IssueToolPolicy) -> tuple[FastMCP, IssueBoard]:
    store_path = tmp_path / "issues.json"
    policy_path = tmp_path / ".vibesys" / "issue-tool-policy.json"
    config_path = tmp_path / ".vibesys" / "issue-tracker.json"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text(policy.model_dump_json(), encoding="utf-8")
    config_path.write_text(IssueTrackerConfig.local().model_dump_json(), encoding="utf-8")
    board = IssueBoard(store_path)
    return build_server(store_path, policy_path, config_path), board


def _permissive_policy() -> IssueToolPolicy:
    return IssueToolPolicy(
        creator="perf_eval",
        iteration=2,
        cap=4,
        allowed_types=("bug", "feature", "perf"),
    )


def test_server_registers_the_fixed_issue_queue_tools(tmp_path: Path) -> None:
    server, _board = _server(tmp_path, _permissive_policy())

    names = {tool.name for tool in asyncio.run(server.list_tools())}

    assert names == {"list_issues", "get_issue", "search_issues", "create_issue"}


def test_tools_create_query_and_filter_the_real_local_board(tmp_path: Path) -> None:
    server, board = _server(tmp_path, _permissive_policy())

    first = asyncio.run(
        _call_tool(
            server,
            "create_issue",
            type="perf",
            title="Paged KV fragmentation",
            description="Reduce allocator fragmentation under mixed loads.",
        )
    )
    second = asyncio.run(
        _call_tool(
            server,
            "create_issue",
            type="bug",
            title="Metrics counter drift",
            description="Correct the request counter after retries.",
        )
    )
    board.update_status(2, IssueStatus.CLOSED, actor="judge", iteration=2, note="verified")

    listed = asyncio.run(_call_tool(server, "list_issues"))
    open_only = asyncio.run(_call_tool(server, "list_issues", status="open"))
    found = asyncio.run(_call_tool(server, "search_issues", query="paged, fragmentation"))
    detail = asyncio.run(_call_tool(server, "get_issue", issue_id=1))

    assert (first, second) == ("created issue #1", "created issue #2")
    assert "Paged KV fragmentation" in listed
    assert "Metrics counter drift" in listed
    assert "Paged KV fragmentation" in open_only
    assert "Metrics counter drift" not in open_only
    assert "Paged KV fragmentation" in found
    assert "Metrics counter drift" not in found
    assert "## Issue #1" in detail
    assert "type: perf" in detail
    assert "Reduce allocator fragmentation under mixed loads." in detail


def test_create_enforces_the_current_plugin_policy(tmp_path: Path) -> None:
    server, board = _server(
        tmp_path,
        IssueToolPolicy(
            creator="judge",
            iteration=3,
            cap=1,
            allowed_types=("bug",),
        ),
    )

    disallowed = asyncio.run(
        _call_tool(
            server,
            "create_issue",
            type="perf",
            title="Tune batching",
            description="Try a larger batch.",
        )
    )
    created = asyncio.run(
        _call_tool(
            server,
            "create_issue",
            type="bug",
            title="Fix retry accounting",
            description="Count each retry once.",
        )
    )
    capped = asyncio.run(
        _call_tool(
            server,
            "create_issue",
            type="bug",
            title="Fix timeout accounting",
            description="Count each timeout once.",
        )
    )

    assert "as 'judge' you may only file types ['bug'], not 'perf'" in disallowed
    assert created == "created issue #1"
    assert "per-iteration cap reached (1/1)" in capped
    persisted = board.list()
    assert len(persisted) == 1
    assert persisted[0].created_by == "judge"
    assert persisted[0].created_iter == 3


def test_list_rejects_an_invalid_status_without_mutating_the_board(tmp_path: Path) -> None:
    server, board = _server(tmp_path, _permissive_policy())

    result = asyncio.run(_call_tool(server, "list_issues", status="reviewing"))

    assert result == "error: invalid status 'reviewing'"
    assert board.list() == []
