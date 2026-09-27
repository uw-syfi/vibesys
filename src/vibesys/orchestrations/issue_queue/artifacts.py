"""Policy-owned issue-board and human-readable artifact rendering."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Any

from vs_issue_tracker.api import Issue, IssueStatus, IssueTracker, ProgressLog

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _payload_lines(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in ("summary", "self_check", "verdict", "analysis", "feedback"):
        value = payload.get(key)
        if value:
            lines.append(f"- **{key.replace('_', ' ').title()}**: {value}")
    files = payload.get("files_touched") or []
    if files:
        lines.append("- **Files touched**: " + ", ".join(f"`{path}`" for path in files))
    return lines


def render_issue(issue: Issue) -> str:
    """Render one issue and its attempt history."""
    lines = [
        f"# #{issue.id:04d} - {issue.title}",
        "",
        f"- **Type**: {issue.type.value}",
        f"- **Status**: {issue.status.value}",
        f"- **Attempts**: {issue.attempts}",
        "",
        "## Description",
        "",
        issue.description,
        "",
        "## Timeline",
        "",
    ]
    for event in issue.history:
        iteration = f" (iteration {event.iteration})" if event.iteration is not None else ""
        note = f": {event.note}" if event.note else ""
        lines.append(f"- `{event.timestamp}` **{event.actor}** {event.action}{iteration}{note}")
        if event.payload:
            lines.extend(_payload_lines(event.payload))
    return "\n".join(lines).rstrip() + "\n"


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

    index = ["# Issue Index", ""]
    for status in _STATUS_ORDER:
        matching = [issue for issue in issues if issue.status is status]
        if not matching:
            continue
        index.extend((f"## {status.value} ({len(matching)})", ""))
        for issue in matching:
            title = issue.title.replace("|", "\\|")
            index.append(
                f"- [#{issue.id} {title}]({_filename(issue)}) "
                f"({issue.type.value}, {issue.attempts} attempts)"
            )
        index.append("")
    if not issues:
        index.extend(("_(no issues yet)_", ""))
    _write(directory / "INDEX.md", "\n".join(index))


def append_progress(progress: ProgressLog, heading: str, response: BaseModel) -> None:
    """Append a compact human-readable record of one paid turn."""
    payload = response.model_dump(mode="json")
    lines = [f"## {heading}", "", *_payload_lines(payload), ""]
    progress.append("\n".join(lines) + "\n")


__all__ = ["append_progress", "render_all", "render_issue"]
