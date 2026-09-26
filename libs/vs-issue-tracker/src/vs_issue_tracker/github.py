"""GitHub Issues adapter for issue_queue work items and run notes.

Issue types use ``vibesys:type/*`` labels. Lifecycle state uses one of
``vibesys:status/in_progress`` or ``vibesys:status/blocked`` labels, with
GitHub's native open/closed state representing open/closed. Structured events
are HTML-comment records in issue comments, so user descriptions and comments
remain untouched. A run's progress log is a separate issue tagged
``vibesys:progress-log``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from vs_github.api import GitHubCLI, GitHubClient
from vs_issue_tracker.core import (
    Issue,
    IssueBoard,
    IssueEvent,
    IssueStatus,
    IssueTracker,
    IssueType,
)
from vs_issue_tracker.errors import IssueTrackerLoadError

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable
    from pathlib import Path

_TYPE_PREFIX = "vibesys:type/"
_STATUS_PREFIX = "vibesys:status/"
_PROGRESS_LABEL = "vibesys:progress-log"
_EVENT_START = "<!-- vibesys:issue-event:v1\n"
_EVENT_END = "\n-->"
_TYPE_RANK = {IssueType.BUG: 0, IssueType.FEATURE: 1, IssueType.PERF: 2}


class _InvalidIssuePayloadError(ValueError):
    """Raised when GitHub returns an issue snapshot without a valid number."""

    def __init__(self, value: object) -> None:
        super().__init__(f"GitHub issue number must be an integer, got {value!r}")


class GitHubIssueTracker(IssueTracker):
    """Implement the issue-queue tracker contract using a GitHub repository."""

    def __init__(self, repository: str, *, cli: GitHubClient | None = None) -> None:
        """Authenticate, bind to a repository, and ensure metadata labels exist."""
        self._cli = cli or GitHubCLI()
        self._cli.ensure_authenticated()
        self.repository = repository
        self._ensure_metadata_labels()

    def _ensure_metadata_labels(self) -> None:
        for name in (
            *(f"{_TYPE_PREFIX}{value.value}" for value in IssueType),
            f"{_STATUS_PREFIX}in_progress",
            f"{_STATUS_PREFIX}blocked",
            _PROGRESS_LABEL,
        ):
            self._cli.ensure_label(self.repository, name)

    def _snapshots(self) -> builtins.list[dict[str, object]]:
        return [
            item
            for item in self._cli.list_issues(self.repository)
            if _label_names(item.get("labels"))
            and _PROGRESS_LABEL not in _label_names(item.get("labels"))
            and any(label.startswith(_TYPE_PREFIX) for label in _label_names(item.get("labels")))
        ]

    def _issue(self, issue_id: int) -> Issue | None:
        try:
            raw = self._cli.view_issue(self.repository, issue_id)
        except Exception as exc:
            if "not found" in str(exc).lower() or "could not resolve" in str(exc).lower():
                return None
            raise
        if _PROGRESS_LABEL in _label_names(raw.get("labels")):
            return None
        return _to_issue(raw)

    def create(
        self,
        *,
        type: IssueType | str,  # noqa: A002  # lint-waiver: LW-920414 [A002]; preserve the storage-neutral tracker create keyword.
        title: str,
        description: str,
        created_by: str,
        iteration: int,
    ) -> Issue:
        """Create an issue with its type label and initial structured event."""
        issue_type = type if isinstance(type, IssueType) else IssueType(type)
        issue_id = self._cli.create_issue(
            self.repository,
            title=title,
            body=description,
            labels=[f"{_TYPE_PREFIX}{issue_type.value}"],
        )
        event = _event(
            actor=created_by,
            action="create",
            iteration=iteration,
            note="",
            payload={"type": issue_type.value, "created_by": created_by},
        )
        self._write_event(issue_id, event)
        issue = self._issue(issue_id)
        if issue is None:
            raise RuntimeError(f"Created GitHub issue #{issue_id} could not be read back")  # noqa: TRY003  # lint-waiver: LW-920415 [TRY003]; identify an impossible post-create read failure.
        return issue

    def get(self, issue_id: int) -> Issue | None:
        """Return a VibeSys issue by GitHub issue number."""
        return self._issue(issue_id)

    def update_status(  # noqa: PLR0913  # lint-waiver: LW-920416 [PLR0913]; implement the shared tracker transition contract without packing fields.
        self,
        issue_id: int,
        status: IssueStatus | str,
        *,
        actor: str,
        iteration: int,
        note: str = "",
        payload: dict[str, Any] | None = None,
    ) -> Issue:
        """Apply native and label state, then append one transition event."""
        target = status if isinstance(status, IssueStatus) else IssueStatus(status)
        raw = self._cli.view_issue(self.repository, issue_id)
        current = _to_issue(raw)
        present_status_labels = _label_names(raw.get("labels")) & {
            f"{_STATUS_PREFIX}in_progress",
            f"{_STATUS_PREFIX}blocked",
        }
        wanted_status_labels = set(_status_labels(target))
        self._cli.edit_issue(
            self.repository,
            issue_id,
            add_labels=sorted(wanted_status_labels - present_status_labels),
            remove_labels=sorted(present_status_labels - wanted_status_labels),
        )
        if (current.status is IssueStatus.CLOSED) != (target is IssueStatus.CLOSED):
            self._cli.set_issue_state(
                self.repository, issue_id, issue_open=target is not IssueStatus.CLOSED
            )
        self._write_event(
            issue_id,
            _event(
                actor=actor,
                action=f"{current.status.value}->{target.value}",
                iteration=iteration,
                note=note,
                payload=payload,
            ),
        )
        return self._required(issue_id)

    def reopen_blocked(self, *, actor: str, iteration: int, note: str = "") -> builtins.list[int]:
        """Reopen blocked issues and record that their attempt budget reset."""
        blocked = self.list(status=IssueStatus.BLOCKED)
        for issue in blocked:
            self._cli.edit_issue(
                self.repository,
                issue.id,
                remove_labels=[f"{_STATUS_PREFIX}blocked"],
            )
            self._write_event(
                issue.id,
                _event(
                    actor=actor,
                    action="blocked->open",
                    iteration=iteration,
                    note=note,
                    payload={"attempts_reset": True},
                ),
            )
        return [issue.id for issue in blocked]

    def increment_attempts(
        self,
        issue_id: int,
        *,
        actor: str,
        iteration: int,
        note: str = "",
        payload: dict[str, Any] | None = None,
    ) -> Issue:
        """Append an attempt event and return the incremented issue projection."""
        self._required(issue_id)
        self._write_event(
            issue_id,
            _event(
                actor=actor,
                action="attempt",
                iteration=iteration,
                note=note,
                payload=payload,
            ),
        )
        return self._required(issue_id)

    def list(
        self,
        *,
        status: IssueStatus | str | None = None,
        type: IssueType | str | None = None,  # noqa: A002  # lint-waiver: LW-920417 [A002]; preserve the storage-neutral tracker filter keyword.
    ) -> builtins.list[Issue]:
        """List VibeSys issues, optionally filtered by status and type."""
        status_filter = (
            status if isinstance(status, IssueStatus) else IssueStatus(status) if status else None
        )
        type_filter = type if isinstance(type, IssueType) else IssueType(type) if type else None
        result = [
            _to_issue(self._cli.view_issue(self.repository, _number(item.get("number"))))
            for item in self._snapshots()
        ]
        if status_filter is not None:
            result = [issue for issue in result if issue.status is status_filter]
        if type_filter is not None:
            result = [issue for issue in result if issue.type is type_filter]
        return sorted(result, key=lambda issue: issue.id)

    def search(self, query: str) -> builtins.list[Issue]:
        """Search titles and descriptions using the local adapter's AND terms."""
        terms = [term.strip().lower() for term in query.split(",") if term.strip()]
        if not terms:
            return []
        return [
            issue
            for issue in self.list()
            if all(term in f"{issue.title}\n{issue.description}".lower() for term in terms)
        ]

    def open_count_by_creator_in_iter(self, creator: str, iteration: int) -> int:
        """Count issues created by one actor in one iteration, any status."""
        return sum(
            1
            for issue in self.list()
            if issue.created_by == creator and issue.created_iter == iteration
        )

    def next_open(self) -> Issue | None:
        """Return the highest-priority open issue, then oldest first."""
        candidates = self.list(status=IssueStatus.OPEN)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda issue: (_TYPE_RANK[issue.type], issue.created_at, issue.id),
        )

    def _required(self, issue_id: int) -> Issue:
        issue = self._issue(issue_id)
        if issue is None:
            raise KeyError(f"issue #{issue_id} not found")  # noqa: TRY003  # lint-waiver: LW-920418 [TRY003]; include the missing remote issue number.
        return issue

    def _write_event(self, issue_id: int, event: IssueEvent) -> None:
        serialized = json.dumps(event.model_dump(), sort_keys=True, separators=(",", ":"))
        self._cli.comment_issue(
            self.repository, issue_id, f"{_EVENT_START}{serialized}{_EVENT_END}"
        )


