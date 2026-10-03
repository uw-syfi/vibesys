"""Byte-exact goldens for the issue-queue text agents read outside their prompts.

The issue Markdown view, the board index, the run progress log, and the MCP
tool results are all read by agents. Regenerate with
``UPDATE_PROMPT_SNAPSHOTS=1`` and review every fixture diff as a prompt diff.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel
from tests.vibesys.golden.helpers import assert_exact_text

from vibesys.orchestration.issue_queue.artifacts import append_progress, render_all, render_issue
from vibesys.orchestration.issue_queue.models import IssueToolPolicy
from vibesys.orchestration.issue_queue.tool_server import build_server
from vs_issue_tracker.api import (
    FileProgressLog,
    Issue,
    IssueBoard,
    IssueEvent,
    IssueStatus,
    IssueTrackerConfig,
    IssueType,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP

_SNAPSHOT_DIR = Path(__file__).with_name("fixtures") / "board"
_NOW = "2026-04-08T12:00:00"


def _event(actor: str, action: str, **changes: object) -> IssueEvent:
    return IssueEvent.model_validate(
        {"timestamp": _NOW, "actor": actor, "action": action, "iteration": 1, **changes}
    )


def _issue(issue_id: int, **changes: object) -> Issue:
    return Issue.model_validate(
        {
            "id": issue_id,
            "type": IssueType.FEATURE,
            "title": "Build server",
            "description": "Run a server on port 8000.",
            "status": IssueStatus.OPEN,
            "created_by": "loop:bootstrap",
            "created_iter": 1,
            "created_at": _NOW,
            "updated_at": _NOW,
            "history": [_event("loop:bootstrap", "create")],
            **changes,
        }
    )


_RICH = _issue(
    42,
    title="Stream | tokens",
    attempts=2,
    status=IssueStatus.IN_PROGRESS,
    history=[
        _event("loop:bootstrap", "create", iteration=None),
        _event(
            "implementer",
            "attempt",
            note="built it",
            payload={
                "summary": "built x",
                "self_check": "all green",
                "files_touched": ["server.py", "tests/test_x.py"],
            },
        ),
        _event(
            "judge",
            "in_progress->open",
            payload={
                "verdict": "fail",
                "analysis": "the endpoint is missing",
                "feedback": "add /v1/completions",
                "evaluator_feedback": ["latency regressed", "p99 over budget"],
            },
        ),
        _event(
            "perf_eval",
            "measure",
            iteration=3,
            payload={"throughput_trend": "improved", "latency_trend": "mixed"},
        ),
    ],
)
_BOARD = [
    _RICH,
    _issue(1),
    _issue(2, type=IssueType.BUG, status=IssueStatus.CLOSED, title="Fix counter", attempts=1),
    _issue(3, type=IssueType.PERF, status=IssueStatus.BLOCKED, title="Cut p99"),
]


class _Turn(BaseModel):
    summary: str
    verdict: str = ""
    feedback: str = ""
    files_touched: list[str] = []


def _index(tmp_path: Path, issues: list[Issue]) -> str:
    render_all(tmp_path / "issues", issues)
    return (tmp_path / "issues" / "INDEX.md").read_text(encoding="utf-8")


def _progress(tmp_path: Path) -> str:
    log = FileProgressLog(tmp_path / "progress.md")
    append_progress(
        log,
        _Turn(summary="checked", verdict="pass", files_touched=["a.py"]),
        iteration=2,
        step="review",
        issue_id=4,
    )
    append_progress(log, _Turn(summary=""), iteration=3, step="performance")
    return log.read()


async def _call_tool(server: FastMCP, name: str, **kwargs: object) -> str:
    response = await server.call_tool(name, kwargs)
    structured = (
        response
        if isinstance(response, dict)
        else cast("tuple[object, dict[str, Any]]", response)[1]
    )
    result = structured["result"]
    assert isinstance(result, str)
    return result


def _tools(tmp_path: Path) -> str:
    store_path = tmp_path / "issues.json"
    policy_path = tmp_path / "policy.json"
    config_path = tmp_path / "tracker.json"
    policy = IssueToolPolicy(creator="perf_eval", iteration=2, cap=2, allowed_types=("perf",))
    policy_path.write_text(policy.model_dump_json(), encoding="utf-8")
    config_path.write_text(IssueTrackerConfig.local().model_dump_json(), encoding="utf-8")
    IssueBoard(store_path)
    server = build_server(store_path, policy_path, config_path)
    calls: list[tuple[str, dict[str, object]]] = [
        ("list_issues", {}),
        ("search_issues", {"query": "kv"}),
        ("get_issue", {"issue_id": 1}),
        ("list_issues", {"status": "nope"}),
        ("create_issue", {"type": "bug", "title": "t", "description": "d"}),
        ("create_issue", {"type": "unknown", "title": "t", "description": "d"}),
        ("create_issue", {"type": "perf", "title": "KV fragmentation", "description": "Fix."}),
        ("create_issue", {"type": "perf", "title": "KV eviction", "description": "Tune."}),
        ("create_issue", {"type": "perf", "title": "Third", "description": "Over cap."}),
        ("list_issues", {}),
        ("list_issues", {"status": "open"}),
        ("search_issues", {"query": "kv"}),
        ("get_issue", {"issue_id": 1}),
    ]
    results = [
        f"### {name} {arguments}\n{asyncio.run(_call_tool(server, name, **arguments))}\n"
        for name, arguments in calls
    ]
    return "".join(results)


_CASES: dict[str, Callable[[Path], str]] = {
    "issue_rich": lambda _: render_issue(_RICH),
    "issue_minimal": lambda _: render_issue(_issue(1)),
    "index_board": lambda tmp_path: _index(tmp_path, _BOARD),
    "index_empty": lambda tmp_path: _index(tmp_path, []),
    "progress_log": _progress,
    "tool_results": _tools,
}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_issue_queue_agent_visible_text_matches_its_golden(tmp_path: Path, name: str) -> None:
    assert_exact_text(_SNAPSHOT_DIR / f"{name}.txt", _CASES[name](tmp_path))
