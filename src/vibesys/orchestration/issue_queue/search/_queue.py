"""The run's own record: where it is between jobs, what it has built and what it decides next.

`Queue` is a pure value. The root flow reads the board, asks the queue what to do (`decide`),
starts the job it names and folds the finished job back in (`after_issue`, `after_perf`). The
stop decision of a run (a blocked queue, a quiet board, an exhausted round budget) is a function
of the board the root read, so it is the same on every replay.
"""

from typing import Literal

from pydantic import Field

from vibesys.orchestration.issue_queue.search import _board as board
from vibesys.orchestration.issue_queue.search._config import IssueQueueConfig
from vibesys.orchestration.issue_queue.search._prompts import IssueBrief
from vibesys.orchestration.issue_queue.search._values import (
    Ending,
    JobDone,
    PerfDone,
    PerfRecord,
)
from vs_core.api.search import Kept, RevisionRef, SpanId, TaskStopped, Value
from vs_issue_tracker.api import IssueRow

DRAINED = "the board is drained and the last evaluation filed nothing"


class Work(Value):
    """Work this issue next."""

    issue: IssueRow


class Assess(Value):
    """Evaluate performance next."""


class Finish(Value):
    """The run is over."""

    ending: Ending


type Step = Work | Assess | Finish


class Queue(Value):
    """What the run knows between jobs."""

    round_idx: int = Field(default=0, ge=0)
    processed: int = Field(default=0, ge=0)
    """Issues worked so far in this round."""
    jobs_started: int = Field(default=0, ge=0)
    assessed: bool = False
    """A performance evaluation just closed the previous round."""
    filed: bool = False
    """That evaluation filed issues."""
    tip: Kept | None = None
    """The last closed issue's retained candidate: what the next job builds on."""
    tip_span: SpanId | None = None
    """The workstream of the job that kept the tip."""
    performance: tuple[PerfRecord, ...] = ()
    ending: Ending | None = None
    stopping: bool = False

    @property
    def next_sequence(self) -> int:
        """The sequence number of the job started next."""
        return self.jobs_started + 1

    @property
    def iteration(self) -> int:
        """The 1-based round the next job belongs to."""
        return self.round_idx + 1

    @property
    def active(self) -> bool:
        """Whether the run goes on: no ending reached and no stop requested."""
        return self.ending is None and not self.stopping

    def decide(self, issues: tuple[IssueRow, ...], config: IssueQueueConfig) -> Step:
        """What to do next, given the board as it was just read."""
        if self.assessed and not self.filed and not board.has_open(issues):
            return Finish(ending=Ending.success(DRAINED))
        if self.round_idx >= config.max_rounds:
            reason = f"the round budget of {config.max_rounds} rounds is spent"
            return Finish(ending=Ending.failure(reason))
        pick = None if self.processed >= config.issues_per_round else board.next_open(issues)
        if pick is not None:
            return Work(issue=pick)
        if board.blocked_queue(issues):
            reason = f"all {len(board.remaining(issues))} remaining issues are blocked"
            return Finish(ending=Ending.failure(reason))
        return Assess()

    def base(self, baseline: RevisionRef) -> RevisionRef:
        """The revision the next job builds on: the last closed issue's candidate, else the input."""
        return baseline if self.tip is None else self.tip.revision

    def started(self) -> "Queue":
        """A job was started."""
        return self.model_copy(update={"jobs_started": self.jobs_started + 1})

    def stopped(self) -> "Queue":
        """The operator stopped the run: no job is started after this."""
        return self.model_copy(update={"stopping": True})

    def ended(self, ending: Ending) -> "Queue":
        """The run reached its ending."""
        return self.model_copy(update={"ending": ending})

    def folded(self, done: JobDone | PerfDone | TaskStopped, span: SpanId | None = None) -> "Queue":
        """Fold whatever the job that just ended left: a result, or the operator's stop.

        ``span`` is the job's own workstream, which the next job links to when it kept the tip.
        """
        match done:
            case JobDone():
                return self.after_issue(done, span)
            case PerfDone():
                return self.after_perf(done)
            case TaskStopped():
                return self.stopped()

    def after_issue(self, done: JobDone, span: SpanId | None = None) -> "Queue":
        """Fold a finished issue job.

        A finished issue counts toward the round's cap. One that closed with a change moves the
        tip. A job that failed ends the run: its issue is left as the board last recorded it,
        and nothing is picked up again that could fail the same way.
        """
        failure = done.failure
        ending = (
            None
            if failure is None
            else Ending.failure(
                f"issue #{done.issue_id} could not be completed: {failure.describe()}"
            )
        )
        return self.model_copy(
            update={
                "processed": self.processed + 1,
                "assessed": False,
                "tip": done.kept or self.tip,
                "tip_span": span if done.kept is not None else self.tip_span,
                "ending": ending or self.ending,
            }
        )

    def after_perf(self, done: PerfDone) -> "Queue":
        """Fold a finished evaluation: it closes the round, whether it completed or not."""
        failure = done.failure
        ending = (
            None
            if failure is None
            else Ending.failure(
                f"performance evaluation {done.iteration} could not be completed: "
                f"{failure.describe()}"
            )
        )
        record = done.record
        return self.model_copy(
            update={
                "round_idx": self.round_idx + 1,
                "processed": 0,
                "assessed": True,
                "filed": record is not None and bool(record.new_issues),
                "performance": (*self.performance, *(() if record is None else (record,))),
                "ending": ending or self.ending,
            }
        )


class QueueRun(Value):
    """What the rounds restart from: the config and what the run knew between two jobs."""

    config: IssueQueueConfig
    queue: Queue


class QueueSnapshot(Value):
    """What the run publishes about itself: the queue in the shape frontends read."""

    round_idx: int = Field(ge=0)
    cursor: Literal["drain", "assess", "finished"]
    current_issue_id: int | None = None
    issues: tuple[IssueBrief, ...] = ()
    performance: tuple[PerfRecord, ...] = ()
    ending: Ending | None = None
    stopped: bool = False

    @classmethod
    def of(
        cls,
        queue: Queue,
        issues: tuple[IssueRow, ...],
        step: Step | None,
    ) -> "QueueSnapshot":
        """The snapshot of ``queue`` over the board it just read, and the step it decided on."""
        match step:
            case Work(issue=issue):
                cursor: Literal["drain", "assess", "finished"] = "drain"
                current = issue.id
            case Assess():
                cursor, current = "assess", None
            case _:
                cursor, current = "finished", None
        return cls(
            round_idx=queue.round_idx,
            cursor=cursor,
            current_issue_id=current,
            issues=board.briefs(issues),
            performance=queue.performance,
            ending=queue.ending or (step.ending if isinstance(step, Finish) else None),
            stopped=queue.stopping,
        )
