"""One round of the multi search: design a hypothesis, implement it, review it, measure it, close it.

A round is a task of the root: it takes where the search stands and ends with what it left behind.
A hypothesis the ledger keeps active is continued without the designer. A new one is designed
first (the attribution command in the profile-guided preset, the router and the profiler when
the run offers one, then the designer's plan). The implementer then works in an isolated
candidate workspace; the judge reads the exact candidate it retained when the cadence asks for a
review; the implementer's validation recipes run when it wrote any and the run offers them; and,
when the cadence says so, the trusted gates measure what it kept. The workspace is a resource, so
it always settles.

The router and the profiler are advisory: a turn of either that ends without an answer costs the
design that advice and nothing else. The designer and the attribution are the run's contract, so
a failure of either ends the search.
"""

from typing import Self

from vibesys.candidates.api import space_of, workspace_mode_for
from vibesys.hypothesis import Continue, Finished, NewHypothesis, rounds
from vibesys.orchestration.multi.models import ImplementerResponse, JudgeResponse, PreRoundDecision
from vibesys.orchestration.multi.search._attempt import MultiAttempt, Next
from vibesys.orchestration.multi.search._config import MultiConfig
from vibesys.orchestration.multi.search._operations import VALIDATE_LOCAL_KIND
from vibesys.orchestration.multi.search._prompts import PreRoundPrompt, ProfilerPrompt
from vibesys.orchestration.multi.search._roles import IMPLEMENTER, JUDGE, PROFILER, ROUTER
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.search_spans.api import turn_label
from vibesys.serial_search import api as serial
from vibesys.serial_search.api import LastProfile, RoundDone, RoundStart
from vs_core.api.search import (
    Declined,
    Failed,
    FailureReason,
    Flow,
    Session,
    Settlement,
    Stopped,
    TurnFinished,
    Workspace,
    flow,
)

DEFAULT_PROFILE_FOCUS = "general steady-state benchmark hotspots"
"""What the profiler looks at when the router wants a profile and names no focus."""


class MultiRound(RoundStart):
    """A round told where the search stands, with the config the search runs under."""

    config: MultiConfig

    @classmethod
    def of(cls, begin: RoundStart, config: MultiConfig) -> Self:
        """``begin`` with the config the search runs under."""
        fields = {name: getattr(begin, name) for name in RoundStart.model_fields}
        return cls(**fields, config=config)


@flow(snapshot=MultiRound)
async def play_round(ctx: Flow, start: MultiRound) -> RoundDone:
    """Play one round to the settlement of its workspace."""
    ctx.checkpoint(start)  # restartable from its start: nothing else is needed to redo it
    try:
        return await _play(ctx, start)
    except Stopped:
        return RoundDone(number=start.number, ledger=start.ledger, counted=False, stopped=True)


async def _play(ctx: Flow, start: MultiRound) -> RoundDone:
    config = start.config
    space = space_of(ctx.task.terms)
    ledger = start.ledger.decode(space)
    match rounds.next_round(config.hypotheses, ledger, start.number):
        case Finished():
            return RoundDone(number=start.number, ledger=start.ledger, counted=False)
        case Continue(hypothesis=hypothesis):
            plan, feedback = hypothesis.plan, hypothesis.feedback
            base = serial.continued_base(
                start, hypothesis.parent_commit, input_revision=ctx.facts.baseline
            )
            # an earlier round's orchestrator designed it; this round's flow cannot name it
            planned_by: tuple[str, ...] = ()
        case NewHypothesis():
            designed = await serial.design(
                ctx,
                start,
                hypotheses=config.hypotheses,
                attribution=config.attribution,
                advise=_advice(ctx, config, start) if config.profiling else None,
            )
            if isinstance(designed, serial.NoDesign):
                return RoundDone(
                    number=start.number,
                    ledger=start.ledger,
                    counted=False,
                    failure=designed.reason,
                )
            ledger, plan, base, feedback = (
                designed.ledger.decode(space),
                designed.plan,
                designed.base,
                None,
            )
            planned_by = (designed.planned_by,)
            ctx.publish(ledger.search)
    active = ledger.search.active_hypothesis
    attempt = MultiAttempt(
        config=config,
        terms=ctx.task.terms,
        baseline=start.baseline,
        number=start.number,
        plan=plan,
        base=base,
        ledger=ledger,
        validation_offered=any(
            item.kind == VALIDATE_LOCAL_KIND for item in ctx.capabilities.operations
        ),
        revalidation_required=active is not None and active.gate_revalidation_pending,
        feedback=feedback,
    )
    settlement = await work(ctx, start, attempt, planned_by)
    closed = attempt.close(settlement)
    ctx.publish(closed.search)
    return serial.round_done(
        start,
        closed,
        hypothesis_id=plan.hypothesis_id,
        settlement=settlement,
        passed=serial.round_passed(settlement, attempt),
        metrics=attempt.benchmark_metrics(),
        measurement_noise=attempt.measurement_noise,
    )


