"""The plain loop's issue board (persistent issue tracker).

:class:`IssueBoard` wraps a single ``logs/issues.json`` file and is the
source of truth for which issues exist; the loop's ``PlainLoopState``
only tracks the cursor (current iteration / current issue id).

The "issue board" terminology mirrors the agent loop's planning artifact
(``roadmap.md`` + ``progress.md``): each outer loop's planning surface
is referred to as its "issue board" in the codebase, even though the
underlying representation differs.

Atomic writes use a tmp+rename pattern.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Callable

from pydantic import BaseModel, Field

_STORE_VERSION = 1


class IssueType(str, Enum):
    BUG = "bug"
    FEATURE = "feature"
    PERF = "perf"


class IssueStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"
    BLOCKED = "blocked"


# Lower rank = higher drain priority. Bug fixes run before features run
# before perf optimizations, ties broken by created_at ASC in next_open().
_TYPE_RANK = {IssueType.BUG: 0, IssueType.FEATURE: 1, IssueType.PERF: 2}


class IssueEvent(BaseModel):
    """A single state-transition or comment record on an issue.

    ``payload`` carries the full structured agent response (implementer or
    judge) when an event records an agent action. It is optional and defaults
    to ``None`` for events that have no rich content (creation, claim,
    blocked-out). The store treats it as an opaque dict; the renderer in
    ``vibeserve_agent/issue/render.py`` interprets the schema.
    """

    timestamp: str
    actor: str
    action: str
    iteration: int | None = None
    note: str = ""
    payload: dict | None = None


class Issue(BaseModel):
    """A single tracker entry. Persisted as JSON inside ``issues.json``.

    ``attempts`` counts the number of times an implementer has run on this
    issue, regardless of the resulting verdict — both passing and failing
    implementer runs increment it. It is the loop's "max_attempts_per_issue"
    budget, not a "failed retries" counter; an issue that passes on the
    first try still ends with ``attempts == 1``.
    """

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
    """Atomic JSON-backed issue tracker.

    The store is the source of truth for the issue-loop. The N-issues-per
    iteration cap is *derived from the store* (via
    ``open_count_by_creator_in_iter``) rather than from a separate counter,
    so resume-after-crash is trivial.
    """

    def __init__(
        self,
        path: Path,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """Construct the store and load any existing JSON file.

        ``on_change`` is invoked from inside ``_save_locked()`` after every
        successful write. It is intended for cheap side-effects (e.g.
        re-rendering a human-readable mirror); the loop registers the
        per-issue markdown renderer here. The bootstrap save in this
        constructor does NOT fire the callback — callers should trigger
        an explicit initial render after attaching the callback if needed.
        """
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
                pass  # corrupt or wrong-version → keep defaults
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._save_locked()
        # Attach the callback AFTER the bootstrap save so the constructor
        # never fires it. The first render will happen on the first real
        # mutation (or via an explicit caller-driven render).
        self._on_change = on_change

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        # Best-effort notify. The store's job is to keep issues.json
        # consistent; the on_change hook is purely a downstream view.
        # A renderer crash must never prevent a successful save from
        # being observed by the rest of the system.
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

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

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
        """Reopen every BLOCKED issue, resetting its attempt budget.

        Used by the loop's resume path so that a stuck run (where every
        remaining issue exhausted ``max_attempts_per_issue``) gets a fresh
        chance on the next invocation. Each reopened issue:

        - flips status BLOCKED -> OPEN,
        - has ``attempts`` reset to 0 (the budget is the per-resume cap,
          not a lifetime counter — full history is preserved in
          ``history``),
        - has ``closed_iter`` cleared,
        - gains a single ``blocked->open`` event in history.

        Returns the list of reopened issue IDs (in store order). A no-op
        when nothing is blocked — the on_change callback does NOT fire
        in that case.
        """
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

    # ------------------------------------------------------------------
    # listing / search / queries
    # ------------------------------------------------------------------

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
        """Substring search across title+description.

        Comma-separated keywords are AND-matched (each keyword must hit).
        Matching is case-insensitive.
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
        """Count issues created by *creator* during *iteration* (any status).

        Used by the create_issue tool to enforce the per-iteration cap.
        We count by creation iteration, not current status, so an agent
        cannot evade the cap by closing an issue mid-call.
        """
        with self._lock:
            return sum(
                1
                for raw in self._data["issues"]
                if raw.get("created_by") == creator and raw.get("created_iter") == iteration
            )

    def next_open(self) -> Issue | None:
        """Return the next open issue to drain.

        Order: type rank (bug > feature > perf), then created_at ASC.
        Returns ``None`` when no OPEN issues remain. IN_PROGRESS issues
        are NOT picked here — the orchestrator is expected to flip an
        issue to IN_PROGRESS itself when it claims it.
        """
        candidates = self.list(status=IssueStatus.OPEN)
        if not candidates:
            return None
        candidates.sort(key=lambda i: (_TYPE_RANK[i.type], i.created_at))
        return candidates[0]
