"""One planning call: ask for a portfolio, correct it, and schedule what is valid.

The planner is a run-scoped, free agent. Its reply arrives already parsed against the reply
model; `plan` validates it against what the run has recorded, asks for a correction up to
`MAX_CORRECTIONS` times, and when the corrections do not fix the plan keeps the best valid
outcome: a held underfilled plan, else the valid part of the last reply, else a fresh question,
else a typed give-up. A lost turn is asked again, a few times, after a backoff on the run clock.
A call that schedules a plan has its handoff file written before it ends (see `_handoff`).

The planner's role has core's own corrections and re-asks off: every ask carries a prompt built
from the run at that moment, and every correction names the problem as typed data.
"""

from dataclasses import dataclass, replace

from pydantic import BaseModel

from vibesys.orchestration.dynamic.models import PortfolioPlan
from vibesys.orchestration.dynamic.search._context import PlanWorld
from vibesys.orchestration.dynamic.search._handoff import HandoffWritten, handoff_request
from vibesys.orchestration.dynamic.search._outline import outline
from vibesys.orchestration.dynamic.search._plan import PlanCheck, validate
from vibesys.orchestration.dynamic.search._roles import LOST_TURN_BACKOFF, PLANNER
from vibesys.orchestration.dynamic.search._run_state import (
    NoPlan,
    PlanOutcome,
    RunRecord,
    Scheduled,
)
from vibesys.orchestration.dynamic.search._values import (
    MergeRecord,
    PlanProblem,
    ProblemKind,
    WorkKind,
)
from vibesys.search_spans.api import PLANNING_CALL, PlanningAttrs, turn_label
from vs_core.api.search import (
    Again,
    Failed,
    FailureReason,
    Flow,
    GaveUp,
    InvocationRef,
    RetryPolicy,
    Session,
    Tries,
    TurnFinished,
)

MAX_CORRECTIONS = 1
"""Times one planning call corrects a reply that cannot be scheduled before it settles for the best."""

MAX_FRESH_ASKS = 1
"""Fresh questions a planning call asks, with nothing running, after its corrections gave nothing."""

LOST_TURNS = RetryPolicy(max_tries=3, backoff=LOST_TURN_BACKOFF)
"""A planner turn the agent lost is asked again twice, after a growing wait on the run clock."""


@dataclass
class Call:
    """What one planning call has spent and kept, which its asks and judgments read."""

    number: int
    """The planning call's number, counted from 1: its handoff file's identity."""
    capacity: int
    """The slots that were free when the call began; a reply should fill them."""
    problem: PlanProblem | None
    """Why the last reply was not taken; the next prompt shows it."""
    again: InvocationRef | None = None
    """The turn the next ask corrects; None for a fresh question."""
    corrections: int = 0
    fresh: int = 0
    held: PlanCheck | None = None
    """The first valid plan that left slots free, kept in case a correction does not improve it."""
    held_blind: bool = False
    """The held plan answered a prompt asked before the input's measurement landed."""


@dataclass(frozen=True)
class Reply:
    """A planner turn that ended, judged against the run as it was when it arrived."""

    plan: PlanCheck | None
    problem: PlanProblem | None
    turn: InvocationRef | None
    world: PlanWorld
    blind: bool = False
    """The prompt it answered was asked before the input's measurement landed."""