def _advice(ctx: Flow, config: MultiConfig, start: MultiRound) -> serial.Advise:
    """The router and the profiler, asked between the attribution and the plan."""

    async def advise(ledger: rounds.Ledger) -> LastProfile | None:
        hypotheses = config.hypotheses
        focus = serial.focus_of(ledger, serial.focus_policy(config.attribution))
        evidence = serial.plan_evidence(
            hypotheses, ledger, rounds.planning(hypotheses, ledger, start.number), focus
        )
        router = ctx.agents.session(ROUTER)
        routed = await route(
            router,
            PreRoundPrompt(
                round_number=start.number,
                has_history=bool(evidence.history),
                regression_kind=evidence.regression_kind,
                regression_round=evidence.regression_round,
                exhaustion_feedback=evidence.exhaustion_feedback,
                exhaustion_round=evidence.exhaustion_round,
            ),
        )
        if routed is None or not routed.need_profile:
            return None
        return await profile(
            ctx.agents.session(PROFILER, started_by=(router.agent,)),
            ProfilerPrompt(
                round_number=start.number,
                profile_focus=routed.profile_focus or DEFAULT_PROFILE_FOCUS,
            ),
        )

    return advise


async def route(router: Session, prompt: PreRoundPrompt) -> PreRoundDecision | None:
    """Ask whether fresh profile evidence is wanted; None when the router gave no answer."""
    match await router.turn(prompt, label=turn_label(router)):
        case TurnFinished(reply=PreRoundDecision() as decision):
            return decision
        case Failed():
            return None
        case TurnFinished() as unexpected:
            message = f"the router answered with {type(unexpected.reply).__name__}"
            raise TypeError(message)


async def profile(profiler: Session, prompt: ProfilerPrompt) -> LastProfile | None:
    """Ask for a profile; None when the profiler gave no answer."""
    match await profiler.turn(prompt, label=turn_label(profiler)):
        case TurnFinished(reply=ProfilerSummary() as findings):
            return LastProfile(
                source="profiler",
                analysis=findings.analysis,
                bottlenecks=findings.bottlenecks,
                suggestions=findings.suggestions,
                perf_metric=findings.perf_metric,
                perf_unit=findings.perf_unit,
            )
        case Failed():
            return None
        case TurnFinished() as unexpected:
            message = f"the profiler answered with {type(unexpected.reply).__name__}"
            raise TypeError(message)


async def work(
    ctx: Flow, start: RoundStart, attempt: MultiAttempt, planned_by: tuple[str, ...] = ()
) -> Settlement | None:
    """Open the round's workspace, let the implementer work in it, settle it; core's settlement.

    ``planned_by`` names the orchestrator whose plan the implementer works on.
    """

    async def turn(session: Session, ws: Workspace) -> str | None:
        step = await take_turn(ctx, attempt, session, ws)
        if step is not Next.AGAIN or attempt.turns >= attempt.config.max_retries_per_round:
            await measure_evaluated(ctx, attempt, ws)
        if step is Next.AGAIN:
            return attempt.feedback or attempt.no_valid_reply or ""
        return None

    return await serial.work(
        ctx,
        start,
        attempt,
        room=serial.Room(
            implementer=IMPLEMENTER,
            mode=workspace_mode_for(isolated_candidates=not attempt.config.exclusive_root),
            paid_turns=attempt.config.max_retries_per_round,
            started_by=planned_by,
        ),
        turn=turn,
    )


