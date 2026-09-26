"""Reusable JSON-backed issue tracker.

The store is intentionally small and framework-neutral: callers provide a
single JSON file path and can attach an optional ``on_change`` callback for
derived views such as markdown mirrors.
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable
_STORE_VERSION = 1


def _issue_not_found(issue_id: int) -> KeyError:
    """Build the stable lookup error used by issue update paths."""
    return KeyError(f"issue #{issue_id} not found")


class IssueType(StrEnum):
    """Kinds of work items managed by an issue board."""

    BUG = "bug"
    FEATURE = "feature"
    PERF = "perf"


class IssueStatus(StrEnum):
    """Lifecycle states for issue-board items."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"
    BLOCKED = "blocked"


_TYPE_RANK = {IssueType.BUG: 0, IssueType.FEATURE: 1, IssueType.PERF: 2}


class IssueEvent(BaseModel):
    """A single state-transition or comment record on an issue."""

    model_config = ConfigDict(extra="forbid")

    timestamp: str
    actor: str
    action: str
    iteration: int | None = None
    note: str = ""
    payload: dict[str, Any] | None = None


class Issue(BaseModel):
    """A single tracker entry persisted as JSON."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    type: IssueType
    title: str
    description: str
    status: IssueStatus = IssueStatus.OPEN
    created_by: str
    created_iter: int
    created_at: str
    updated_at: str
    attempts: int = 0
    history: list[IssueEvent] = Field(default_factory=list)
    closed_iter: int | None = None


class IssueTracker(Protocol):
    """Storage-neutral contract for the issue-queue workflow.

    Reads observe updates committed by other tracker clients before returning.
    Implementations preserve issue identity, ordering, lifecycle, and event
    history. Storage paths and provider-specific operations stay out of this
    interface.
    """

    def create(
        self,
        *,
        type: IssueType | str,  # noqa: A002  # lint-waiver: LW-920407 [A002]; preserve the established public keyword across tracker backends.
        title: str,
        description: str,
        created_by: str,
        iteration: int,
    ) -> Issue:
        """Create an open issue and its initial create event."""
        ...

    def get(self, issue_id: int) -> Issue | None:
        """Return a detached issue, or ``None`` if the identifier is absent."""
        ...

    def update_status(  # noqa: PLR0913  # lint-waiver: LW-920409 [PLR0913]; retain the tracker transition contract's named state, actor, iteration, and event fields.
        self,
        issue_id: int,
        status: IssueStatus | str,
        *,
        actor: str,
        iteration: int,
        note: str = "",
        payload: dict[str, Any] | None = None,
    ) -> Issue:
        """Change status and append a history event; missing IDs raise ``KeyError``."""
        ...

    def reopen_blocked(
        self,
        *,
        actor: str,
        iteration: int,
        note: str = "",
    ) -> builtins.list[int]:
        """Reopen blocked issues and reset their attempt counts."""
        ...

    def increment_attempts(
        self,
        issue_id: int,
        *,
        actor: str,
        iteration: int,
        note: str = "",
        payload: dict[str, Any] | None = None,
    ) -> Issue:
        """Increment the attempt count and append an attempt event."""
        ...

    def list(
        self,
        *,
        status: IssueStatus | str | None = None,
        type: IssueType | str | None = None,  # noqa: A002  # lint-waiver: LW-920408 [A002]; preserve the established public filter keyword across tracker backends.
    ) -> builtins.list[Issue]:
        """List detached issues, optionally filtered by status and type."""
        ...

    def search(self, query: str) -> builtins.list[Issue]:
        """Case-insensitive substring search with comma-separated AND terms."""
        ...

    def open_count_by_creator_in_iter(self, creator: str, iteration: int) -> int:
        """Count issues created by this actor and iteration, regardless of status."""
        ...

    def next_open(self) -> Issue | None:
        """Return the open issue with the earliest type priority and creation time."""
        ...


class IssueBoardLoadError(ValueError):
    """Raised when persisted issue-board state cannot be loaded safely."""

    @classmethod
    def at_path(cls, path: Path, details: str) -> Self:
        """Describe why the persisted issue-board file could not be loaded."""
        return cls(f"cannot load issue board {path}: {details}")


class _IssueBoardData(BaseModel):
    """Versioned persistence contract for an issue board."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    next_id: int = Field(ge=1)
    issues: list[Issue]

    @model_validator(mode="after")
    def _valid_issue_identity(self) -> Self:
        issue_ids = [issue.id for issue in self.issues]
        if len(issue_ids) != len(set(issue_ids)):
            message = "issue IDs must be unique"
            raise ValueError(message)
        if issue_ids and self.next_id <= max(issue_ids):
            message = "next_id must be greater than every persisted issue ID"
            raise ValueError(message)
        return self