async def plan(ctx: Flow, run: RunRecord) -> PlanOutcome:
    """Ask the planner for a portfolio for the free slots, until one can be scheduled.

    A planning call is not restartable from a snapshot: after a code change the root plans again.
    A run that follows a plan declaring no steps asks for its outline first (`outline`).
    """
    ctx.label(
        PLANNING_CALL, title=f"planning call {run.calls}", attrs=PlanningAttrs(call=run.calls)
    )
    if run.needs_outline:
        return await outline(ctx, run)
    planner = ctx.agents.session(PLANNER)
    call = Call(number=run.calls, capacity=run.capacity(), problem=run.gate.last_error)

    async def ask(tries: Tries[Failed]) -> Reply | NoPlan | Again[Failed]:
        return await ask_planner(planner, run, call, tries.reasons, now=ctx.clock.now())

    while True:
        match await ctx.retry(ask, LOST_TURNS):
            case GaveUp(reasons=lost):
                return _no_plan(run.world(), _no_reply(lost[-1]))
            case NoPlan() as outcome:
                return outcome
            case Reply() as reply:
                if (outcome := _judge(call, reply)) is not None:
                    handed = await _handed_off(ctx, call.number, outcome, run.world().merges)
                    # the plans this call scheduled are the planner's: its work starts them
                    if isinstance(handed, Scheduled):
                        return replace(handed, planner=planner.agent)
                    return handed


async def _handed_off(
    ctx: Flow, number: int, outcome: PlanOutcome, merges: tuple[MergeRecord, ...]
) -> PlanOutcome:
    """A scheduled plan with its handoff file written; a call that planned nothing writes none.

    A write whose wait timed out or was lost may have landed, so it is sent again (the write is
    keyed, so the same bytes land). One that still fails leaves the plan standing without a new
    listing: the plan is what the run needs, and the next successful write lists every file the
    owner holds, this one included if its bytes landed.
    """
    if not isinstance(outcome, Scheduled):
        return outcome
    request = handoff_request(number, outcome.check, merges)

    async def write(tries: Tries[Failed]) -> BaseModel | Failed | Again[Failed]:
        del tries
        written = await ctx.owners.perform(request, within=ctx.task.terms.operation_seconds)
        if isinstance(written, Failed) and written.reason in _UNKNOWN_WRITE:
            return Again(reason=written)
        return written

    match await ctx.retry(write, HANDOFF_TRIES):
        case HandoffWritten() as written:
            return Scheduled(outcome.check, blind=outcome.blind, handoff=written)
        case _:
            return outcome


HANDOFF_TRIES = RetryPolicy(max_tries=2)
"""A handoff write whose outcome is unknown is sent once more."""

_UNKNOWN_WRITE = frozenset({FailureReason.TIMED_OUT, FailureReason.LOST})
"""The failures after which the write may have landed."""


async def ask_planner(
    planner: Session,
    run: RunRecord,
    call: Call,
    lost: tuple[Failed, ...],
    *,
    now: float | None = None,
) -> Reply | NoPlan | Again[Failed]:
    """One ask: the planner's reply, a plan-less end, or a lost turn to ask again.

    ``now`` is the run clock when it is asked: the request says how long candidates have waited.
    """
    asked_of = lost[-1].turn if lost else call.again
    asked = run.world(now)
    prompt = asked.prompt(call.problem, again=asked_of)
    event = await planner.turn(prompt, label=turn_label(planner))
    world = run.world()  # the run as it is when the reply arrives
    match event:
        case TurnFinished(reply=PortfolioPlan() as given, invocation=turn):
            return Reply(_check(given, world), None, turn, world, blind=asked.baseline is None)
        case Failed(reason=FailureReason.INVALID_REPLY, errors=errors, turn=turn):
            unparsed = PlanProblem(kind=ProblemKind.UNPARSEABLE, errors=errors)
            return Reply(None, unparsed, turn, world)
        case Failed(reason=FailureReason.TIMED_OUT | FailureReason.TURN_FAILED, turn=turn):
            return Reply(None, _no_reply(event), turn, world)
        case Failed(reason=FailureReason.LOST, turn=lost_turn) if lost_turn is not None:
            return Again(reason=event)
        case Failed():
            return _no_plan(world, _no_reply(event))
        case TurnFinished() as unexpected:
            message = f"the planner answered with {type(unexpected.reply).__name__}"
            raise TypeError(message)


