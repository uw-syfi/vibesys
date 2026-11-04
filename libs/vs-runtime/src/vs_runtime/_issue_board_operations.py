"""Owner of the issue-board operations, over the public `IssueTracker` and `ProgressLog`.

A strategy asks to read the board, change it with keyed writes, or log progress; this owner
performs each against the tracker the run selected. Keyed writes are what make a replay safe:
every change carries a key that the tracker stored with the issue or event it wrote, so
`inspect` answers from the tracker's own records whether a batch landed, and `execute`
skips any change whose key is already there. A batch that crashed half way is therefore
finished, not repeated.

Tracker calls are synchronous (the GitHub tracker shells out to `gh`); they run on the event
loop exactly as the legacy loop ran them, one board operation at a time per run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from pydantic import JsonValue, TypeAdapter

from vs_decode.api import Unreadable, decode_json
from vs_issue_tracker.api import (
    AppliedChanges,
    ApplyIssueChanges,
    BoardSnapshot,
    ChangeResult,
    CreateIssue,
    IssueChange,
    IssueRow,
    LoggedProgress,
    LogProgress,
    ReadBoard,
    RecordAttempt,
    SetIssueStatus,
    TemplateBody,
    TextBody,
)
from vs_runtime._operation_catalog import Applied, Inspection, NotApplied

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vs_core.api import OperationRequest
    from vs_issue_tracker.api import Issue, IssueTracker, ProgressLog
    from vs_runtime._core_requests import ExecutionContext

BOARD_KEY = "the change key the tracker stores with the issue or progress entry it wrote"
"""What a keyed board write is keyed by: its owner's ``inspect`` reads it back from the tracker."""
_PROGRESS_MARKER = "<!-- vibesys:progress-key:v1 {key} -->\n"
_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
"""The largest turn reply payload rendered into a progress entry."""


class BoardTexts(Protocol):
    """The text a board write needs that only the strategy's templates can produce."""

    def issue_body(self, template: str) -> str:
        """The description of an issue the strategy filed from a template."""
        ...

    def progress_entry(self, request: LogProgress) -> str:
        """One human-readable progress entry for a turn's recorded reply."""
        ...


class IssueBoardOwner:
    """Perform and inspect the three issue-board operations over one tracker."""

    def __init__(
        self,
        tracker: IssueTracker,
        progress: ProgressLog,
        texts: BoardTexts,
        publish: Callable[[list[Issue]], None] | None = None,
    ) -> None:
        """Bind the run's tracker, progress log and the strategy's text renderer.

        ``publish`` receives every board read, for a human-readable mirror of the board.
        """
        self._tracker = tracker
        self._progress = progress
        self._texts = texts
        self._publish = publish

    async def execute(self, request: OperationRequest, context: ExecutionContext) -> BaseModel:
        """Perform one operation; a tracker failure propagates as an unknown effect."""
        del context
        return self.perform(request)

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        """Say from the tracker's records whether the write landed; a query answers by running."""
        del context
        return self.observe(request)

    def perform(self, request: OperationRequest) -> BaseModel:
        """Perform one operation now: the body of `execute`, for callers that hold no event loop."""
        if isinstance(request, ReadBoard):
            return self._read()
        if isinstance(request, ApplyIssueChanges):
            return self._apply(request)
        return self._log(cast("LogProgress", request))

    def observe(self, request: OperationRequest) -> Inspection:
        """Say whether the operation's effect is recorded now: the body of `inspect`."""
        if isinstance(request, ReadBoard):
            return Applied(self._read())
        if isinstance(request, ApplyIssueChanges):
            found = tuple(self._landed(change) for change in request.changes)
            if all(item is not None for item in found):
                return Applied(
                    AppliedChanges(
                        status="succeeded",
                        results=tuple(
                            ChangeResult(key=change.key, issue_id=issue.id)
                            for change, issue in zip(request.changes, found, strict=True)
                            if issue is not None
                        ),
                    )
                )
            return NotApplied()
        key = cast("LogProgress", request).key
        if _PROGRESS_MARKER.format(key=key) in self._progress.read():
            return Applied(LoggedProgress(status="succeeded"))
        return NotApplied()

    def _read(self) -> BoardSnapshot:
        issues = sorted(self._tracker.list(), key=lambda issue: issue.id)
        if self._publish is not None:
            self._publish(issues)
        return BoardSnapshot(
            status="succeeded",
            issues=tuple(
                IssueRow(
                    id=issue.id,
                    type=issue.type,
                    status=issue.status,
                    title=issue.title,
                    description="" if issue.status.value == "closed" else issue.description,
                    attempts=issue.attempts,
                    created_by=issue.created_by,
                    created_iter=issue.created_iter,
                )
                for issue in issues
            ),
        )

    def _landed(self, change: IssueChange) -> Issue | None:
        """The issue a change already wrote to, or None if its key is not recorded."""
        if isinstance(change, CreateIssue):
            return next(
                (issue for issue in self._tracker.list() if issue.has_key(change.key)), None
            )
        issue = self._tracker.get(change.issue_id)
        return issue if issue is not None and issue.has_key(change.key) else None

    def _apply(self, request: ApplyIssueChanges) -> AppliedChanges:
        results: list[ChangeResult] = []
        for change in request.changes:
            landed = self._landed(change)
            if landed is not None:
                results.append(ChangeResult(key=change.key, issue_id=landed.id))
                continue
            try:
                results.append(self._perform(change))
            except KeyError as error:
                return AppliedChanges(
                    status="rejected",
                    results=tuple(results),
                    detail=f"change {change.key!r}: {error.args[0] if error.args else error}",
                )
        return AppliedChanges(status="succeeded", results=tuple(results))

    def _perform(self, change: IssueChange) -> ChangeResult:
        match change:
            case CreateIssue():
                body = change.body
                description = (
                    body.text
                    if isinstance(body, TextBody)
                    else self._texts.issue_body(cast("TemplateBody", body).template)
                )
                issue = self._tracker.create(
                    type=change.type,
                    title=change.title,
                    description=description,
                    created_by=change.created_by,
                    iteration=change.iteration,
                    key=change.key,
                )
            case RecordAttempt():
                issue = self._tracker.increment_attempts(
                    change.issue_id,
                    actor=change.actor,
                    iteration=change.iteration,
                    note=change.note,
                    payload=_payload(change.payload_json),
                    key=change.key,
                )
            case SetIssueStatus():
                issue = self._tracker.update_status(
                    change.issue_id,
                    change.status,
                    actor=change.actor,
                    iteration=change.iteration,
                    note=change.note,
                    payload=_payload(change.payload_json),
                    key=change.key,
                )
        return ChangeResult(key=change.key, issue_id=issue.id)

    def _log(self, request: LogProgress) -> LoggedProgress:
        marker = _PROGRESS_MARKER.format(key=request.key)
        if marker in self._progress.read():
            return LoggedProgress(status="succeeded")
        entry = self._texts.progress_entry(request)
        self._progress.append(f"{entry.rstrip()}\n{marker}\n")
        return LoggedProgress(status="succeeded")


class ProgressPayloadUnreadableError(ValueError):
    """A recorded turn payload that is too large or not JSON."""


def _payload(payload_json: str) -> dict[str, object] | None:
    if not payload_json:
        return None
    loaded = decode_json(
        payload_json, _JSON_VALUE, source="progress payload", max_bytes=_MAX_PAYLOAD_BYTES
    )
    if isinstance(loaded, Unreadable):
        raise ProgressPayloadUnreadableError(str(loaded))
    return loaded if isinstance(loaded, dict) else {"value": loaded}
