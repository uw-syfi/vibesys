"""Per-issue markdown renderer for the issue-loop.

This module turns ``Issue`` objects into a human-readable directory of
markdown files mirrored next to ``logs/issues.json``:

    logs/
      issues.json                      # canonical, machine-readable
      issues/
        INDEX.md                       # status table
        0001-build-fastapi-server.md   # one file per issue
        0002-add-streaming-completions.md
        ...

The renderer is invoked by ``IssueBoard``'s ``on_change`` callback so the
markdown view is always
re-generated after every successful save. Writes are atomic via the same
tmp+rename pattern the store uses.

Pure functions only — this module owns no state and reads no globals. The
only side-effect surface is the ``render_*`` functions, which write files.
The caller is responsible for passing the right paths.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from vs_issue_board import (
    Issue,
    IssueBoard,
    IssueEvent,
    IssueStatus,
)

_DEFAULT_SLUG = "untitled"
_SLUG_MAX_LEN = 40

# Order in which status groups appear in INDEX.md (top → bottom).
_STATUS_DISPLAY_ORDER: tuple[IssueStatus, ...] = (
    IssueStatus.IN_PROGRESS,
    IssueStatus.OPEN,
    IssueStatus.BLOCKED,
    IssueStatus.CLOSED,
)


# ---------------------------------------------------------------------------
# slugify + filename
# ---------------------------------------------------------------------------


def slugify(title: str, max_len: int = _SLUG_MAX_LEN) -> str:
    """Convert a free-form title into a filesystem-safe slug.

    NFKD-normalises unicode then drops combining marks (so ``café`` →
    ``cafe``), lowercases, replaces any run of non-``[a-z0-9]`` characters
    with a single ``-`` (so unicode separators like the em-dash become
    proper word boundaries), strips leading/trailing dashes, truncates to
    ``max_len``, and strips again. Falls back to ``"untitled"`` if empty.
    """
    if not title:
        return _DEFAULT_SLUG
    # NFKD decomposes "é" → "e" + COMBINING ACUTE; we then drop the
    # combining mark via category 'Mn'. Crucially, we do NOT encode to
    # ASCII here — non-decomposable unicode chars (em-dash, etc.) are
    # left in place so the regex below treats them as separators.
    normalised = unicodedata.normalize("NFKD", title)
    no_marks = "".join(ch for ch in normalised if not unicodedata.combining(ch))
    lowered = no_marks.lower()
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered)
    stripped = collapsed.strip("-")
    if not stripped:
        return _DEFAULT_SLUG
    truncated = stripped[:max_len].rstrip("-")
    return truncated or _DEFAULT_SLUG


def issue_md_filename(issue: Issue) -> str:
    """Stable filename for an issue's markdown file: ``{id:04d}-{slug}.md``."""
    return f"{issue.id:04d}-{slugify(issue.title)}.md"


def issue_md_path(issues_dir: Path, issue: Issue) -> Path:
    """Absolute path to ``issues_dir / {id:04d}-{slug}.md``."""
    return issues_dir / issue_md_filename(issue)


# ---------------------------------------------------------------------------
# atomic write
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via tmp + ``os.replace``.

    Mirrors ``IssueBoard._save_locked`` so a crash mid-render leaves the
    target either at its previous content or fully overwritten — never half.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# event → markdown helpers
# ---------------------------------------------------------------------------


def _render_event_bullet(evt: IssueEvent) -> str:
    """One-line bullet for the timeline section."""
    iter_part = f" (iter {evt.iteration})" if evt.iteration is not None else ""
    note_part = f" — {evt.note}" if evt.note else ""
    return f"- `{evt.timestamp}` **{evt.actor}** {evt.action}{iter_part}{note_part}"