def _judge(call: Call, reply: Reply) -> PlanOutcome | None:
    """Judge a reply: schedule it, keep the best, give up, or set up the next ask (None)."""
    world = reply.world
    check = reply.plan
    # The planner was asked to fill the slots free when the call began. A workstream that
    # finished while it was answering frees another slot, and the next call fills that one;
    # judging the reply against the later capacity would spend a correction on a slot the
    # planner was never offered.
    want = min(call.capacity, world.wanted) if call.capacity else world.wanted
    if check is not None and check.valid and len(check.accepted) >= want:
        return Scheduled(check, blind=reply.blind)
    call.problem = _problem(check, reply.problem, want)
    if reply.turn is not None and call.corrections < MAX_CORRECTIONS:
        call.corrections += 1
        if (held := _held(check)) is not None:
            call.held, call.held_blind = held, reply.blind
        call.again = reply.turn
        return None
    chosen = _final_choice(call, world, check)
    if chosen is not None:
        return Scheduled(chosen, blind=reply.blind if chosen is check else call.held_blind)
    if world.active == 0 and call.fresh < MAX_FRESH_ASKS:
        call.fresh += 1
        call.held = None
        call.corrections = 0
        call.again = None
        return None
    return _no_plan(world, call.problem)


def _check(given: PortfolioPlan, world: PlanWorld) -> PlanCheck:
    return validate(given, world.ledger, world.offered, capacity=world.capacity, offer=world.offer)


def _held(check: PlanCheck | None) -> PlanCheck | None:
    return check if check is not None and check.valid and check.accepted else None


def _no_reply(event: Failed) -> PlanProblem:
    return PlanProblem(
        kind=ProblemKind.NO_REPLY, ended=event.reason, turn_failure=event.turn_failure
    )


def _no_plan(world: PlanWorld, problem: PlanProblem | None) -> NoPlan:
    """A call that ended empty; it is stuck only when nothing is in flight to wait for.

    The input's measurement still running is something to wait for: planning is asked again
    when it lands.
    """
    stuck = world.active == 0 and world.baseline is not None
    return NoPlan(problem=problem, stuck=stuck, blocked_at_done=world.done)


def _problem(check: PlanCheck | None, found: PlanProblem | None, wanted: int) -> PlanProblem:
    """What was wrong with the reply, as the correction names it."""
    if found is not None:
        return found
    if check is None or check.valid:
        scheduled = 0 if check is None else len(check.accepted)
        return PlanProblem(kind=ProblemKind.UNDERFILLED, scheduled=scheduled, wanted=wanted)
    return PlanProblem(kind=ProblemKind.VIOLATIONS, violations=check.violations)


def _final_choice(call: Call, world: PlanWorld, check: PlanCheck | None) -> PlanCheck | None:
    """The best valid outcome once corrections are exhausted."""
    if check is not None and check.valid and check.accepted:
        return check
    if call.held is not None and _schedulable(call.held, world):
        return call.held
    if check is not None and check.accepted:
        return check
    return None


def _schedulable(held: PlanCheck, world: PlanWorld) -> bool:
    """Whether a plan that was valid when it was held still fits what the run has now.

    The run may have moved while the corrections ran: a slot may have filled, an ID may have
    been taken, a parent may no longer be offered.
    """
    ledger = world.ledger
    taken = {item.hypothesis_id for item in ledger.hypotheses} | ledger.running | ledger.profile_ids
    taken |= ledger.merge_ids
    offered = {
        (item.snapshot.hypothesis_id, item.snapshot.revision.revision_id.root)
        for item in world.offered
    }
    fresh = all(item.continue_hypothesis or item.work_id not in taken for item in held.accepted)
    parents = all(
        item.parent_hypothesis_id is None
        or item.parent_revision is None
        or (item.parent_hypothesis_id, item.parent_revision) in offered
        for item in held.accepted
    )
    options = ledger.merge_options
    merges = [item for item in held.accepted if item.kind is WorkKind.MERGE]
    mergeable = not (merges and ledger.merging) and all(
        options.mergeable(candidate) for item in merges for candidate in item.candidates
    )
    return len(held.accepted) <= world.capacity and fresh and parents and mergeable
