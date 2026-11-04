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
from typing import TYPE_CHECKING, Any, Literal

from vs_decode.api import Unreadable, decode_json
from vs_issue_tracker.core import (
    Issue,
    IssueBoard,
    IssueEvent,
    IssueStatus,
    IssueTracker,
    IssueType,
    timestamp_at,
)
from vs_issue_tracker.errors import IssueTrackerLoadError

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable
    from pathlib import Path

    from vs_github.api import GitHubClient
    from vs_sim.api import Clock, Threads

_TYPE_PREFIX = "vibesys:type/"
_STATUS_PREFIX = "vibesys:status/"
_PROGRESS_LABEL = "vibesys:progress-log"
_EVENT_START = "<!-- vibesys:issue-event:v1\n"
_EVENT_END = "\n-->"
_MAX_EVENT_BYTES = 256 * 1024
"""The largest issue event read out of one comment (GitHub caps a comment at 64 KiB)."""
_KEY_START = "<!-- vibesys:issue-key:v1 "
_KEY_END = " -->"
_TYPE_RANK = {IssueType.BUG: 0, IssueType.FEATURE: 1, IssueType.PERF: 2}


class _InvalidIssuePayloadError(ValueError):
    """Raised when GitHub returns an issue snapshot without a valid number."""

    def __init__(self, value: object) -> None:
        super().__init__(f"GitHub issue number must be an integer, got {value!r}")


