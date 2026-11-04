"""One performance evaluation: the evaluator measures the tip and files the issues it finds.

The evaluator works in its own checkout of the tip, which is discarded when the attempt settles.
What it measures and files is its reply; the flow writes the issues it names (at most the
configured number) to the board and reports the evaluation for the next one to read.
"""

from vibesys.orchestration.issue_queue.models import IssuePerfEvalResult
from vibesys.orchestration.issue_queue.search import _writes as writes
from vibesys.orchestration.issue_queue.search._prompts import (
    OverCapPrompt,
    PerformancePrompt,
    PriorPerformance,
)
from vibesys.orchestration.issue_queue.search._replies import (
    load_levels_json,
    perf_record,
)
from vibesys.orchestration.issue_queue.search._roles import PERF_EVALUATOR
from vibesys.orchestration.issue_queue.search._spans import evaluation_begun
from vibesys.orchestration.issue_queue.search._tracker import write
from vibesys.orchestration.issue_queue.search._values import (
    Failure,
    FailureKind,
    JobEndedError,
    PerfDone,
    PerfJob,
    failure_of,
)
from vibesys.search_spans.api import turn_label
from vs_core.api.search import (
    Assessment,
    Failed,
    Flow,
    NotOpened,
    Session,
    TurnFinished,
    Workspace,
    flow,
)


@flow(snapshot=PerfJob)
async def perf_job(ctx: Flow, job: PerfJob) -> PerfDone:
    """Run one evaluation to the settlement of its workspace."""
    ctx.checkpoint(job)  # restartable from its start: the issues it files are keyed
    evaluation_begun(ctx, job)
    async with ctx.workspaces.open(
        attempt=job.attempt, item=job.item, plan=job.workspace, budget=job.budget
    ) as opened:
        if isinstance(opened, NotOpened):
            failure = Failure(kind=FailureKind.NOT_OPENED, reason=opened.failed.reason.value)
            return PerfDone(iteration=job.iteration, failure=failure)
        done = await evaluate(ctx, job, opened)
        outcome = "failed" if done.failure is not None else "succeeded"
        ctx.workspaces.keep(opened, None, Assessment(outcome=outcome, retention="discard"))
    return done


async def evaluate(ctx: Flow, job: PerfJob, ws: Workspace) -> PerfDone:
    """Ask the evaluator, file what it found, and report the evaluation."""
    try:
        reply = await assess(ctx, job, ws)
        filed = writes.filing(job, reply.new_issues, reply.model_dump_json())
        applied = await write(ctx, job.config, filed, "filing the evaluation's issues")
        created = tuple(result.issue_id for result in applied.results)
        return PerfDone(iteration=job.iteration, record=perf_record(job.iteration, reply, created))
    except JobEndedError as ended:
        return PerfDone(iteration=job.iteration, failure=ended.failure)


async def assess(ctx: Flow, job: PerfJob, ws: Workspace) -> IssuePerfEvalResult:
    """One evaluator turn: what it measured and the issues it names."""
    session = ctx.agents.session(PERF_EVALUATOR, workspace=ws, revision=job.base)
    prompt = PerformancePrompt(
        iteration=job.iteration,
        max_new_issues=job.config.max_issues_per_perf_eval,
        board=job.board,
        prior=tuple(
            PriorPerformance(
                iteration=record.iteration,
                throughput_trend=record.primary_trend,
                latency_trend=record.secondary_trend,
                metrics_json=record.metrics_json,
                new_issues=len(record.new_issues),
            )
            for record in job.prior
        ),
        load_levels_json=load_levels_json(job.config),
    )
    reply = await ask(session, prompt)
    cap = job.config.max_issues_per_perf_eval
    if len(reply.new_issues) > cap:
        # The reply schema cannot know the configured cap, so the flow holds the evaluator to it
        # the way core holds a reply to its schema: one correction, then the job fails.
        reply = await ask(session, OverCapPrompt(named=len(reply.new_issues), max_new_issues=cap))
    if len(reply.new_issues) > cap:
        failure = Failure(kind=FailureKind.AGENT, role=PERF_EVALUATOR.name, reason="invalid-reply")
        raise JobEndedError(failure)
    return reply


async def ask(session: Session, prompt: PerformancePrompt | OverCapPrompt) -> IssuePerfEvalResult:
    """One evaluator turn, or the failure that ended the job."""
    match await session.turn(prompt, label=turn_label(session)):
        case TurnFinished(reply=IssuePerfEvalResult() as reply):
            return reply
        case Failed() as failed:
            raise JobEndedError(failure_of(failed, PERF_EVALUATOR.name))
        case TurnFinished() as unexpected:
            message = f"the evaluator answered with {type(unexpected.reply).__name__}"
            raise TypeError(message)
