"""The run-scoped steps that design a hypothesis, shared by the serial searches.

A new hypothesis starts before any candidate does. In the profile-guided preset the configured
attribution command runs first, in a throwaway workspace holding the best revision, and its
ranked components steer the designer. The designer is shown the run's roadmap and the ideas that
passed, and answers with a plan, which may name the base to start from (omitted: the best) and
may change roadmap items: the ledger may refuse the plan (a base that is neither the input nor a
passed idea), or the roadmap the change, in which case it is asked once more with the reason. An
accepted change is put on the roadmap before the hypothesis starts. What a search does between attribution and the plan (multi routes
and profiles) is its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.candidates.api import space_of
from vibesys.hypothesis import HypothesisConfig, OrchestratorPlan, rounds
from vibesys.search_spans.api import turn_label
from vibesys.serial_search._attribution import (
    OUTPUT_ARGUMENT,
    AttributionConfig,
    attributed,
    focus_of,
    focus_policy,
)
from vibesys.serial_search._ledger import LedgerJson
from vibesys.serial_search._plans import accept_plan, base_commit
from vibesys.serial_search._prompts import PlanCorrection, PlanPrompt, correction_of, plan_prompt
from vibesys.serial_search._roadmap import RoadmapUnavailable, put_roadmap, read_roadmap
from vibesys.serial_search._roles import ORCHESTRATOR
from vibesys.serial_search._spans import not_designed, round_begun
from vs_core.api.search import (
    CaptureSpec,
    CommandCapture,
    Failed,
    Flow,
    RevisionRef,
    Session,
    TurnFinished,
    Value,
)
from vs_issue_tracker.api import roadmap_problem

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vibesys.metrics import MetricSpace
    from vibesys.serial_search._plan_view import LastProfile
    from vibesys.serial_search._run import RoundStart

PLAN_CORRECTIONS = 1
"""Times the designer is asked to fix a plan the ledger or the roadmap refused, beyond core's own."""


class AcceptedPlan(Value):
    """The designer's plan, accepted by the ledger."""

    plan: OrchestratorPlan


class Designed(Value):
    """A hypothesis designed for the round: the ledger that started it, its plan and its base.

    ``base`` is the code the idea starts from: the plan's own choice, else the best idea so far.
    ``planned_by`` is the orchestrator agent that planned it, whose plan starts the implementer.
    """

    ledger: LedgerJson
    plan: OrchestratorPlan
    base: RevisionRef
    planned_by: str


class NoDesign(Value):
    """The design ended without a plan; the search ends with this reason."""

    reason: str


async def capture_attribution(  # noqa: PLR0913  # LW-641202 [PLR0913]; the capture needs the preset, the revision, the ledger it updates and the round it is for.
    ctx: Flow,
    attribution: AttributionConfig,
    *,
    base: RevisionRef,
    ledger: LedgerJson,
    number: int,
    space: MetricSpace,
) -> LedgerJson | NoDesign:
    """Run the attribution command at ``base`` and fold its ranked components into the ledger."""
    spec = CaptureSpec(
        revision=base,
        argv=attribution.command,
        output_argument=OUTPUT_ARGUMENT,
        timeout_seconds=attribution.timeout_seconds,
    )
    within = float(attribution.timeout_seconds) + ctx.task.terms.operation_seconds
    match await ctx.evaluation.capture(spec, within=within):
        case Failed() as failed:
            reason = f"profile-guided attribution failed: the command ended ({failed.reason.value})"
            return NoDesign(reason=reason)
        case None:
            return NoDesign(reason="profile-guided attribution failed: no result")
        case CommandCapture() as capture:
            folded = attributed(ledger.decode(space), attribution, capture, round_number=number)
            return NoDesign(reason=folded) if isinstance(folded, str) else LedgerJson.of(folded)


