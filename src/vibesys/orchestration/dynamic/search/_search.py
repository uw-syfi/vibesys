"""The dynamic search as a story: measure the input and plan at once, run workstreams, name a winner.

This is the whole search, top to bottom. The input's measurement, planning and the workstreams
are tasks in three groups, so they run concurrently; the root decides only when to start one and
when the search is over. Planning does not wait for the input's measurement (on a shared cluster
it may wait hours for a slot): the planner reads the task and the code meanwhile, and is told the
measurement is pending. Workstreams start too: their agents write code and run their own jobs,
and only what compares a candidate with the input (its trusted measurement, the judge's review,
its settlement) waits for the input's measurement, which the root hands them through the
`INPUT_MEASURED` latch when it lands (`_measure.the_input`). An input that fails its measurement
drops the plans not yet started, and planning is asked again (`RunRecord.measured`).
`RunRecord` holds what the search knows, and the tasks' results are folded into it. An operator
stop reaches every group (workstreams settle what they started; a planning call and the input's
measurement are dropped, since nothing they would lead to would start); the root sees it as
`StopRequested`, plans no more, and returns once nothing is in flight. The winner is named
as the search goes (`ctx.choose`), so a stop or a deadline adopts what led at that moment.
"""

from typing import assert_never

from vibesys.candidates.api import Baseline, Measured, NotConfigured, Unmeasurable, measure_input
from vibesys.orchestration.dynamic.search._config import DynamicConfig
from vibesys.orchestration.dynamic.search._handoff import HANDOFF
from vibesys.orchestration.dynamic.search._ids import INPUT_KEY, INPUT_MEASURED
from vibesys.orchestration.dynamic.search._operations import OPERATIONS
from vibesys.orchestration.dynamic.search._outcomes import WorkstreamDone
from vibesys.orchestration.dynamic.search._planner import plan
from vibesys.orchestration.dynamic.search._roles import ROLES
from vibesys.orchestration.dynamic.search._run_state import (
    NoPlan,
    PlanOutcome,
    RunRecord,
    Scheduled,
)
from vibesys.orchestration.dynamic.search._workstream import workstream
from vs_core.api.search import Flow, Group, NoWinner, StopRequested, TaskStopped, search


@search("dynamic", config=DynamicConfig, operations=OPERATIONS, roles=ROLES, owned=(HANDOFF,))
async def dynamic(ctx: Flow, config: DynamicConfig) -> NoWinner | None:
    """Measure the input while planning, run workstreams in the free slots, name the best."""
    ctx.checkpoint(config)
    run = RunRecord(config, ctx.facts.baseline, None, ctx.task.terms)
    # typed, so the match on what they yield below is checked exhaustive
    workstreams: Group[WorkstreamDone] = ctx.group(limit=config.max_in_flight)
    planning: Group[PlanOutcome] = ctx.group(limit=1)
    measuring: Group[Baseline] = ctx.group(limit=1)
    async with workstreams, planning, measuring:
        measuring.start(INPUT_KEY, measure_input)
        while not run.over(
            planning=not planning.free,
            measuring=not measuring.free,
            stopping=ctx.stop_requested,
        ):
            for start in run.ready_to_start(workstreams.free):
                workstreams.start(start.key, workstream, start)
            # an input with nothing to measure has its result in at once: read it before planning
            unread = run.baseline is None and measuring.free
            if planning.free and not unread and run.may_plan(stopping=ctx.stop_requested):
                planning.start(run.next_call(), plan, run)
            match await ctx.first(measuring, planning, workstreams, ctx.stopped):
                case Measured() | Unmeasurable() | NotConfigured() as baseline:
                    run.measured(baseline)
                    ctx.latch(INPUT_MEASURED).set(baseline)  # the workstreams waiting for it go on
                case Scheduled() | NoPlan() as outcome:
                    run.planned(outcome)
                case WorkstreamDone() as done:
                    run.finished(done)
                    ctx.choose(run.leader())
                case TaskStopped(key=key):
                    run.stopped_before_start(key)
                case StopRequested():
                    pass  # the groups have stopped their tasks
                case unexpected:
                    assert_never(unexpected)
            ctx.publish(run.snapshot())
    return run.ending()
