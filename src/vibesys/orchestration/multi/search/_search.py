"""The multi search as a story: measure the input, play rounds one after another, name the winner.

This is the whole search, top to bottom. Each round is a task in a group of one: the root decides
only when to start the next and when the search is over. `SerialRun` holds what the search knows
and folds the finished rounds into it, and is shared with the single search. An operator stop
reaches the round (it settles what it started); the root sees it as `StopRequested`, begins no
more, and returns. The winner is named as the search goes (`ctx.choose`), so a stop or a
deadline adopts what led at that moment.
"""

from vibesys.orchestration.multi.search._config import MultiConfig
from vibesys.orchestration.multi.search._operations import OPERATIONS
from vibesys.orchestration.multi.search._resume import MultiResume
from vibesys.orchestration.multi.search._roles import ROLES
from vibesys.orchestration.multi.search._round import MultiRound, play_round
from vibesys.search_spans.api import SPANS, adoption_label
from vibesys.serial_search.api import ROADMAP, RoundDone, open_run
from vs_core.api.search import INPUT, Flow, NoWinner, StopRequested, TaskStopped, search


@search(
    "multi",
    config=MultiConfig,
    snapshot=MultiResume,
    operations=OPERATIONS,
    roles=ROLES,
    owned=ROADMAP,
    spans=SPANS,
)
async def multi(ctx: Flow, given: MultiConfig | MultiResume) -> NoWinner | None:
    """Measure the input, play the rounds in order within budget, name the best reviewed candidate."""
    resumed = isinstance(given, MultiResume)
    config = given.config if resumed else given
    ctx.checkpoint(given)  # until the input is measured, a restart starts from here
    ctx.fall_back_to_input(adoption_label(INPUT, None, None))
    run = await open_run(ctx, config.hypotheses, given.state if resumed else None)
    ctx.checkpoint(MultiResume(config=config, state=run.state()))
    ctx.fall_back_to_input(run.leading()[1])
    async with ctx.group(limit=1) as rounds:
        while not run.over(stopping=ctx.stop_requested):
            begin = run.start()
            task = rounds.start(f"{begin.number:06d}", play_round, MultiRound.of(begin, config))
            # a restart on changed code resumes the round under way and the search's record
            ctx.checkpoint(MultiResume(config=config, state=run.state()))
            match await ctx.first(rounds, ctx.stopped):
                case RoundDone() as done:
                    run.finished(done, task.span, stopping=ctx.stop_requested)
                    ctx.choose(*run.leading())
                case TaskStopped():
                    run.stopped = run.stopped or ctx.stop_requested
                case StopRequested():
                    # The group has stopped the round, which settles what it started. The stop
                    # is judged once it has: a resume that lifts it meanwhile lets the search
                    # go on with the rounds it still has.
                    match await ctx.first(rounds):
                        case RoundDone() as done:
                            run.finished(done, task.span, stopping=ctx.stop_requested)
                            ctx.choose(*run.leading())
                        case TaskStopped():
                            run.stopped = run.stopped or ctx.stop_requested
            ctx.publish(run.snapshot())
        ctx.checkpoint(MultiResume(config=config, state=run.state()))  # the record is final
    return run.ending()