async def ask_plan(
    planner: Session,
    prompt: PlanPrompt,
    *,
    hypotheses: HypothesisConfig,
    ledger: rounds.Ledger,
    start: RoundStart,
) -> AcceptedPlan | NoDesign:
    """Ask the designer for a plan; when the ledger or the roadmap refuses it, ask again.

    The roadmap refuses a change that `roadmap_problem` names over the items the prompt showed.
    """
    asked: PlanPrompt | PlanCorrection = prompt
    problem = "the designer did not answer"
    for _ in range(1 + PLAN_CORRECTIONS):
        match await planner.turn(asked, label=turn_label(planner)):
            case TurnFinished(reply=OrchestratorPlan() as reply, invocation=turn):
                accepted = accept_plan(hypotheses, ledger, reply, start=start)
                if isinstance(accepted, OrchestratorPlan):
                    refused = roadmap_problem(prompt.roadmap, accepted.roadmap)
                    if refused is None:
                        return AcceptedPlan(plan=accepted)
                    accepted = refused
                problem = accepted
                asked = correction_of(prompt, turn, problem)
            case Failed() as failed:
                return NoDesign(
                    reason=f"the designer's turn ended without a plan ({failed.reason.value})"
                )
            case TurnFinished() as unexpected:
                message = f"the designer answered with {type(unexpected.reply).__name__}"
                raise TypeError(message)
    return NoDesign(reason=problem)


type Advise = Callable[[rounds.Ledger], Awaitable[LastProfile | None]]
"""What a search does between attribution and the plan; the profile it learned, if any."""


async def design(
    ctx: Flow,
    start: RoundStart,
    *,
    hypotheses: HypothesisConfig,
    attribution: AttributionConfig | None,
    advise: Advise | None = None,
) -> Designed | NoDesign:
    """Design a new hypothesis for the round: attribution, the search's own advice, the plan.

    The ledger returned has the hypothesis started in it, on the base the plan named or else the
    best idea so far. The round's workstream is labelled while it designs, and failed when no
    design comes of it.
    """
    round_begun(ctx, start)
    designed = await _design(
        ctx, start, hypotheses=hypotheses, attribution=attribution, advise=advise
    )
    if isinstance(designed, NoDesign):
        not_designed(ctx)
    return designed


async def _design(
    ctx: Flow,
    start: RoundStart,
    *,
    hypotheses: HypothesisConfig,
    attribution: AttributionConfig | None,
    advise: Advise | None,
) -> Designed | NoDesign:
    terms = ctx.task.terms
    space = space_of(terms)
    ledger = start.ledger.decode(space)
    if attribution is not None:
        folded = await capture_attribution(
            ctx, attribution, base=start.base, ledger=start.ledger, number=start.number, space=space
        )
        if isinstance(folded, NoDesign):
            return folded
        ledger = folded.decode(space)
    profile = start.last_profile if advise is None else (await advise(ledger)) or start.last_profile
    roadmap = await read_roadmap(ctx)
    if isinstance(roadmap, RoadmapUnavailable):
        return NoDesign(reason=roadmap.reason)
    prompt = plan_prompt(
        hypotheses,
        ledger,
        rounds.planning(hypotheses, ledger, start.number),
        focus_of(ledger, focus_policy(attribution)),
        start=start,
        benchmark_enabled="benchmark" in terms.measured,
        last_profile=profile,
        roadmap=roadmap,
    )
    orchestrator = ctx.agents.session(ORCHESTRATOR)
    asked = await ask_plan(
        orchestrator,
        prompt,
        hypotheses=hypotheses,
        ledger=ledger,
        start=start,
    )
    if isinstance(asked, NoDesign):
        return asked
    unwritten = await put_roadmap(ctx, tuple(asked.plan.roadmap))
    if unwritten is not None:
        return NoDesign(reason=unwritten.reason)
    base = base_commit(asked.plan, start, input_revision=ctx.facts.baseline)
    started_ledger, _ = rounds.start(
        hypotheses,
        ledger,
        asked.plan,
        round_number=start.number,
        parent=rounds.Parent(base.round, base.revision.revision_id.root),
    )
    return Designed(
        ledger=LedgerJson.of(started_ledger),
        plan=asked.plan,
        base=base.revision,
        planned_by=orchestrator.agent,
    )
