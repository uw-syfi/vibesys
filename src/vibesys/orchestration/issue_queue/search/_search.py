"""The issue-queue search as a story: file the task, work the board round by round, adopt the tip.

This is the whole search, top to bottom. The board is the source of truth: the rounds read it
before every decision, and `Queue` (a pure value) says what the board calls for next: work an
issue, evaluate performance, or end. The rounds are a flow of their own (`work_queue`) whose
snapshot is the queue, so a restart on changed code keeps the rounds spent and the performance
history. Each job is a task in a group of one, so it is restartable from its start, and the
operator's stop reaches it (it settles what it started) while the rounds only see the stop, start
nothing more and return. The closed issues build on one another, so the winner is the last of
them: it is named (`ctx.choose`) as soon as core would adopt it as it stands, so a stop or a
deadline adopts it, and after the accuracy proof its adoption needs otherwise.
"""

from typing import TYPE_CHECKING

from vibesys.orchestration.issue_queue.search._board import bootstrap_title, briefs
from vibesys.orchestration.issue_queue.search._config import IssueQueueConfig
from vibesys.orchestration.issue_queue.search._issue import issue_job
from vibesys.orchestration.issue_queue.search._operations import BOARD, OPERATIONS
from vibesys.orchestration.issue_queue.search._perf import perf_job
from vibesys.orchestration.issue_queue.search._proof import prove
from vibesys.orchestration.issue_queue.search._queue import (
    Assess,
    Finish,
    Queue,
    QueueRun,
    QueueSnapshot,
    Work,
)
from vibesys.orchestration.issue_queue.search._roles import ROLES
from vibesys.orchestration.issue_queue.search._spans import builds_on
from vibesys.orchestration.issue_queue.search._tracker import apply, read_board
from vibesys.orchestration.issue_queue.search._values import Ending, Failure, IssueJob, PerfJob
from vibesys.orchestration.issue_queue.search._writes import bootstrap
from vibesys.search_spans.api import SPANS, adoption_label
from vs_core.api.search import (
    INPUT,
    Flow,
    Input,
    Kept,
    NoWinner,
    TaskStopped,
    flow,
    search,
)

if TYPE_CHECKING:
    from vs_issue_tracker.api import IssueRow

BLANK_OBJECTIVE = (
    "the run's objective is blank, so there is no first issue to file: give the task an objective"
)


@search(
    "plain",
    config=IssueQueueConfig,
    operations=OPERATIONS,
    roles=ROLES,
    owned=BOARD,
    spans=SPANS,
)
async def issue_queue(ctx: Flow, config: IssueQueueConfig) -> NoWinner | None:
    """File the task as the first issue, work the board in rounds, and name the last closed issue."""
    ctx.checkpoint(config)
    ctx.fall_back_to_input(adoption_label(INPUT, None, None))
    title = bootstrap_title(ctx.task.objective)
    if not title:
        return NoWinner(reason=BLANK_OBJECTIVE)
    queue = Queue()
    filed = await apply(
        ctx,
        config,
        bootstrap(ctx.task.run_id, title, queue.iteration),
        "filing the bootstrap issue",
    )
    if isinstance(filed, Failure):
        queue = queue.ended(
            Ending.failure(f"the bootstrap issue could not be filed: {filed.describe()}")
        )
    # The rounds are a flow of their own so that what they know (the rounds spent, the
    # performance history) is a snapshot: a restart on changed code resumes from it. A restart
    # of the root files the bootstrap issue again (keyed, so it is the same write) and resumes
    # the rounds from their snapshot, whatever queue is passed here.
    async with ctx.group(stops=False) as rounds:
        rounds.start("queue", work_queue, QueueRun(config=config, queue=queue))
        done = await rounds
    queue = queue.stopped() if isinstance(done, TaskStopped) else done
    return await conclude(ctx, queue)


@flow(snapshot=QueueRun)
async def work_queue(ctx: Flow, run: QueueRun) -> Queue:
    """Work the board round by round until the queue ends, and report what the run came to."""
    ctx.checkpoint(run)  # restartable from its start: the board says what is left to do
    config, queue = run.config, run.queue
    issues: tuple[IssueRow, ...] = ()
    async with ctx.group(limit=1) as jobs:
        while queue.active:
            ctx.checkpoint(QueueRun(config=config, queue=queue))
            if ctx.stop_requested:
                queue = queue.stopped()
                break
            read = await read_board(ctx, config)
            if isinstance(read, Failure):
                queue = queue.ended(
                    Ending.failure(f"the issue tracker is unavailable: {read.describe()}")
                )
                break
            issues = read
            step = queue.decide(issues, config)
            ctx.publish(QueueSnapshot.of(queue, issues, step))
            base = queue.base(ctx.facts.baseline)
            common = {
                "sequence": queue.next_sequence,
                "iteration": queue.iteration,
                "run_id": ctx.task.run_id,
                "base": base,
                "board": briefs(issues),
                "config": config,
            }
            match step:
                case Finish(ending=ending):
                    queue = queue.ended(ending)
                    continue
                case Work(issue=issue):
                    start = IssueJob(**common, issue=issue)
                    span = jobs.start(start.key, issue_job, start).span
                    builds_on(ctx, span, queue.tip_span)
                case Assess():
                    evaluation = PerfJob(**common, prior=queue.performance)
                    span = jobs.start(evaluation.key, perf_job, evaluation).span
            queue = queue.started().folded(await jobs, span)
            name_tip(ctx, queue)
    ctx.publish(QueueSnapshot.of(queue, issues, None))
    return queue


def name_tip(ctx: Flow, queue: Queue) -> None:
    """Name the tip as the winner as soon as core would adopt it as it stands.

    A stop or a deadline then adopts the last closed issue's work. A task that needs an accuracy
    proof adopts only a proven revision, so there the tip is named after `prove`, in `conclude`.
    """
    if queue.tip is not None and not ctx.task.terms.requires_accuracy_proof:
        choose(ctx, queue.tip)


def choose(ctx: Flow, winner: Kept | Input) -> None:
    """Name ``winner``, with the label of the span that adopts it.

    Nothing measures the tip but the accuracy proof, whose reading core decides on, so the
    label says which candidate is adopted and nothing of its value.
    """
    ctx.choose(winner, adoption_label(winner, None, None))


async def conclude(ctx: Flow, queue: Queue) -> NoWinner | None:
    """Name the winner and say whether the run found one.

    The winner is the last closed issue's candidate, or the input when no issue closed with a
    change. A run that built something and then stopped short of its goal still adopts what it
    built, and the run still fails with the reason it stopped short. An operator's stop leaves
    the choice as the run made it, and the framework ends the run as cancelled.
    """
    if queue.stopping:
        return None
    ending = queue.ending
    tip = queue.tip
    if tip is None:
        choose(ctx, INPUT)
        if ending is not None and ending.outcome == "failure":
            return NoWinner(reason=ending.reason)
        return None
    await prove(ctx, tip)
    choose(ctx, tip)
    if ending is not None and ending.outcome == "failure":
        return NoWinner(reason=ending.reason, keep_choice=True)
    return None
