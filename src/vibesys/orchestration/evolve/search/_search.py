"""The evolve search as a story: a cold start until one child passes, then generations of children.

This is the whole search, top to bottom. A round is the cold start (one child at a time, each
repairing the last one's work in progress) or one generation (up to `max_parallelism` children at
once, their parents chosen from the population as the generation began). The root decides only
when the next round starts and when the search is over. The winner is named as the search goes
(`ctx.choose`), so a stop or a deadline adopts what led at that moment.
"""

from vibesys.orchestration.evolve.search._child import child
from vibesys.orchestration.evolve.search._config import EvolveConfig
from vibesys.orchestration.evolve.search._operations import OPERATIONS
from vibesys.orchestration.evolve.search._record import EvolveRecord
from vibesys.orchestration.evolve.search._roles import ROLES
from vibesys.orchestration.evolve.search._round import Round
from vibesys.orchestration.evolve.search._spans import builds_on
from vibesys.orchestration.evolve.search._values import STOPPED, ChildDone, Closing
from vibesys.search_spans.api import SPANS
from vs_core.api.search import Flow, Group, NoWinner, SpanId, TaskStopped, search


@search("evolve", config=EvolveConfig, operations=OPERATIONS, roles=ROLES, spans=SPANS)
async def evolve(ctx: Flow, config: EvolveConfig) -> NoWinner | None:
    """Seed the population, evolve it generation by generation, name the best."""
    ctx.checkpoint(config)
    run = EvolveRecord.begin(config, ctx.facts.baseline, ctx.task.terms)
    ctx.fall_back_to_input(run.leading()[1])
    while (round_ := run.open_round(stopping=ctx.stop_requested)) is not None:
        await run_round(ctx, run, round_)
        run.close_round(round_)
        ctx.publish(run.snapshot())
    return run.ending()


async def run_round(ctx: Flow, run: EvolveRecord, round_: Round) -> None:
    """Run a round's children, admitting each outcome in slot order as it becomes admissible."""
    spans: dict[str, SpanId | None] = {}
    async with ctx.group(limit=round_.width) as children:
        launch(ctx, run, children, round_, spans)
        ctx.publish(run.snapshot())
        async for result in children:
            for done in round_.finish(finished(round_, result)):
                run.admit(done, spans.get(done.start.key))
            ctx.choose(*run.leading())
            launch(ctx, run, children, round_, spans)
            ctx.publish(run.snapshot())


def launch(
    ctx: Flow,
    run: EvolveRecord,
    children: Group[ChildDone],
    round_: Round,
    spans: dict[str, SpanId | None],
) -> None:
    """Start the children that fit in the free slots, each linked to the one it builds on."""
    for start in round_.ready(children.free, stopping=ctx.stop_requested):
        span = children.start(start.key, child, start).span
        spans[start.key] = span
        builds_on(ctx, span, run.built_by(start.base))


def finished(round_: Round, result: ChildDone | TaskStopped) -> ChildDone:
    """A child's outcome; a child the stop ended before it began is a stopped one."""
    if isinstance(result, ChildDone):
        return result
    start = next(item for item in round_.starts if item.key == result.key)
    return ChildDone(start, Closing().ended_by(STOPPED))
