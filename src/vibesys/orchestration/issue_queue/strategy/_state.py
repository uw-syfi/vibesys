"""`IssueQueueStrategyState`: only the policy memory of an issue-queue run.

Lifecycle and accounting facts (attempt phases, slot use, charges, session phases, the
settlements) are never copied here; they are read from `RunView`. What lives here is what
core cannot know: where the run is between issues, the board as last read, the one open job,
the evaluations done and the revision the next job builds on.

The board is a cache of the tracker, which stays the source of truth. A write makes it stale,
and no decision that depends on it is proposed from a stale cache.

Every model is a strict, frozen, tuple-only `Value`, so the state round-trips through the
envelope codec (`validate_immutable_schema`) unchanged.
"""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from vibesys.candidates.api import RunPhase, Winner
from vibesys.orchestration.issue_queue.strategy._prompts import PromptContext
from vs_core.api import (
    AttemptTrack,
    DecisionId,
    InvocationRef,
    RevisionRef,
    SchemaRef,
    SettlementId,
    Step,
    StrategyState,
    TurnChain,
    Value,
)
from vs_issue_tracker.api import IssueRow, IssueType

STATE_SCHEMA = SchemaRef(name="plain.strategy-state", version=1)


class Role(StrEnum):
    """Agent roles; values match the `plain-<role>` role ids."""

    IMPLEMENTER = "implementer"
    JUDGE = "judge"
    PERF = "perf-evaluator"


class JobKind(StrEnum):
    """What the one open job does with its attempt."""

    ISSUE = "issue"
    PERF = "perf"


class Phase(StrEnum):
    """Where the open job is. Core owns the attempt lifecycle; this is the policy's step.

    START opens the attempt. CLAIM marks the issue in progress. WORK is the implementer's
    (or the evaluator's) turn. RECORD writes the attempt to the board. REVIEW is the judge's
    turn. FILE writes the verdict, or the evaluator's issues, to the board. BLOCK marks an
    issue whose attempts are spent. SETTLE closes the attempt. DONE is a settled job.
    """

    START = "start"
    CLAIM = "claim"
    WORK = "work"
    RECORD = "record"
    REVIEW = "review"
    FILE = "file"
    BLOCK = "block"
    SETTLE = "settle"
    DONE = "done"


class Cursor(StrEnum):
    """Where the run is between jobs."""

    START = "start"
    DRAIN = "drain"
    ASSESS = "assess"
    AFTER_PERF = "after_perf"


class Pending(Value):
    """One board request the run itself sent: waiting, and how often it was sent again."""

    step: Step = Step.NEEDED
    awaiting: DecisionId | None = None
    retries: int = Field(default=0, ge=0)


class BoardState(Value):
    """The board as last read, and whether a write has made that reading stale."""

    issues: tuple[IssueRow, ...] = ()
    fresh: bool = False
    read: Pending = Pending()
    # How many reads were sent, so each read has its own decision identity.
    reads: int = Field(default=0, ge=0)
    bootstrap_done: bool = False
    bootstrap: Pending = Pending()


class FiledIssue(Value):
    """An issue an agent asked to file, as the reply named it."""

    type: IssueType
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)


class TurnRecord(TurnChain[PromptContext]):
    """One role's turn chain: free, paid, corrections and resumes."""

    role: Role


class Job(AttemptTrack[TurnRecord]):
    """The one open job: working an issue, or one performance evaluation."""

    kind: JobKind
    # One more than the jobs opened before it, so each job's identities are its own.
    sequence: int = Field(gt=0)
    phase: Phase = Phase.START
    iteration: int = Field(gt=0)
    issue: IssueRow | None = None
    # The revision the job builds on: the tip when it opened.
    base: RevisionRef
    # Implementer turns charged so far, bounded by `max_attempts_per_issue`.
    turns_spent: int = Field(default=0, ge=0)
    # The serial the next first turn of a role takes: one past every serial used in this job.
    next_serial: int = Field(default=0, ge=0)
    # How often the job's current board write was sent again after its effect stayed unknown.
    write_retries: int = Field(default=0, ge=0)
    # The revision the implementer's turn retained; None when the turn changed nothing.
    candidate: RevisionRef | None = None
    # The reply last accepted from the role that just answered, as JSON.
    reply_json: str = ""
    # The implementer's account of its turn, shown to the judge.
    summary: str = ""
    files_touched: tuple[str, ...] = ()
    self_check: str = ""
    # The evaluator's trends and metrics, kept to record the evaluation once it settles.
    throughput_trend: str = ""
    latency_trend: str = ""
    metrics_json: str = ""
    review_passed: bool | None = None
    judge_invocation: InvocationRef | None = None
    feedback: str | None = None
    analysis: str | None = None
    filed: tuple[FiledIssue, ...] = ()
    closed: bool = False
    # The ids of the issues the job's write created, in the order filed.
    created: tuple[int, ...] = ()


class PerformanceRecord(Value):
    """A finished performance evaluation, shown to the next one."""

    iteration: int = Field(gt=0)
    throughput_trend: str
    latency_trend: str
    metrics_json: str
    new_issues: tuple[int, ...] = ()


class Tip(Value):
    """The revision the next job builds on: the last closed issue's candidate, else the input."""

    revision: RevisionRef | None = None
    settlement: SettlementId | None = None


class Ending(Value):
    """How the run ends once nothing is in flight."""

    outcome: Literal["success", "failure"]
    reason: str = Field(min_length=1)


class Verification(Value):
    """The trusted accuracy measurement of the final candidate that adoption requires."""

    step: Step = Step.NEEDED
    awaiting: DecisionId | None = None
    done: bool = False


class IssueQueueStrategyState(StrategyState):
    """Persisted policy state of the issue-queue strategy."""

    schema_version: int = Field(default=STATE_SCHEMA.version, ge=1)
    phase: RunPhase = RunPhase.SEARCHING
    stopping: bool = False
    cursor: Cursor = Cursor.START
    round_idx: int = Field(default=0, ge=0)
    # Issues worked so far in this round.
    processed: int = Field(default=0, ge=0)
    board: BoardState = BoardState()
    job: Job | None = None
    jobs_started: int = Field(default=0, ge=0)
    performance: tuple[PerformanceRecord, ...] = ()
    tip: Tip = Tip()
    ending: Ending | None = None
    verification: Verification = Verification()
    winner: Winner | None = None
    # Why the work of the closed issues could not be adopted, when it could not.
    adoption_error: str = ""
    notes: tuple[str, ...] = ()
