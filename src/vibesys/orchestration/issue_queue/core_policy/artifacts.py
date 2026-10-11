"""Policy-owned issue-board and human-readable artifact rendering."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from vibesys.orchestration.issue_queue.core_policy.prompts import issue_index, issue_markdown
from vs_issue_tracker.api import Issue, IssueStatus, IssueTracker

if TYPE_CHECKING:
    from pathlib import Path

    from vs_prompts.api import RenderedPrompt

_STATUS_ORDER = (
    IssueStatus.IN_PROGRESS,
    IssueStatus.OPEN,
    IssueStatus.BLOCKED,
    IssueStatus.CLOSED,
)


def _slug(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title)
    ascii_words = "".join(char for char in normalized if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9]+", "-", ascii_words.lower()).strip("-")[:40].rstrip("-")
    return value or "untitled"


def _filename(issue: Issue) -> str:
    return f"{issue.id:04d}-{_slug(issue.title)}.md"


def _write(path: Path, text: RenderedPrompt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def render_issue(issue: Issue) -> RenderedPrompt:
    """Render one issue and its attempt history."""
    return issue_markdown(issue)


def render_all(directory: Path, source: IssueTracker | list[Issue]) -> None:
    """Regenerate the derived Markdown view from an issue snapshot."""
    issues = source if isinstance(source, list) else source.list()
    directory.mkdir(parents=True, exist_ok=True)
    expected = {_filename(issue) for issue in issues}
    for stale in directory.glob("[0-9][0-9][0-9][0-9]-*.md"):
        if stale.name not in expected:
            stale.unlink()
    for issue in issues:
        _write(directory / _filename(issue), render_issue(issue))
    groups = [
        (status, matching)
        for status in _STATUS_ORDER
        if (matching := [issue for issue in issues if issue.status is status])
    ]
    filenames = {issue.id: _filename(issue) for issue in issues}
    _write(directory / "INDEX.md", issue_index(groups, filenames))


__all__ = ["render_all", "render_issue"]
