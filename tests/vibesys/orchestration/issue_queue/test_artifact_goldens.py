"""Byte-exact goldens for the issue-queue text people read outside the prompts.

The issue Markdown view, the board index and the run progress log are mirrored for a person.
Regenerate with
``UPDATE_PROMPT_SNAPSHOTS=1`` and review every fixture diff as a prompt diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.golden.helpers import assert_exact_text

from vibesys.issue_queue_board import IssueQueueTexts, render_all, render_issue
from vs_issue_tracker.api import (
    FileProgressLog,
    Issue,
    IssueEvent,
    IssueStatus,
    IssueType,
    LogProgress,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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


def _index(tmp_path: Path, issues: list[Issue]) -> str:
    render_all(tmp_path / "issues", issues)
    return (tmp_path / "issues" / "INDEX.md").read_text(encoding="utf-8")


def _progress(tmp_path: Path) -> str:
    log = FileProgressLog(tmp_path / "progress.md")
    texts = IssueQueueTexts({})
    for request in (
        LogProgress(
            key="k1",
            iteration=2,
            step="review",
            issue_id=4,
            payload_json=json.dumps(
                {"summary": "checked", "verdict": "pass", "files_touched": ["a.py"]}
            ),
        ),
        LogProgress(key="k2", iteration=3, step="performance", issue_id=None, payload_json="{}"),
    ):
        log.append(texts.progress_entry(request))
    return log.read()


_CASES: dict[str, Callable[[Path], str]] = {
    "issue_rich": lambda _: render_issue(_RICH),
    "issue_minimal": lambda _: render_issue(_issue(1)),
    "index_board": lambda tmp_path: _index(tmp_path, _BOARD),
    "index_empty": lambda tmp_path: _index(tmp_path, []),
    "progress_log": _progress,
}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_issue_queue_agent_visible_text_matches_its_golden(tmp_path: Path, name: str) -> None:
    assert_exact_text(_SNAPSHOT_DIR / f"{name}.txt", _CASES[name](tmp_path))