class IssueBoard:
    """Atomic JSON-backed issue tracker."""

    def __init__(
        self,
        path: Path,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """Open the JSON issue board with an optional change callback."""
        self.path = Path(path)
        self._lock = RLock()
        self._on_change: Callable[[], None] | None = None
        self._data = _IssueBoardData(
            version=_STORE_VERSION,
            next_id=1,
            issues=[],
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            self._data = self._load_from_disk()
        else:
            self._save_locked()
        self._on_change = on_change

    def _load_from_disk(self) -> _IssueBoardData:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise IssueBoardLoadError.at_path(self.path, f"invalid JSON: {exc}") from exc
        except OSError as exc:
            raise IssueBoardLoadError.at_path(self.path, f"cannot read store: {exc}") from exc
        if not isinstance(loaded, dict):
            raise IssueBoardLoadError.at_path(self.path, "expected a JSON object")
        if "version" not in loaded:
            raise IssueBoardLoadError.at_path(
                self.path,
                f"unsupported version None; expected {_STORE_VERSION}",
            )
        if loaded["version"] != _STORE_VERSION:
            raise IssueBoardLoadError.at_path(
                self.path,
                f"unsupported version {loaded['version']!r}; expected {_STORE_VERSION}",
            )
        try:
            data = _IssueBoardData.model_validate(loaded)
        except ValidationError as exc:
            raise IssueBoardLoadError.at_path(
                self.path,
                f"invalid store structure: {exc}",
            ) from exc
        return data

    def _refresh_locked(self) -> None:
        """Read the latest persisted snapshot while holding ``_lock``."""
        self._data = self._load_from_disk()

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(self._data.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(self.path)
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001  # lint-waiver: LW-010100 [BLE001]; callbacks are caller code, and persistence must survive any callback failure.
                sys.stderr.write("[IssueBoard] on_change callback raised; ignoring:\n")
                traceback.print_exc(file=sys.stderr)

    def reload(self) -> None:
        """Re-read the JSON file from disk, discarding the in-memory copy."""
        with self._lock:
            self._refresh_locked()

    def _replace_issue(self, issue: Issue) -> None:
        for idx, stored in enumerate(self._data.issues):
            if stored.id == issue.id:
                self._data.issues[idx] = issue.model_copy(deep=True)
                return
        raise _issue_not_found(issue.id)

    def create(
        self,
        *,
        type: IssueType | str,  # noqa: A002  # lint-waiver: LW-010101 [A002]; `type` is the established public create keyword and serialized issue field name.
        title: str,
        description: str,
        created_by: str,
        iteration: int,
    ) -> Issue:
        """Create and persist a new open issue with its initial history event."""
        issue_type = IssueType(type) if not isinstance(type, IssueType) else type
        with self._lock:
            self._refresh_locked()
            now = datetime.now(UTC).isoformat()
            issue = Issue(
                id=self._data.next_id,
                type=issue_type,
                title=title.strip(),
                description=description.strip(),
                status=IssueStatus.OPEN,
                created_by=created_by,
                created_iter=iteration,
                created_at=now,
                updated_at=now,
                attempts=0,
                history=[
                    IssueEvent(
                        timestamp=now,
                        actor=created_by,
                        action="create",
                        iteration=iteration,
                    )
                ],
            )
            self._data.next_id += 1
            self._data.issues.append(issue.model_copy(deep=True))
            self._save_locked()
            return issue

    def get(self, issue_id: int) -> Issue | None:
        """Return a detached copy of an issue, or ``None`` when absent."""
        with self._lock:
            self._refresh_locked()
            for issue in self._data.issues:
                if issue.id == issue_id:
                    return issue.model_copy(deep=True)
        return None

    def update_status(  # noqa: PLR0913  # lint-waiver: LW-010193 [PLR0913]; Preserve IssueBoard.update_status's named-argument contract because callers pass these independent settings directly.
        self,
        issue_id: int,
        status: IssueStatus | str,
        *,
        actor: str,
        iteration: int,
        note: str = "",
        payload: dict[str, Any] | None = None,
    ) -> Issue:
        """Change issue status and append the corresponding history event."""
        if not isinstance(status, IssueStatus):
            status = IssueStatus(status)
        with self._lock:
            self._refresh_locked()
            issue = self.get(issue_id)
            if issue is None:
                raise _issue_not_found(issue_id)
            now = datetime.now(UTC).isoformat()
            old_status = issue.status
            issue.status = status
            issue.updated_at = now
            if status in (IssueStatus.CLOSED, IssueStatus.BLOCKED):
                issue.closed_iter = iteration
            issue.history.append(
                IssueEvent(
                    timestamp=now,
                    actor=actor,
                    action=f"{old_status.value}->{status.value}",
                    iteration=iteration,
                    note=note,
                    payload=payload,
                )
            )
            self._replace_issue(issue)
            self._save_locked()
            return issue

    def reopen_blocked(
        self,
        *,
        actor: str,
        iteration: int,
        note: str = "",
    ) -> builtins.list[int]:
        """Reopen every blocked issue, resetting its attempt budget."""
        reopened: list[int] = []
        with self._lock:
            self._refresh_locked()
            for issue in self._data.issues:
                if issue.status is not IssueStatus.BLOCKED:
                    continue
                now = datetime.now(UTC).isoformat()
                issue.status = IssueStatus.OPEN
                issue.attempts = 0
                issue.closed_iter = None
                issue.updated_at = now
                issue.history.append(
                    IssueEvent(
                        timestamp=now,
                        actor=actor,
                        action="blocked->open",
                        iteration=iteration,
                        note=note,
                    )
                )
                reopened.append(issue.id)
            if reopened:
                self._save_locked()
        return reopened

    def increment_attempts(
        self,
        issue_id: int,
        *,
        actor: str,
        iteration: int,
        note: str = "",
        payload: dict[str, Any] | None = None,
    ) -> Issue:
        """Increment an issue's attempt count and append an attempt event."""
        with self._lock:
            self._refresh_locked()
            issue = self.get(issue_id)
            if issue is None:
                raise _issue_not_found(issue_id)
            now = datetime.now(UTC).isoformat()
            issue.attempts += 1
            issue.updated_at = now
            issue.history.append(
                IssueEvent(
                    timestamp=now,
                    actor=actor,
                    action="attempt",
                    iteration=iteration,
                    note=note,
                    payload=payload,
                )
            )
            self._replace_issue(issue)
            self._save_locked()
            return issue

    # This method shadows the builtin ``list`` inside the class body, which is
    # the scope where method annotations are resolved.  Return annotations that
    # need the builtin sequence type must therefore spell it ``builtins.list``.
    def list(
        self,
        *,
        status: IssueStatus | str | None = None,
        type: IssueType | str | None = None,  # noqa: A002  # lint-waiver: LW-010102 [A002]; preserve the public issue-type filter keyword used by callers.
    ) -> builtins.list[Issue]:
        """Return detached issue copies filtered by optional status and type."""
        if status is not None and not isinstance(status, IssueStatus):
            status = IssueStatus(status)
        issue_type = (
            type if isinstance(type, IssueType) else IssueType(type) if type is not None else None
        )
        with self._lock:
            self._refresh_locked()
            out: list[Issue] = []
            for issue in self._data.issues:
                if status is not None and issue.status != status:
                    continue
                if issue_type is not None and issue.type != issue_type:
                    continue
                out.append(issue.model_copy(deep=True))
        return out

    def search(self, query: str) -> builtins.list[Issue]:
        """Substring search across title and description.

        Comma-separated keywords are AND-matched. Matching is case-insensitive.
        """
        if not query or not query.strip():
            return []
        keywords = [kw.strip().lower() for kw in query.split(",") if kw.strip()]
        if not keywords:
            return []
        with self._lock:
            self._refresh_locked()
            out: list[Issue] = []
            for issue in self._data.issues:
                hay = (issue.title + "\n" + issue.description).lower()
                if all(kw in hay for kw in keywords):
                    out.append(issue.model_copy(deep=True))
        return out

    def open_count_by_creator_in_iter(self, creator: str, iteration: int) -> int:
        """Count issues created by *creator* during *iteration*, any status."""
        with self._lock:
            self._refresh_locked()
            return sum(
                1
                for issue in self._data.issues
                if issue.created_by == creator and issue.created_iter == iteration
            )

    def next_open(self) -> Issue | None:
        """Return the next open issue by type priority, then creation time."""
        candidates = self.list(status=IssueStatus.OPEN)
        if not candidates:
            return None
        candidates.sort(key=lambda i: (_TYPE_RANK[i.type], i.created_at))
        return candidates[0]