class GitHubIssueTracker(IssueTracker):
    """Implement the issue-queue tracker contract using a GitHub repository."""

    def __init__(
        self,
        repository: str,
        *,
        cli: GitHubClient,
        clock: Clock,
    ) -> None:
        """Authenticate, bind to a repository, and ensure metadata labels exist.

        ``clock`` (epoch seconds) stamps the events this tracker writes.
        """
        self._cli = cli
        self._clock = clock
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
        return _to_issue(raw, observed_at=timestamp_at(self._clock))

    def create(  # noqa: PLR0913  # lint-waiver: LW-771008 [PLR0913]; the create contract names independent fields plus the idempotency key.
        self,
        *,
        type: IssueType | str,  # noqa: A002  # lint-waiver: LW-771009 [A002]; preserve the storage-neutral tracker create keyword.
        title: str,
        description: str,
        created_by: str,
        iteration: int,
        key: str | None = None,
    ) -> Issue:
        """Create an issue with its type label and initial structured event.

        A key is also written into the issue body as a hidden marker, in the same call that
        creates the issue, so a crash before the create event is written still leaves the key
        findable.
        """
        issue_type = type if isinstance(type, IssueType) else IssueType(type)
        issue_id = self._cli.create_issue(
            self.repository,
            title=title,
            body=description if key is None else f"{description}\n\n{_KEY_START}{key}{_KEY_END}",
            labels=[f"{_TYPE_PREFIX}{issue_type.value}"],
        )
        event = _event(
            timestamp=timestamp_at(self._clock),
            actor=created_by,
            action="create",
            iteration=iteration,
            note="",
            payload={"type": issue_type.value, "created_by": created_by},
            key=key,
        )
        self._write_event(issue_id, event)
        issue = self._issue(issue_id)
        if issue is None:
            raise RuntimeError(f"Created GitHub issue #{issue_id} could not be read back")  # noqa: TRY003  # lint-waiver: LW-771010 [TRY003]; identify an impossible post-create read failure.
        return issue

    def get(self, issue_id: int) -> Issue | None:
        """Return a VibeSys issue by GitHub issue number."""
        return self._issue(issue_id)

    def update_status(  # noqa: PLR0913  # lint-waiver: LW-771011 [PLR0913]; implement the shared tracker transition contract without packing fields.
        self,
        issue_id: int,
        status: IssueStatus | str,
        *,
        actor: str,
        iteration: int,
        note: str = "",
        payload: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> Issue:
        """Apply native and label state, then append one transition event."""
        target = status if isinstance(status, IssueStatus) else IssueStatus(status)
        raw = self._cli.view_issue(self.repository, issue_id)
        current = _to_issue(raw, observed_at=timestamp_at(self._clock))
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
                timestamp=timestamp_at(self._clock),
                actor=actor,
                action=f"{current.status.value}->{target.value}",
                iteration=iteration,
                note=note,
                payload=payload,
                key=key,
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
                    timestamp=timestamp_at(self._clock),
                    actor=actor,
                    action="blocked->open",
                    iteration=iteration,
                    note=note,
                    payload={"attempts_reset": True},
                ),
            )
        return [issue.id for issue in blocked]

    def increment_attempts(  # noqa: PLR0913  # lint-waiver: LW-771012 [PLR0913]; the increment_attempts contract names independent fields plus the idempotency key.
        self,
        issue_id: int,
        *,
        actor: str,
        iteration: int,
        note: str = "",
        payload: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> Issue:
        """Append an attempt event and return the incremented issue projection."""
        self._required(issue_id)
        self._write_event(
            issue_id,
            _event(
                timestamp=timestamp_at(self._clock),
                actor=actor,
                action="attempt",
                iteration=iteration,
                note=note,
                payload=payload,
                key=key,
            ),
        )
        return self._required(issue_id)

    def list(
        self,
        *,
        status: IssueStatus | str | None = None,
        type: IssueType | str | None = None,  # noqa: A002  # lint-waiver: LW-771013 [A002]; preserve the storage-neutral tracker filter keyword.
    ) -> builtins.list[Issue]:
        """List VibeSys issues, optionally filtered by status and type."""
        status_filter = (
            status if isinstance(status, IssueStatus) else IssueStatus(status) if status else None
        )
        type_filter = type if isinstance(type, IssueType) else IssueType(type) if type else None
        result = [
            _to_issue(
                self._cli.view_issue(self.repository, _number(item.get("number"))),
                observed_at=timestamp_at(self._clock),
            )
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
            raise KeyError(f"issue #{issue_id} not found")  # noqa: TRY003  # lint-waiver: LW-771014 [TRY003]; include the missing remote issue number.
        return issue

    def _write_event(self, issue_id: int, event: IssueEvent) -> None:
        serialized = json.dumps(event.model_dump(), sort_keys=True, separators=(",", ":"))
        self._cli.comment_issue(
            self.repository, issue_id, f"{_EVENT_START}{serialized}{_EVENT_END}"
        )


def _event(  # noqa: PLR0913  # lint-waiver: LW-771015 [PLR0913]; the fields are the independent facts of one event record.
    *,
    actor: str,
    action: str,
    iteration: int,
    note: str,
    payload: dict[str, Any] | None,
    timestamp: str,
    key: str | None = None,
) -> IssueEvent:
    return IssueEvent(
        timestamp=timestamp,
        actor=actor,
        action=action,
        iteration=iteration,
        note=note,
        payload=payload,
        key=key,
    )


def _split_key(body: str) -> tuple[str, str | None]:
    """The issue body without its hidden key marker, and the key the marker carries."""
    start = body.rfind(_KEY_START)
    if start < 0 or not body.endswith(_KEY_END):
        return body, None
    key = body[start + len(_KEY_START) : -len(_KEY_END)]
    return body[:start].rstrip("\n"), key


def _to_issue(raw: dict[str, object], *, observed_at: str) -> Issue:
    """The issue ``raw`` describes; ``observed_at`` stands in for a missing creation time."""
    labels = _label_names(raw.get("labels"))
    type_value = next(
        (label.removeprefix(_TYPE_PREFIX) for label in labels if label.startswith(_TYPE_PREFIX)),
        None,
    )
    if type_value is None:
        raise ValueError(f"GitHub issue #{raw.get('number')} lacks a VibeSys type label")  # noqa: TRY003  # lint-waiver: LW-771016 [TRY003]; identify malformed tracker metadata by issue number.
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
    description, create_key = _split_key(str(raw.get("body") or ""))
    created_at = str(raw.get("createdAt") or observed_at)
    return Issue(
        id=number,
        type=IssueType(type_value),
        title=str(raw.get("title", "")),
        description=description,
        status=status,
        created_by=str((create.payload or {}).get("created_by") if create else _author(raw)),
        created_iter=create.iteration if create and create.iteration is not None else 0,
        created_at=created_at,
        updated_at=str(raw.get("updatedAt") or created_at),
        attempts=_attempt_count(events),
        history=events,
        closed_iter=_closed_iteration(events),
        create_key=create_key,
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
        # Anyone who can comment on the issue writes this text.
        event = decode_json(
            body[len(_EVENT_START) : -len(_EVENT_END)],
            IssueEvent,
            source="issue event comment",
            max_bytes=_MAX_EVENT_BYTES,
        )
        if isinstance(event, Unreadable):
            raise IssueTrackerLoadError.invalid_issue_event()
        events.append(event)
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


def open_issue_tracker(  # noqa: PLR0913  # lint-waiver: LW-261401 [PLR0913]; each keyword is one backend's independent input.
    # > A settings object would only rename the keywords every caller passes, and splitting
    # > the factory per backend would duplicate the selection it exists to own.
    backend: Literal["local", "github"],
    *,
    local_path: Path,
    repository: str | None = None,
    on_change: Callable[[], None] | None = None,
    github: GitHubClient | None = None,
    clock: Clock,
    threads: Threads,
) -> IssueTracker:
    """Construct the selected issue tracker behind the IssueTracker contract.

    ``github`` reaches GitHub; the GitHub backend requires it. ``clock`` (epoch seconds)
    stamps the events the tracker writes, and ``threads`` makes the local board's lock.
    """
    if backend == "local":
        return IssueBoard(local_path, on_change=on_change, clock=clock, threads=threads)
    if repository is None:
        raise ValueError("repository is required for the GitHub issue backend")  # noqa: TRY003  # lint-waiver: LW-771017 [TRY003]; name the missing backend configuration field.
    if github is None:
        message = "a GitHub client is required for the GitHub issue backend"
        raise ValueError(message)
    return GitHubIssueTracker(repository, cli=github, clock=clock)


__all__ = ["GitHubIssueTracker", "open_issue_tracker"]
