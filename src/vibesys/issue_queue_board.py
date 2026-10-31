"""The issue board of a core run: its tracker, its human-readable mirror and the texts it needs.

The pure policy renders nothing and opens nothing. This module opens the tracker the run
selected, mirrors the board as Markdown a person can read, renders the bodies and progress
entries the strategy asks the board owner for, and builds the owner of the board operations.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

from pydantic import JsonValue, TypeAdapter

from vibesys.orchestration.issue_queue.launch import IssueQueueLaunch
from vs_issue_tracker.api import (
    Issue,
    IssueStatus,
    IssueTracker,
    board_operation_registrations,
    open_issue_tracker_session,
)
from vs_prompts.api import RenderedPrompt, TemplateRenderer
from vs_runtime.api.core import BOARD_KEY, IssueBoardOwner, OperationEntry, read_or_keyed

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_issue_tracker.api import LogProgress
    from vs_runtime.api import OwnedOperationResources

_RENDERER = TemplateRenderer(
    Path(__file__).parent / "orchestration" / "issue_queue" / "core_policy" / "prompts"
)
_BOARD_DIRECTORY = "issue-board"
_ISSUES_VIEW = PurePosixPath("board") / "issues"
_ISSUE_FILE = re.compile(r"[0-9]{4}-.*\.md")
_PAYLOAD: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

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


class BoardDocuments(Protocol):
    """Where the Markdown mirror of the board is kept: named documents in one store."""

    def entries(self, relative_directory: str | PurePosixPath) -> tuple[str, ...]:
        """Names directly inside one directory of the store."""
        ...

    def write_bytes(self, relative_path: str | PurePosixPath, contents: bytes) -> None:
        """Replace one document atomically."""
        ...

    def delete(self, relative_path: str | PurePosixPath) -> bool:
        """Remove one document, returning whether it existed."""
        ...


def render_issue(issue: Issue) -> RenderedPrompt:
    """Render one issue and its attempt history."""
    return _RENDERER.render_template("board/issue.j2", issue=issue)


def render_all(source: IssueTracker | list[Issue]) -> dict[str, RenderedPrompt]:
    """Render the derived Markdown view of an issue snapshot: file name to document."""
    issues = source if isinstance(source, list) else source.list()
    documents = {_filename(issue): render_issue(issue) for issue in issues}
    groups = [
        (status, matching)
        for status in _STATUS_ORDER
        if (matching := [issue for issue in issues if issue.status is status])
    ]
    filenames = {issue.id: _filename(issue) for issue in issues}
    documents["INDEX.md"] = _RENDERER.render_template(
        "board/index.j2", groups=groups, filenames=filenames
    )
    return documents


def publish_view(store: BoardDocuments, issues: list[Issue]) -> None:
    """Replace the board's Markdown mirror in ``store`` with the view of ``issues``."""
    documents = render_all(issues)
    for name in store.entries(_ISSUES_VIEW):
        if _ISSUE_FILE.fullmatch(name) and name not in documents:
            store.delete(_ISSUES_VIEW / name)
    for name, text in documents.items():
        store.write_bytes(_ISSUES_VIEW / name, text.encode("utf-8"))


class IssueQueueTexts:
    """Render bootstrap issue bodies and progress entries from the issue queue's templates."""

    def __init__(self, variables: Mapping[str, object]) -> None:
        """Bind the run-level template variables (the run facts) the bodies read."""
        self._variables = dict(variables)

    def issue_body(self, template: str) -> str:
        """The description of an issue the strategy filed from a template."""
        return _RENDERER.render_template(f"{template}.j2", **self._variables)

    def progress_entry(self, request: LogProgress) -> str:
        """One human-readable progress entry for a turn's recorded reply."""
        payload = _PAYLOAD.validate_json(request.payload_json) if request.payload_json else {}
        return _RENDERER.render_template(
            "board/progress.j2",
            iteration=request.iteration,
            step=request.step,
            issue_id=request.issue_id,
            payload=payload if isinstance(payload, dict) else {},
        )


def board_entries(resources: OwnedOperationResources) -> tuple[OperationEntry, ...]:
    """The board operations, owned by an owner over the tracker the run selected.

    The local tracker keeps its board and progress log in the run's portable state, beside the
    Markdown mirror of the board a person can read; the GitHub tracker keeps them in the
    repository the options name.
    """
    options = IssueQueueLaunch.model_validate(resources.options)
    namespace = resources.project.state.portable_namespace(resources.run_id, _BOARD_DIRECTORY)
    directory = namespace.external_directory("board")

    def mirror(issues: list[Issue]) -> None:
        publish_view(namespace, issues)

    session = open_issue_tracker_session(
        options.tracker,
        local_store_path=directory / "issues.json",
        local_progress_path=directory / "progress.md",
        run_id=resources.run_id,
        view_sink=mirror,
        github_cli=resources.github,
        clock=resources.clock,
        threads=resources.threads,
    )
    owner = IssueBoardOwner(
        session.tracker, session.progress, IssueQueueTexts(resources.variables), publish=mirror
    )
    return tuple(
        OperationEntry.owned(registration, owner, read_or_keyed(registration, BOARD_KEY))
        for registration in board_operation_registrations()
    )


__all__ = [
    "BoardDocuments",
    "IssueQueueTexts",
    "board_entries",
    "publish_view",
    "render_all",
    "render_issue",
]
