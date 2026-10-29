"""The work of a child in its open workspace: mutate, review, measure, profile, export.

Each step takes the closing so far and returns the next one. A step that ends the child returns
the closing ended by its typed cause; the steps after it are skipped. `passed` is true from a
passing review until something fails it, so a closing that comes out of `work` with `passed`
true passed everything the run configured.
"""

from vibesys.orchestration.evolve.models import JudgeResponse, MutatorResponse
from vibesys.orchestration.evolve.search._measure import gate
from vibesys.orchestration.evolve.search._prompts import JudgePrompt, ObjectiveAxis, ProfilerPrompt
from vibesys.orchestration.evolve.search._roles import JUDGE, MUTATOR, PROFILER
from vibesys.orchestration.evolve.search._spans import mutated
from vibesys.orchestration.evolve.search._values import (
    ChildStart,
    Closing,
    Failure,
    FailureKind,
    ProfileReading,
    Verdict,
)
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.review import Verdict as ReviewVerdict
from vibesys.search_spans.api import turn_label
from vs_core.api.search import (
    Declined,
    Failed,
    Flow,
    PatchExport,
    Retained,
    Session,
    TurnFinished,
    Workspace,
)


async def work(ctx: Flow, start: ChildStart, ws: Workspace) -> Closing:
    """Let the agents and the gates decide what the child is: a failed or a passing individual."""
    mutator = ctx.agents.session(MUTATOR, workspace=ws, revision=start.base)
    closing = await mutate(start, mutator)
    mutated(ctx, start, closing)
    if closing.cause is not None:
        return closing
    # the judge and the profiler look at what the mutator made: its work started them
    made_by = (mutator.agent,)
    closing = await review(ctx, ws, closing, made_by)
    if not closing.passed:
        return closing
    if ctx.task.terms.measured:
        closing = await gate(ctx, ws, closing)
        if not closing.passed:
            return closing
    if start.config.profiling:
        closing = await profile(ctx, ws, closing, made_by)
    if start.config.needs_code:
        closing = await export(ctx, closing)
    return closing


def no_reply(role: str, failed: Failed) -> Failure:
    """An agent's turn that ended without a valid reply."""
    return Failure(
        kind=FailureKind.NO_REPLY,
        role=role,
        ended=failed.reason,
        turn_failure=failed.turn_failure,
    )


async def mutate(start: ChildStart, mutator: Session) -> Closing:
    """Have the mutator edit the workspace into an offspring, which core checkpoints.

    A mutation is never asked again: a reply that stays invalid, or a turn that is lost for
    good, is a failed individual.
    """
    match await mutator.turn(start.prompt, label=turn_label(mutator)):
        case TurnFinished(reply=MutatorResponse() as reply, checkpoint=Declined() as declined):
            declining = Failure(kind=FailureKind.CHECKPOINT_DECLINED, declined=declined.reason)
            return Closing(summary=reply.summary).ended_by(declining)
        case TurnFinished(reply=MutatorResponse() as reply, checkpoint=Retained(revision=revision)):
            return Closing(revision=revision, summary=reply.summary)
        case Failed() as failed:
            return Closing().ended_by(no_reply(MUTATOR.name, failed))
        case unexpected:
            message = f"the mutator answered with {type(unexpected).__name__}"
            raise TypeError(message)


async def review(ctx: Flow, ws: Workspace, closing: Closing, made_by: tuple[str, ...]) -> Closing:
    """Have the judge grade the offspring, showing it what the mutator's own evaluations said."""
    revision = closing.revision
    if revision is None:
        message = "only a checkpointed offspring is reviewed"
        raise ValueError(message)
    judge = ctx.agents.session(JUDGE, workspace=ws, started_by=made_by)
    prompt = JudgePrompt(candidate=revision, cited_evidence=ctx.evaluation.taken(ws, revision))
    match await judge.turn(prompt, revision=revision, label=turn_label(judge)):
        case TurnFinished(reply=JudgeResponse() as reply, invocation=invocation):
            passed = reply.verdict is ReviewVerdict.PASS
            return closing.model_copy(
                update={
                    "verdict": Verdict(passed=passed, invocation=invocation),
                    "passed": passed,
                    "feedback": reply.feedback if passed else reply.feedback or "review failed",
                }
            )
        case Failed() as failed:
            return closing.ended_by(no_reply(JUDGE.name, failed))
        case unexpected:
            message = f"the judge answered with {type(unexpected).__name__}"
            raise TypeError(message)


async def profile(ctx: Flow, ws: Workspace, closing: Closing, made_by: tuple[str, ...]) -> Closing:
    """Ask the advisory profiler about a passing offspring; a failed profile is only a note."""
    revision = closing.revision
    if revision is None:
        message = "only a checkpointed offspring is profiled"
        raise ValueError(message)
    axes = tuple(
        ObjectiveAxis(name=item.name, direction=item.direction)
        for item in ctx.task.terms.objectives
    )
    profiler = ctx.agents.session(PROFILER, workspace=ws, revision=revision, started_by=made_by)
    match await profiler.turn(
        ProfilerPrompt(candidate=revision, objectives=axes), label=turn_label(profiler)
    ):
        case TurnFinished(reply=ProfilerSummary() as summary):
            reading = ProfileReading(
                perf_metric=summary.perf_metric,
                perf_unit=summary.perf_unit,
                metrics=tuple(summary.metrics.items()),
            )
            return closing.model_copy(update={"profile": reading})
        case Failed() as failed:
            note = f"profiler: {failed.reason.value}"
            return closing.model_copy(update={"notes": (*closing.notes, note)})
        case unexpected:
            message = f"the profiler answered with {type(unexpected).__name__}"
            raise TypeError(message)


async def export(ctx: Flow, closing: Closing) -> Closing:
    """Export the passing offspring's patch, for a selector that ranks candidates by their code."""
    revision = closing.revision
    if revision is None:
        message = "only a checkpointed offspring is exported"
        raise ValueError(message)
    exported = await ctx.workspaces.export_patch(revision, within=ctx.task.terms.operation_seconds)
    if isinstance(exported, Failed):
        refused = Failure(kind=FailureKind.PATCH_EXPORT_FAILED, status=exported.reason.value)
        return closing.ended_by(refused)
    if not isinstance(exported, PatchExport) or exported.status != "succeeded":
        status = "unknown" if not isinstance(exported, PatchExport) else exported.status
        detail = "" if not isinstance(exported, PatchExport) else exported.detail
        return closing.ended_by(
            Failure(kind=FailureKind.PATCH_EXPORT_FAILED, status=status, detail=detail)
        )
    return closing.model_copy(update={"patch": exported.patch})
