"""The single search as a story: measure the input, play rounds one after another, name the winner.

This is the whole search, top to bottom. Each round is a task in a group of one: the root decides
only when to start the next and when the search is over. `SerialRun` holds what the search knows
and folds the finished rounds into it. An operator stop reaches the round (it settles what it
started); the root sees it as `StopRequested`, begins no more, and returns. The winner is named as
the search goes (`ctx.choose`), so a stop or a deadline adopts what led at that moment.
"""

from vibesys.orchestration.single.search._config import SingleConfig
from vibesys.orchestration.single.search._operations import OPERATIONS
from vibesys.orchestration.single.search._resume import SingleResume
from vibesys.orchestration.single.search._roles import ROLES
from vibesys.orchestration.single.search._round import SingleRound, play_round
from vibesys.search_spans.api import SPANS, adoption_label
from vibesys.serial_search.api import ROADMAP, RoundDone, open_run
from vs_core.api.search import (
    INPUT,
    Flow,
    NoWinner,
    StopRequested,
    TaskStopped,
    Uncompared,
    search,
)


@search(
    "single",
    config=SingleConfig,
    snapshot=SingleResume,
    operations=OPERATIONS,
    roles=ROLES,
    owned=ROADMAP,
    spans=SPANS,
)
async def single(ctx: Flow, given: SingleConfig | SingleResume) -> NoWinner | Uncompared | None:
    """Measure the input, play the rounds in order within budget, name the best candidate."""
    resumed = isinstance(given, SingleResume)
    config = given.config if resumed else given
    ctx.checkpoint(given)  # until the input is measured, a restart starts from here
    ctx.fall_back_to_input(adoption_label(INPUT, None, None))
    run = await open_run(ctx, config.hypotheses, given.state if resumed else None)
    ctx.checkpoint(SingleResume(config=config, state=run.state()))
    ctx.fall_back_to_input(run.leading()[1])
    async with ctx.group(limit=1) as rounds:
        while not run.over(stopping=ctx.stop_requested):
            begin = run.start()
            task = rounds.start(f"{begin.number:06d}", play_round, SingleRound.of(begin, config))
            # a restart on changed code resumes the round under way and the search's record
            ctx.checkpoint(SingleResume(config=config, state=run.state()))
            match await ctx.first(rounds, ctx.stopped):
                case RoundDone() as done:
                    run.finished(done, task.span)
                    ctx.choose(*run.leading())
                case TaskStopped():
                    pass  # the round settled what it started; `over` judges the stop
                case StopRequested():
                    # The group has stopped the round, which settles what it started. The stop
                    # is judged once it has: a resume that lifts it meanwhile lets the search
                    # go on with the rounds it still has.
                    match await ctx.first(rounds):
                        case RoundDone() as done:
                            run.finished(done, task.span)
                            ctx.choose(*run.leading())
                        case TaskStopped():
                            pass  # the round settled what it started; `over` judges the stop
            ctx.publish(run.snapshot())
        ctx.checkpoint(SingleResume(config=config, state=run.state()))  # the record is final
    return run.ending()
