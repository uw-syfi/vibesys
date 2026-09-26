"""Tests for issue-queue policy artifacts."""

from pathlib import Path
from typing import Required, TypedDict, Unpack

from vibesys.orchestrations.issue_queue.artifacts import render_all, render_issue
from vs_issue_board.api import Issue, IssueBoard, IssueEvent, IssueStatus, IssueType


class _IssueOptions(TypedDict, total=False):
    id: int
    title: str
    description: str
    type: IssueType
    status: IssueStatus
    attempts: int
    created_by: str
    history: list[IssueEvent] | None


class _EventOptions(TypedDict, total=False):
    actor: Required[str]
    action: Required[str]
    iteration: int
    note: str
    payload: dict | None
    timestamp: str


def _make_event(**options: Unpack[_EventOptions]) -> IssueEvent:
    return IssueEvent(
        timestamp=options.get("timestamp", "2026-04-08T12:01:00"),
        actor=options["actor"],
        action=options["action"],
        iteration=options.get("iteration", 1),
        note=options.get("note", ""),
        payload=options.get("payload"),
    )


def _make_issue(**options: Unpack[_IssueOptions]) -> Issue:
    now = "2026-04-08T12:00:00"
    creator = options.get("created_by", "loop:bootstrap")
    return Issue(
        id=options.get("id", 1),
        type=options.get("type", IssueType.FEATURE),
        title=options.get("title", "Test issue"),
        description=options.get("description", "A test issue."),
        status=options.get("status", IssueStatus.OPEN),
        created_by=creator,
        created_iter=1,
        created_at=now,
        updated_at=now,
        attempts=options.get("attempts", 0),
        history=options.get("history")
        or [_make_event(actor=creator, action="create", timestamp=now)],
    )


def test_render_issue_includes_policy_state_and_paid_turn_evidence() -> None:
    issue = _make_issue(
        id=42,
        title="Build server",
        description="Run a FastAPI server on port 8000.",
        attempts=1,
        history=[
            _make_event(actor="loop:bootstrap", action="create"),
            _make_event(
                actor="implementer",
                action="attempt",
                note="built it",
                payload={
                    "summary": "built x",
                    "files_touched": ["server.py", "tests/test_x.py"],
                    "self_check": "all green",
                },
            ),
            _make_event(
                actor="judge",
                action="in_progress->open",
                note="not done",
                payload={
                    "verdict": "fail",
                    "analysis": "the endpoint is missing",
                    "feedback": "add /v1/completions",
                },
            ),
        ],
    )

    markdown = render_issue(issue)

    assert "# #0042 - Build server" in markdown
    assert "- **Type**: feature" in markdown
    assert "- **Status**: open" in markdown
    assert "- **Attempts**: 1" in markdown
    assert "Run a FastAPI server on port 8000." in markdown
    assert "**implementer** attempt" in markdown
    assert "**Summary**: built x" in markdown
    assert "`server.py`" in markdown
    assert "`tests/test_x.py`" in markdown
    assert "**Self Check**: all green" in markdown
    assert "**judge** in_progress->open" in markdown
    assert "**Verdict**: fail" in markdown
    assert "**Analysis**: the endpoint is missing" in markdown
    assert "**Feedback**: add /v1/completions" in markdown


def test_render_issue_preserves_markdown_and_handles_missing_payload() -> None:
    issue = _make_issue(
        description="# A markdown heading\n\n- a list",
        history=[
            _make_event(actor="loop:bootstrap", action="create"),
            _make_event(actor="implementer", action="attempt", payload=None),
        ],
    )

    markdown = render_issue(issue)

    assert "# A markdown heading" in markdown
    assert "- a list" in markdown
    assert "**implementer** attempt" in markdown


def test_render_all_projects_board_in_status_order_and_is_idempotent(tmp_path: Path) -> None:
    board = IssueBoard(tmp_path / "issues.json")
    opened = board.create(
        type=IssueType.FEATURE,
        title="Build server",
        description="d",
        created_by="loop:bootstrap",
        iteration=1,
    )
    blocked = board.create(
        type=IssueType.BUG,
        title="Crash | startup",
        description="d",
        created_by="judge",
        iteration=1,
    )
    board.update_status(opened.id, IssueStatus.IN_PROGRESS, actor="loop", iteration=1)
    board.increment_attempts(
        opened.id,
        actor="implementer",
        iteration=1,
        payload={"summary": "did stuff", "files_touched": ["s.py"], "self_check": "ok"},
    )
    board.update_status(blocked.id, IssueStatus.BLOCKED, actor="loop", iteration=1)
    issues_dir = tmp_path / "nested" / "issues"

    render_all(issues_dir, board)
    first = {path.name: path.read_bytes() for path in issues_dir.glob("*.md")}
    render_all(issues_dir, board)
    second = {path.name: path.read_bytes() for path in issues_dir.glob("*.md")}

    assert first == second
    assert sorted(first) == ["0001-build-server.md", "0002-crash-startup.md", "INDEX.md"]
    index = first["INDEX.md"].decode()
    assert index.index("## in_progress") < index.index("## blocked")
    assert "Crash \\| startup" in index
    assert "did stuff" in first["0001-build-server.md"].decode()
    assert "`s.py`" in first["0001-build-server.md"].decode()


def test_render_all_removes_stale_derived_files(tmp_path: Path) -> None:
    board = IssueBoard(tmp_path / "issues.json")
    board.create(
        type=IssueType.BUG,
        title="Current issue",
        description="d",
        created_by="judge",
        iteration=1,
    )
    issues_dir = tmp_path / "issues"
    issues_dir.mkdir()
    stale = issues_dir / "0001-old-title.md"
    stale.write_text("stale", encoding="utf-8")

    render_all(issues_dir, board)

    assert not stale.exists()
    assert (issues_dir / "0001-current-issue.md").is_file()
    assert (issues_dir / "INDEX.md").is_file()


def test_render_all_writes_empty_index(tmp_path: Path) -> None:
    board = IssueBoard(tmp_path / "issues.json")

    render_all(tmp_path / "issues", board)

    assert "no issues yet" in (tmp_path / "issues" / "INDEX.md").read_text(encoding="utf-8")