async def measure_evaluated(ctx: Flow, attempt: MultiAttempt, ws: Workspace) -> None:
    """Have the trusted gates read the revision the agent evaluated, when the attempt is over.

    A round whose implementer was blocked or failed, or never replied, produces no checkpoint
    and so reaches no gate. The evaluations it took through the trusted tool are still the
    only measurement of its work, and the designer must not lose it.
    """
    if not attempt.owes_evaluated_measurement():
        return
    revision = ctx.evaluation.last_evaluated(ws)
    if revision is not None:
        attempt.evaluated_gated(
            revision, await serial.measure_gates(ctx, ws, revision, attempt.baseline)
        )


async def take_turn(ctx: Flow, attempt: MultiAttempt, session: Session, ws: Workspace) -> Next:
    """One paid turn: the reply, then the review, the validation and the gates it leads to."""
    prompt = attempt.prompt()
    attempt.turns += 1
    match await session.turn(prompt, label=turn_label(session)):
        case TurnFinished(reply=ImplementerResponse() as reply, checkpoint=checkpoint):
            if isinstance(checkpoint, Declined):
                return attempt.failed(
                    f"implementer checkpoint declined ({checkpoint.reason.value})"
                )
            step = attempt.replied(reply, getattr(checkpoint, "revision", None))
            return await _onward(ctx, attempt, ws, step, session.agent)
        case (
            Failed(
                reason=FailureReason.INVALID_REPLY
                | FailureReason.TIMED_OUT
                | FailureReason.TURN_FAILED
            ) as failed
        ):
            return attempt.no_reply(
                f"the implementer's turn ended without a reply ({failed.reason.value})"
            )
        case Failed() as failed:
            return attempt.failed(f"the implementer's turn ended ({failed.reason.value})")
        case TurnFinished() as unexpected:
            message = f"the implementer answered with {type(unexpected.reply).__name__}"
            raise TypeError(message)


async def _onward(
    ctx: Flow, attempt: MultiAttempt, ws: Workspace, step: Next, implementer: str
) -> Next:
    """Carry a reply through the review, the validation recipes and the trusted gates it owes."""
    if step is Next.REVIEW:
        step = await review(ctx, attempt, ws, implementer)
    if step is Next.VALIDATE:
        step = await validate(ctx, attempt, ws)
    if step is Next.GATES and attempt.candidate is not None:
        step = attempt.gated(
            await serial.measure_gates(ctx, ws, attempt.candidate, attempt.baseline)
        )
    return step


async def review(ctx: Flow, attempt: MultiAttempt, ws: Workspace, implementer: str) -> Next:
    """Have a fresh judge read the exact retained candidate and the evaluations taken of it."""
    revision = attempt.candidate or attempt.base
    cited = () if attempt.candidate is None else ctx.evaluation.taken(ws, revision)
    # the implementer's reply asked for the review: its work started the judge
    judge = ctx.agents.session(JUDGE, workspace=ws, revision=revision, started_by=(implementer,))
    match await judge.turn(attempt.judge_prompt(cited), revision=revision, label=turn_label(judge)):
        case TurnFinished(reply=JudgeResponse() as verdict, invocation=invocation):
            return attempt.judged(verdict, invocation)
        case Failed():
            return attempt.review_lost()
        case TurnFinished() as unexpected:
            message = f"the judge answered with {type(unexpected.reply).__name__}"
            raise TypeError(message)


async def validate(ctx: Flow, attempt: MultiAttempt, ws: Workspace) -> Next:
    """Run the implementer's validation recipes against the candidate it retained."""
    recipe = attempt.recipe()
    if recipe is None or attempt.candidate is None:
        message = "only a retained candidate with a recipe is validated"
        raise ValueError(message)
    report = await ctx.evaluation.validate_local(
        attempt.candidate, recipe, workspace=ws, within=ctx.task.terms.operation_seconds
    )
    return attempt.validated(None if isinstance(report, Failed) else report)
