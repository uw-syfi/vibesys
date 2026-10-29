"""One round of the single search: design a hypothesis, implement it, measure it, close the round.

A round is a task of the root: it takes where the search stands and ends with what it left behind.
A hypothesis the ledger keeps active is continued without the designer. A new one is designed
first (the attribution command in the profile-guided preset, then the designer's plan). The one
implementer agent then works in an isolated candidate workspace: it implements, reviews its own
work and, when the cadence says so, the trusted gates measure what it kept. The workspace is a
resource, so it always settles: with the assertion the attempt made, with a failure, or as
cancelled when the operator stops the run.
"""

from typing import Self

from vibesys.candidates.api import space_of, workspace_mode_for
from vibesys.hypothesis import Continue, Finished, NewHypothesis, rounds
from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vibesys.orchestration.single.search._attempt import Next, SingleAttempt
from vibesys.orchestration.single.search._config import SingleConfig
from vibesys.orchestration.single.search._roles import IMPLEMENTER
from vibesys.search_spans.api import turn_label
from vibesys.serial_search import api as serial
from vibesys.serial_search.api import RoundDone, RoundStart
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


class SingleRound(RoundStart):
    """A round told where the search stands, with the config the search runs under."""

    config: SingleConfig

    @classmethod
    def of(cls, begin: RoundStart, config: SingleConfig) -> Self:
        """``begin`` with the config the search runs under."""
        fields = {name: getattr(begin, name) for name in RoundStart.model_fields}
        return cls(**fields, config=config)


@flow(snapshot=SingleRound)
async def play_round(ctx: Flow, start: SingleRound) -> RoundDone:
    """Play one round to the settlement of its workspace."""
    ctx.checkpoint(start)  # restartable from its start: nothing else is needed to redo it
    try:
        return await _play(ctx, start)
    except Stopped:
        return RoundDone(number=start.number, ledger=start.ledger, counted=False, stopped=True)


async def _play(ctx: Flow, start: SingleRound) -> RoundDone:
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
                ctx, start, hypotheses=config.hypotheses, attribution=config.attribution
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
    attempt = SingleAttempt(
        config=config,
        terms=ctx.task.terms,
        baseline=start.baseline,
        number=start.number,
        plan=plan,
        base=base,
        ledger=ledger,
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
        last_profile=attempt.last_profile(),
        measurement_noise=attempt.measurement_noise,
    )


async def work(
    ctx: Flow, start: RoundStart, attempt: SingleAttempt, planned_by: tuple[str, ...] = ()
) -> Settlement | None:
    """Open the round's workspace, let the implementer work in it, settle it; core's settlement.

    ``planned_by`` names the orchestrator whose plan the implementer works on.
    """

    async def turn(session: Session, ws: Workspace) -> str | None:
        step = await take_turn(ctx, attempt, session, ws)
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


async def take_turn(ctx: Flow, attempt: SingleAttempt, session: Session, ws: Workspace) -> Next:
    """One paid turn: the reply judged, and the trusted gates when it passes and they are due."""
    prompt = attempt.prompt()
    attempt.turns += 1
    match await session.turn(prompt, label=turn_label(session)):
        case TurnFinished(reply=SingleAgentRoundResponse() as reply, checkpoint=checkpoint):
            if isinstance(checkpoint, Declined):
                return attempt.failed(
                    f"implementer checkpoint declined ({checkpoint.reason.value})"
                )
            step = attempt.replied(reply, getattr(checkpoint, "revision", None))
            candidate = attempt.candidate
            if step is Next.GATES and candidate is not None:
                return attempt.gated(
                    await serial.measure_gates(ctx, ws, candidate, attempt.baseline)
                )
            return step
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
