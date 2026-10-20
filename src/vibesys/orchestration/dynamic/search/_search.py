"""The dynamic search as a story: measure the input and plan at once, run workstreams, name a winner.

This is the whole search, top to bottom. The input's measurement, planning and the workstreams
are tasks in three groups, so they run concurrently; the root decides only when to start one and
when the search is over. Planning does not wait for the input's measurement (on a shared cluster
it may wait hours for a slot): the planner reads the task and the code meanwhile, and is told the
measurement is pending. Workstreams start too: their agents write code, run their own jobs and
are reviewed; a candidate's trusted measurement, and a settlement that scores it against the
input, wait for the input's measurement, which the root hands them through the `INPUT_MEASURED`
latch when it lands (`_measure.the_input`). An input that fails its measurement drops the plans not yet started, and
planning is asked again (`RunRecord.measured`).

Agents never wait for a trusted evaluation slot. A workstream whose candidate is ready submits it
on the `REPORTS` channel and waits; its agent slot is free at once, so the planner can fill it.
The root keeps the submissions in a queue (`RunRecord.submit`) and, each time a slot is free,
binds the one the run's pick policy chooses (the newest, by default) and answers each workstream
on its own `VERDICTS` latch: measure now, or never, because a newer submission superseded it or
the queue overflowed. An agent turn core suspended on its own evaluations frees its slot the same
way (`RunRecord.suspend`); once core authorizes its resume, the root lends it the next free slot
on its `RESUMES` latch (`RunRecord.resumes`), so agents at work never outnumber `max_in_flight`.
A workstream that waits too long for the input's measurement reports it, and
the planner is told on its next turn (`RunRecord.overdue`).

`RunRecord` holds what the search knows, and the tasks' results are folded into it. An operator
stop reaches every group (workstreams settle what they started; a planning call and the input's
measurement are dropped, since nothing they would lead to would start); the root sees it as
`StopRequested`, plans and binds no more, and returns once nothing is in flight. The winner is
named as the search goes (`ctx.choose`), so a stop or a deadline adopts what led at that moment.

Before each wait the root checkpoints its record (`DynamicResume`). After a code change it
resumes from there (`resume`): the input is not measured again once its measurement landed,
every running workstream resumes from its own checkpoint, and every verdict already decided is
handed again, since a restart empties the latches. A workstream that waited in a suspended turn
asks its agent again only once the root lends it a slot (`RunRecord.held_over`).
"""

from typing import assert_never

from vibesys.candidates.api import Baseline, Measured, NotConfigured, Unmeasurable
from vibesys.orchestration.dynamic.search._config import DynamicConfig
from vibesys.orchestration.dynamic.search._handoff import HANDOFF
from vibesys.orchestration.dynamic.search._ids import INPUT_KEY, INPUT_MEASURED
from vibesys.orchestration.dynamic.search._measure import InputPending, measure_the_input
from vibesys.orchestration.dynamic.search._operations import OPERATIONS
from vibesys.orchestration.dynamic.search._outcomes import WorkstreamDone
from vibesys.orchestration.dynamic.search._planner import plan
from vibesys.orchestration.dynamic.search._queue import Submission
from vibesys.orchestration.dynamic.search._reports import (
    REPORTS,
    RESUMES,
    VERDICTS,
    Granted,
    InputOverdue,
    Released,
    Report,
    Resuming,
    Suspended,
)
from vibesys.orchestration.dynamic.search._roles import ROLES
from vibesys.orchestration.dynamic.search._run_state import (
    DynamicResume,
    NoPlan,
    Outlined,
    PlanOutcome,
    RunRecord,
    Scheduled,
)
from vibesys.orchestration.dynamic.search._spans import BuildsOn
from vibesys.orchestration.dynamic.search._workstream import workstream
from vibesys.search_spans.api import SPANS
from vs_core.api.search import (
    Flow,
    Group,
    NoWinner,
    StopRequested,
    TaskStopped,
    Uncompared,
    search,
)


