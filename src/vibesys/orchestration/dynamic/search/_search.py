"""The dynamic search as a story: measure the input, plan and run workstreams, name the winner.

This is the whole search, top to bottom. Planning and the workstreams are tasks in two groups, so
they run concurrently; the root decides only when to start one and when the search is over.
`RunRecord` holds what the search knows, and the tasks' results are folded into it. An operator
stop reaches both groups (workstreams settle what they started; a planning call is dropped, since
nothing it planned would start); the root sees it as `StopRequested`, plans no more, and returns
once nothing is in flight. The winner is named
as the search goes (`ctx.choose`), so a stop or a deadline adopts what led at that moment.
"""

from vibesys.candidates.api import measure_input
from vibesys.orchestration.dynamic.search._config import DynamicConfig
from vibesys.orchestration.dynamic.search._handoff import HANDOFF
from vibesys.orchestration.dynamic.search._operations import OPERATIONS
from vibesys.orchestration.dynamic.search._outcomes import WorkstreamDone
from vibesys.orchestration.dynamic.search._planner import plan
from vibesys.orchestration.dynamic.search._roles import ROLES
from vibesys.orchestration.dynamic.search._run_state import NoPlan, RunRecord, Scheduled
from vibesys.orchestration.dynamic.search._workstream import workstream
from vs_core.api.search import Flow, NoWinner, StopRequested, TaskStopped, search


@search("dynamic", config=DynamicConfig, operations=OPERATIONS, roles=ROLES, owned=(HANDOFF,))
async def dynamic(ctx: Flow, config: DynamicConfig) -> NoWinner | None:
    """Measure the input, run workstreams in the free slots within budget, name the best."""
    ctx.checkpoint(config)
    run = RunRecord(config, ctx.facts.baseline, await measure_input(ctx), ctx.task.terms)
    async with (
        ctx.group(limit=config.max_in_flight) as workstreams,
        ctx.group(limit=1) as planning,
    ):
        while not run.over(planning=not planning.free, stopping=ctx.stop_requested):
            for start in run.ready_to_start(workstreams.free):
                workstreams.start(start.key, workstream, start)
            if planning.free and run.may_plan(stopping=ctx.stop_requested):
                planning.start(run.next_call(), plan, run)
            match await ctx.first(planning, workstreams, ctx.stopped):
                case Scheduled() | NoPlan() as outcome:
                    run.planned(outcome)
                case WorkstreamDone() as done:
                    run.finished(done)
                    ctx.choose(run.leader())
                case TaskStopped(key=key):
                    run.stopped_before_start(key)
                case StopRequested():
                    pass  # the workstreams' group has stopped them
            ctx.publish(run.snapshot())
    return run.ending()