def _event(
    *, actor: str, action: str, iteration: int, note: str, payload: dict[str, Any] | None
) -> IssueEvent:
    return IssueEvent(
        timestamp=datetime.now(UTC).isoformat(),
        actor=actor,
        action=action,
        iteration=iteration,
        note=note,
        payload=payload,
    )


def _to_issue(raw: dict[str, object]) -> Issue:
    labels = _label_names(raw.get("labels"))
    type_value = next(
        (label.removeprefix(_TYPE_PREFIX) for label in labels if label.startswith(_TYPE_PREFIX)),
        None,
    )
    if type_value is None:
        raise ValueError(f"GitHub issue #{raw.get('number')} lacks a VibeSys type label")  # noqa: TRY003  # lint-waiver: LW-920419 [TRY003]; identify malformed tracker metadata by issue number.
    events = _events(raw.get("comments"))
    create = next((event for event in events if event.action == "create"), None)
    state = str(raw.get("state", "OPEN")).lower()
    if state == "closed":
        status = IssueStatus.CLOSED
    elif f"{_STATUS_PREFIX}blocked" in labels:
        status = IssueStatus.BLOCKED
    elif f"{_STATUS_PREFIX}in_progress" in labels:
        status = IssueStatus.IN_PROGRESS
    else:
        status = IssueStatus.OPEN
    number = _number(raw["number"])
    created_at = str(raw.get("createdAt") or datetime.now(UTC).isoformat())
    return Issue(
        id=number,
        type=IssueType(type_value),
        title=str(raw.get("title", "")),
        description=str(raw.get("body") or ""),
        status=status,
        created_by=str((create.payload or {}).get("created_by") if create else _author(raw)),
        created_iter=create.iteration if create and create.iteration is not None else 0,
        created_at=created_at,
        updated_at=str(raw.get("updatedAt") or created_at),
        attempts=_attempt_count(events),
        history=events,
        closed_iter=_closed_iteration(events),
    )