@search(
    "dynamic",
    config=DynamicConfig,
    operations=OPERATIONS,
    roles=ROLES,
    snapshot=DynamicResume,
    owned=(HANDOFF,),
    spans=SPANS,
)
async def dynamic(ctx: Flow, given: DynamicConfig | DynamicResume) -> NoWinner | Uncompared | None:
    """Measure the input while planning, run workstreams in the free slots, name the best."""
    base, terms = ctx.facts.baseline, ctx.task.terms
    if isinstance(given, DynamicResume):
        run = RunRecord.resumed(given.config, base, terms, given.record)
        run.held_over()
    else:
        run = RunRecord(given, base, None, terms)
    ctx.fall_back_to_input(run.leading()[1])
    # typed, so the match on what they yield below is checked exhaustive
    # unbounded: the record bounds the workstreams whose agents work (`RunRecord.free_agents`)
    workstreams: Group[WorkstreamDone] = ctx.group()
    planning: Group[PlanOutcome] = ctx.group(limit=1)
    measuring: Group[Baseline] = ctx.group(limit=1)
    built = BuildsOn()
    async with workstreams, planning, measuring:
        resume(ctx, run, workstreams, measuring)
        while not run.over(
            planning=not planning.free,
            measuring=not measuring.free,
            stopping=ctx.stop_requested,
        ):
            for key, wait in run.resumes():  # before new work takes the free slots
                ctx.latch(RESUMES.of(f"{key}.{wait}")).set(Granted())
            for start in run.ready_to_start(run.free_agents()):
                built.started(ctx, start, workstreams.start(start.key, workstream, start))
            for key, verdict in run.verdicts(stopping=ctx.stop_requested):
                ctx.latch(VERDICTS.of(key)).set(verdict)
            # an input with nothing to measure has its result in at once: read it before planning
            unread = run.baseline is None and measuring.free
            if planning.free and not unread and run.may_plan(stopping=ctx.stop_requested):
                planning.start(run.next_call(), plan, run)
            reports = ctx.channel(REPORTS)
            ctx.checkpoint(DynamicResume(config=run.config, record=run.state()))
            fold(ctx, run, await ctx.first(measuring, planning, workstreams, reports, ctx.stopped))
            ctx.publish(run.snapshot())
        # the record is final: a restart while the winner is adopted resumes with all of it
        ctx.checkpoint(DynamicResume(config=run.config, record=run.state()))
    return run.ending()


def resume(
    ctx: Flow, run: RunRecord, workstreams: Group[WorkstreamDone], measuring: Group[Baseline]
) -> None:
    """Start what the record has in flight: on a fresh run, only the input's measurement.

    After a code change, a measured input is handed to the workstreams that wait for it, and
    one still being measured resumes its measurement; every running workstream resumes from its
    own checkpoint, and gets the verdict already decided for its submission again. One that
    waited in a suspended turn is held until it has an agent slot; the others go on at once.
    """
    if run.baseline is None:
        measuring.start(INPUT_KEY, measure_the_input, InputPending())
    else:
        ctx.latch(INPUT_MEASURED).set(run.baseline)
    for start in run.running.values():
        workstreams.start(start.key, workstream, start)
        if start.key not in run.resuming:
            ctx.latch(RESUMES.of(f"{start.key}.0")).set(Granted())
    for key, verdict in run.decided_verdicts():
        ctx.latch(VERDICTS.of(key)).set(verdict)


type Event = Baseline | PlanOutcome | WorkstreamDone | Report | TaskStopped | StopRequested
"""What the root awaits: a result of one of its groups, a workstream's report, or a stop."""


def fold(ctx: Flow, run: RunRecord, event: Event) -> None:
    """Fold what the root awaited into the record, and hand on what follows from it."""
    match event:
        case Measured() | Unmeasurable() | NotConfigured() as baseline:
            run.measured(baseline)
            ctx.fall_back_to_input(run.leading()[1])  # the input's reading is known now
            ctx.latch(INPUT_MEASURED).set(baseline)  # the workstreams waiting for it go on
        case Scheduled() | NoPlan() | Outlined() as outcome:
            run.planned(outcome)
        case WorkstreamDone() as done:
            run.finished(done)
            ctx.choose(*run.leading())
        case Submission() | Released() | InputOverdue() | Suspended() | Resuming() as report:
            fold_report(run, report)
        case TaskStopped(key=key):
            run.stopped_before_start(key)
        case StopRequested():
            pass  # the groups have stopped their tasks
        case unexpected:
            assert_never(unexpected)


def fold_report(run: RunRecord, report: Report) -> None:
    """Fold a workstream's report into the record."""
    match report:
        case Submission() as submission:
            run.submit(submission)
        case Released(key=key):
            run.released(key)
        case InputOverdue() as overdue:
            run.overdue(overdue)
        case Suspended() as suspended:
            run.suspend(suspended)
        case Resuming() as resuming:
            run.resume(resuming)
        case unexpected:
            assert_never(unexpected)
