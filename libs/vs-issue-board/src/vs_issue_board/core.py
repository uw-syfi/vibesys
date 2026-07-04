"""Reusable JSON-backed issue tracker.

The store is intentionally small and framework-neutral: callers provide a
single JSON file path and can attach an optional ``on_change`` callback for
derived views such as markdown mirrors.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock

from pydantic import BaseModel, Field

_STORE_VERSION = 1


class IssueType(StrEnum):
    BUG = "bug"
    FEATURE = "feature"
    PERF = "perf"


class IssueStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"
    BLOCKED = "blocked"


_TYPE_RANK = {IssueType.BUG: 0, IssueType.FEATURE: 1, IssueType.PERF: 2}


class IssueEvent(BaseModel):
    """A single state-transition or comment record on an issue."""

    timestamp: str
    actor: str
    action: str
    iteration: int | None = None
    note: str = ""
    payload: dict | None = None


class Issue(BaseModel):
    """A single tracker entry persisted as JSON."""

    id: int
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


class IssueBoard:
    """Atomic JSON-backed issue tracker."""

    def __init__(
        self,
        path: Path,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._on_change: Callable[[], None] | None = None
        self._data: dict = {
            "version": _STORE_VERSION,
            "next_id": 1,
            "issues": [],
        }
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("version") == _STORE_VERSION:
                    self._data = loaded
            except (json.JSONDecodeError, ValueError):
                pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._save_locked()
        self._on_change = on_change

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001
                print(
                    "[IssueBoard] on_change callback raised; ignoring:",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)

    def reload(self) -> None:
        """Re-read the JSON file from disk, discarding the in-memory copy."""
        with self._lock:
            if not self.path.is_file():
                return
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                return
            if isinstance(loaded, dict) and loaded.get("version") == _STORE_VERSION:
                self._data = loaded

    def _issue_from_dict(self, raw: dict) -> Issue:
        return Issue.model_validate(raw)

    def _replace_issue(self, issue: Issue) -> None:
        for idx, raw in enumerate(self._data["issues"]):
            if raw.get("id") == issue.id:
                self._data["issues"][idx] = issue.model_dump(mode="json")
                return
        raise KeyError(f"issue #{issue.id} not found")

    def create(
        self,
        *,
        type: IssueType | str,
        title: str,
        description: str,
        created_by: str,
        iteration: int,
    ) -> Issue:
        if not isinstance(type, IssueType):
            type = IssueType(type)
        with self._lock:
            now = datetime.now().isoformat()
            issue = Issue(
                id=self._data["next_id"],
                type=type,
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
            self._data["next_id"] += 1
            self._data["issues"].append(issue.model_dump(mode="json"))
            self._save_locked()
            return issue

    def get(self, issue_id: int) -> Issue | None:
        with self._lock:
            for raw in self._data["issues"]:
                if raw.get("id") == issue_id:
                    return self._issue_from_dict(raw)
        return None

    def update_status(
        self,
        issue_id: int,
        status: IssueStatus | str,
        *,
        actor: str,
        iteration: int,
        note: str = "",
        payload: dict | None = None,
    ) -> Issue:
        if not isinstance(status, IssueStatus):
            status = IssueStatus(status)
        with self._lock:
            issue = self.get(issue_id)
            if issue is None:
                raise KeyError(f"issue #{issue_id} not found")
            now = datetime.now().isoformat()
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
    ) -> list[int]:
        """Reopen every blocked issue, resetting its attempt budget."""
        reopened: list[int] = []
        with self._lock:
            for raw in self._data["issues"]:
                if raw.get("status") != IssueStatus.BLOCKED.value:
                    continue
                issue = self._issue_from_dict(raw)
                now = datetime.now().isoformat()
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
                self._replace_issue(issue)
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
        payload: dict | None = None,
    ) -> Issue:
        with self._lock:
            issue = self.get(issue_id)
            if issue is None:
                raise KeyError(f"issue #{issue_id} not found")
            now = datetime.now().isoformat()
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

    def list(
        self,
        *,
        status: IssueStatus | str | None = None,
        type: IssueType | str | None = None,
    ) -> list[Issue]:
        if status is not None and not isinstance(status, IssueStatus):
            status = IssueStatus(status)
        if type is not None and not isinstance(type, IssueType):
            type = IssueType(type)
        with self._lock:
            out: list[Issue] = []
            for raw in self._data["issues"]:
                issue = self._issue_from_dict(raw)
                if status is not None and issue.status != status:
                    continue
                if type is not None and issue.type != type:
                    continue
                out.append(issue)
        return out

    def search(self, query: str) -> list[Issue]:
        """Substring search across title and description.

        Comma-separated keywords are AND-matched. Matching is case-insensitive.
        """
        if not query or not query.strip():
            return []
        keywords = [kw.strip().lower() for kw in query.split(",") if kw.strip()]
        if not keywords:
            return []
        with self._lock:
            out: list[Issue] = []
            for raw in self._data["issues"]:
                issue = self._issue_from_dict(raw)
                hay = (issue.title + "\n" + issue.description).lower()
                if all(kw in hay for kw in keywords):
                    out.append(issue)
        return out

    def open_count_by_creator_in_iter(self, creator: str, iteration: int) -> int:
        """Count issues created by *creator* during *iteration*, any status."""
        with self._lock:
            return sum(
                1
                for raw in self._data["issues"]
                if raw.get("created_by") == creator and raw.get("created_iter") == iteration
            )

    def next_open(self) -> Issue | None:
        """Return the next open issue by type priority, then creation time."""
        candidates = self.list(status=IssueStatus.OPEN)
        if not candidates:
            return None
        candidates.sort(key=lambda i: (_TYPE_RANK[i.type], i.created_at))
        return candidates[0]