def _render_implementer_payload(payload: dict) -> str:
    """Render an IssueImplementerResponse payload as a markdown section body."""
    lines: list[str] = []
    summary = payload.get("summary", "").strip()
    if summary:
        lines.append(f"**Summary**: {summary}")
        lines.append("")
    files_touched = payload.get("files_touched") or []
    if files_touched:
        lines.append("**Files touched**:")
        for fp in files_touched:
            lines.append(f"- `{fp}`")
        lines.append("")
    self_check = payload.get("self_check", "").strip()
    if self_check:
        lines.append(f"**Self-check**: {self_check}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_judge_payload(payload: dict) -> str:
    """Render an IssueJudgeResponse payload as a markdown section body."""
    lines: list[str] = []
    verdict = payload.get("verdict", "")
    if verdict:
        lines.append(f"**Verdict**: {str(verdict).upper()}")
        lines.append("")
    analysis = payload.get("analysis", "").strip()
    if analysis:
        lines.append(f"**Analysis**: {analysis}")
        lines.append("")
    feedback = payload.get("feedback", "").strip()
    if feedback:
        lines.append(f"**Feedback**: {feedback}")
        lines.append("")
    new_issues = payload.get("new_issues_filed") or []
    if new_issues:
        ids = ", ".join(f"#{i}" for i in new_issues)
        lines.append(f"**New issues filed**: {ids}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _is_judge_event(evt: IssueEvent) -> bool:
    """An event is a judge verdict iff it's a status change emitted by ``judge``."""
    return evt.actor == "judge" and "->" in evt.action


def _is_implementer_event(evt: IssueEvent) -> bool:
    """An event is an implementer attempt iff its action is ``"attempt"``."""
    return evt.action == "attempt"


# ---------------------------------------------------------------------------
# renderer functions
# ---------------------------------------------------------------------------


def render_issue_markdown(issue: Issue) -> str:
    """Return the full markdown body for one issue. Pure function."""
    parts: list[str] = []

    # Header
    parts.append(f"# #{issue.id:04d} — {issue.title}\n")
    parts.append(f"- **Type**: {issue.type.value}")
    parts.append(f"- **Status**: {issue.status.value}")
    parts.append(f"- **Attempts**: {issue.attempts}")
    parts.append(f"- **Created by**: {issue.created_by} (iter {issue.created_iter})")
    parts.append(f"- **Created at**: {issue.created_at}")
    parts.append(f"- **Updated at**: {issue.updated_at}")
    if issue.closed_iter is not None:
        parts.append(f"- **Closed at iter**: {issue.closed_iter}")
    parts.append("")

    # Description
    parts.append("## Description\n")
    parts.append(issue.description.rstrip() + "\n")

    # Timeline
    parts.append("## Timeline\n")
    if issue.history:
        for evt in issue.history:
            parts.append(_render_event_bullet(evt))
    else:
        parts.append("_(no events recorded)_")
    parts.append("")

    # Per-attempt detail sections (only events with payload)
    impl_attempt = 0
    judge_attempt = 0
    detail_sections: list[str] = []
    for evt in issue.history:
        if _is_implementer_event(evt) and evt.payload:
            impl_attempt += 1
            heading = f"### Implementer attempt {impl_attempt}"
            iter_suffix = f" (iter {evt.iteration})" if evt.iteration is not None else ""
            detail_sections.append(f"{heading}{iter_suffix}\n")
            detail_sections.append(_render_implementer_payload(evt.payload))
        elif _is_judge_event(evt) and evt.payload:
            judge_attempt += 1
            heading = f"### Judge review {judge_attempt}"
            iter_suffix = f" (iter {evt.iteration})" if evt.iteration is not None else ""
            detail_sections.append(f"{heading}{iter_suffix}\n")
            detail_sections.append(_render_judge_payload(evt.payload))
    if detail_sections:
        parts.append("## Attempt detail\n")
        parts.extend(detail_sections)

    return "\n".join(parts).rstrip() + "\n"


def render_index_markdown(issues: list[Issue]) -> str:
    """Return the markdown body for INDEX.md. Pure function."""
    lines: list[str] = []
    lines.append("# Issue Index\n")

    if not issues:
        lines.append("_(no issues yet)_\n")
        return "\n".join(lines)

    by_status: dict[IssueStatus, list[Issue]] = {s: [] for s in _STATUS_DISPLAY_ORDER}
    for issue in issues:
        # Defensive: unknown status sorts to the bottom under CLOSED.
        bucket = by_status.get(issue.status)
        if bucket is None:
            by_status.setdefault(issue.status, []).append(issue)
        else:
            bucket.append(issue)

    for status in _STATUS_DISPLAY_ORDER:
        bucket = by_status.get(status, [])
        if not bucket:
            continue
        lines.append(f"## {status.value} ({len(bucket)})\n")
        lines.append("| ID | Type | Title | Attempts | Created iter | Updated |")
        lines.append("|---:|------|-------|---------:|-------------:|---------|")
        for issue in sorted(bucket, key=lambda i: i.id):
            link = f"[{_escape_pipe(issue.title)}]({issue_md_filename(issue)})"
            lines.append(
                f"| {issue.id} | {issue.type.value} | {link} | "
                f"{issue.attempts} | {issue.created_iter} | {issue.updated_at} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _escape_pipe(text: str) -> str:
    """Escape ``|`` so it doesn't break markdown table cells."""
    return text.replace("|", "\\|")


def render_issue_file(issues_dir: Path, issue: Issue) -> Path:
    """(Re)write the per-issue markdown file. Returns the written path."""
    path = issue_md_path(issues_dir, issue)
    _atomic_write_text(path, render_issue_markdown(issue))
    return path


def render_index_file(issues_dir: Path, issues: list[Issue]) -> Path:
    """(Re)write ``INDEX.md`` covering all issues."""
    path = issues_dir / "INDEX.md"
    _atomic_write_text(path, render_index_markdown(issues))
    return path


def render_all(issues_dir: Path, store: IssueBoard) -> None:
    """Re-render every per-issue file plus ``INDEX.md`` from the store.

    Single entry point used by ``IssueBoard``'s ``on_change`` callback. The
    cost is one atomic write per issue + one for the index — trivial for
    realistic issue counts (~tens) and idempotent.

    NOTE on title rename: the filename derives from the (currently
    immutable) title via ``issue_md_filename``. If issue rename is ever
    added, this function will write a new file under the new slug and the
    old file will linger as an orphan. Add cleanup logic at that time.
    """
    issues_dir.mkdir(parents=True, exist_ok=True)
    issues = store.list()
    for issue in issues:
        render_issue_file(issues_dir, issue)
    render_index_file(issues_dir, issues)
