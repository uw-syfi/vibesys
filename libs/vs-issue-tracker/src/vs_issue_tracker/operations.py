"""The issue board as core operations: read it, change it with keyed writes, log progress.

A strategy on the vs-core lifecycle never calls a tracker. It declares what it wants as an
operation and a runtime owner performs it over an `IssueTracker`, so every board change is
one durable request with a stable identity and its outcome returns as one event.

- `ReadBoard` is a query. Its snapshot is a cache for the strategy; the tracker stays the
  source of truth.
- `ApplyIssueChanges` is an idempotent write of an ordered batch. Every change carries a
  key, unique to the fact it records, that the tracker stores with the issue or event it
  writes (`IssueTracker.create(key=...)`). The owner answers "did this batch land?" from
  those keys alone, so a replay after a crash resolves to applied or not applied and a
  duplicate issue cannot be created.
- `LogProgress` appends one keyed entry to the run's progress log.

Request and outcome models are immutable values that need only `vs_core`.
"""

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, Field

from vs_core.api import (
    LifecycleClass,
    OperationDescriptor,
    OperationRegistration,
    OperationRequest,
    SchemaRef,
    Value,
)
from vs_issue_tracker.core import IssueStatus, IssueType

type OutcomeStatus = Literal["succeeded", "rejected", "failed"]

READ_KIND = "issue_board.read"
APPLY_KIND = "issue_board.apply_changes"
LOG_KIND = "issue_board.log_progress"


class IssueRow(Value):
    """One issue as a strategy sees it.

    ``description`` is empty for a closed issue: a strategy never prompts about one, and the
    snapshot would otherwise carry every finished issue's text on every read.
    """

    id: int = Field(ge=1)
    type: IssueType
    status: IssueStatus
    title: str
    description: str = ""
    attempts: int = Field(ge=0)
    created_by: str
    created_iter: int


class BoardSnapshot(Value):
    """The board at one moment, ordered by issue id."""

    status: OutcomeStatus
    issues: tuple[IssueRow, ...] = ()
    detail: str = ""


class TextBody(Value):
    """Issue text given as data, for example a bug an agent described."""

    form: Literal["text"] = "text"
    text: str = Field(min_length=1)


class TemplateBody(Value):
    """Issue text the owner renders from a template with the run's prompt variables."""

    form: Literal["template"] = "template"
    template: str = Field(min_length=1)


type IssueBody = Annotated[TextBody | TemplateBody, Field(discriminator="form")]


class CreateIssue(Value):
    """File a new open issue."""

    change: Literal["create"] = "create"
    key: str = Field(min_length=1)
    type: IssueType
    title: str = Field(min_length=1)
    body: IssueBody
    created_by: str = Field(min_length=1)
    iteration: int


class RecordAttempt(Value):
    """Count one paid attempt on an issue and append its record."""

    change: Literal["attempt"] = "attempt"
    key: str = Field(min_length=1)
    issue_id: int = Field(ge=1)
    actor: str = Field(min_length=1)
    iteration: int
    note: str = ""
    payload_json: str = ""


class SetIssueStatus(Value):
    """Move an issue to a status and append the transition's record."""

    change: Literal["status"] = "status"
    key: str = Field(min_length=1)
    issue_id: int = Field(ge=1)
    status: IssueStatus
    actor: str = Field(min_length=1)
    iteration: int
    note: str = ""
    payload_json: str = ""


type IssueChange = Annotated[
    CreateIssue | RecordAttempt | SetIssueStatus, Field(discriminator="change")
]


class ChangeResult(Value):
    """Where one change landed: the issue it created or touched."""

    key: str
    issue_id: int = Field(ge=1)


class AppliedChanges(Value):
    """The outcome of a batch: one result per change, in order, when it succeeded."""

    status: OutcomeStatus
    results: tuple[ChangeResult, ...] = ()
    detail: str = ""


class LoggedProgress(Value):
    """The outcome of one progress entry."""

    status: OutcomeStatus
    detail: str = ""


class ReadBoard(OperationRequest):
    """Read every issue."""

    kind: Literal["issue_board.read"] = "issue_board.read"
    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = BoardSnapshot


class ApplyIssueChanges(OperationRequest):
    """Apply an ordered batch of keyed changes; replaying it never repeats a landed change."""

    kind: Literal["issue_board.apply_changes"] = "issue_board.apply_changes"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = AppliedChanges
    changes: tuple[IssueChange, ...] = Field(min_length=1)


class LogProgress(OperationRequest):
    """Append one keyed entry to the run's progress log."""

    kind: Literal["issue_board.log_progress"] = "issue_board.log_progress"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = LoggedProgress
    key: str = Field(min_length=1)
    iteration: int
    step: Literal["implement", "review", "performance"]
    issue_id: int | None = Field(default=None, ge=1)
    payload_json: str


def _registration(
    kind: str, request: type[OperationRequest], outcome: type[BaseModel]
) -> OperationRegistration:
    stem = kind.removeprefix("issue_board.").replace("_", "-")
    lifecycle = request.model_fields["lifecycle"].default
    return OperationRegistration(
        descriptor=OperationDescriptor(
            kind=kind,
            request_schema=SchemaRef(name=f"issue-board.{stem}", version=1),
            outcome_schema=SchemaRef(name=f"issue-board.{stem}-outcome", version=1),
            lifecycle=lifecycle,
            inspect=lifecycle is not LifecycleClass.QUERY,
        ),
        request_model=request,
        outcome_model=outcome,
    )


def board_operation_registrations() -> tuple[OperationRegistration, ...]:
    """The codec registrations of the three board operations, in a fixed order."""
    return (
        _registration(READ_KIND, ReadBoard, BoardSnapshot),
        _registration(APPLY_KIND, ApplyIssueChanges, AppliedChanges),
        _registration(LOG_KIND, LogProgress, LoggedProgress),
    )


__all__ = [
    "APPLY_KIND",
    "LOG_KIND",
    "READ_KIND",
    "AppliedChanges",
    "ApplyIssueChanges",
    "BoardSnapshot",
    "ChangeResult",
    "CreateIssue",
    "IssueBody",
    "IssueChange",
    "IssueRow",
    "LogProgress",
    "LoggedProgress",
    "OutcomeStatus",
    "ReadBoard",
    "RecordAttempt",
    "SetIssueStatus",
    "TemplateBody",
    "TextBody",
    "board_operation_registrations",
]