def _events(comments: object) -> list[IssueEvent]:
    if not isinstance(comments, list):
        return []
    events: list[IssueEvent] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body", ""))
        if not body.startswith(_EVENT_START):
            continue
        if not body.endswith(_EVENT_END):
            raise IssueTrackerLoadError.issue_event_unclosed()
        try:
            value = json.loads(body[len(_EVENT_START) : -len(_EVENT_END)])
            events.append(IssueEvent.model_validate(value))
        except (json.JSONDecodeError, ValueError) as exc:
            raise IssueTrackerLoadError.invalid_issue_event() from exc
    return events


def _attempt_count(events: list[IssueEvent]) -> int:
    attempts = 0
    for event in events:
        if event.action == "attempt":
            attempts += 1
        elif event.action == "blocked->open" and (event.payload or {}).get("attempts_reset"):
            attempts = 0
    return attempts


def _closed_iteration(events: list[IssueEvent]) -> int | None:
    closed_iter = None
    for event in events:
        if event.action.endswith(("->closed", "->blocked")):
            closed_iter = event.iteration
        elif event.action == "blocked->open" and (event.payload or {}).get("attempts_reset"):
            closed_iter = None
    return closed_iter


def _label_names(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(label.get("name"))
        for label in value
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    }


def _author(raw: dict[str, object]) -> str:
    author = raw.get("author")
    if isinstance(author, dict):
        return str(author.get("login", "unknown"))
    return "unknown"


def _number(value: object) -> int:
    if not isinstance(value, (int, str)):
        raise _InvalidIssuePayloadError(value)
    return int(value)


def _status_labels(status: IssueStatus) -> list[str]:
    if status is IssueStatus.IN_PROGRESS:
        return [f"{_STATUS_PREFIX}in_progress"]
    if status is IssueStatus.BLOCKED:
        return [f"{_STATUS_PREFIX}blocked"]
    return []


def open_issue_tracker(
    backend: Literal["local", "github"],
    *,
    local_path: Path,
    repository: str | None = None,
    on_change: Callable[[], None] | None = None,
) -> IssueTracker:
    """Construct the selected issue tracker behind the IssueTracker contract."""
    if backend == "local":
        return IssueBoard(local_path, on_change=on_change)
    if repository is None:
        raise ValueError("repository is required for the GitHub issue backend")  # noqa: TRY003  # lint-waiver: LW-920420 [TRY003]; name the missing backend configuration field.
    return GitHubIssueTracker(repository)


__all__ = ["GitHubIssueTracker", "open_issue_tracker"]
