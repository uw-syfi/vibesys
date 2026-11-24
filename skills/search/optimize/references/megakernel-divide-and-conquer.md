# Megakernel divide-and-conquer mode

This mode is for megakernel tasks only: the candidate is one GPU kernel that runs a whole model
step (or layer) as phases inside one launch, sharing its resources, and a per-phase timeline can
time each phase. Do not select it for other tasks, even ones with a fused or persistent kernel;
it has been designed for and tried on megakernels alone. Once a working design exists, the program is divided at its phase
boundaries. Each phase gets its own worker, which works under a contract, and the lead glues the
accepted phases back into one candidate, and the cycle repeats on the new head until the budget
is spent or the user stops the run. The loop of the skill otherwise stays as written: Start, Rules,
Reporting, the harness file and the briefs all hold, and Finish is replaced by
[Budget and the end of the run](#budget-and-the-end-of-the-run). You still write no code: a
combiner makes the glue commits, and you decide what they hold.

Phases that share one launch do not add up the way separate programs do. A phase that gets faster
by taking more registers or shared memory slows every other phase through occupancy. A phase that
finishes sooner can leave its wait at the next barrier. And the largest gains often lie across
phases: overlap, fusion, a barrier removed. So the mode divides only a settled design, holds each
phase to a resource budget, accepts a phase on its window in the whole program and not in
isolation, and keeps one line of work across phases alive.

The mode needs a way to time phases inside the whole program: the timeline domain skill for the
task's backend (`kernel-timeline-analysis` on CUDA), or a timeline tool the task itself provides.
If neither exists, the first plan is a `probe` that builds one, and the mode starts when it is
accepted. Without a timeline, run the default loop.

## The stages

1. **Bootstrap.** Run the default loop until the head is ready to divide (below). A base that
   fails its check is bootstrapped first ([from-scratch.md](from-scratch.md)). Ask the bootstrap
   worker for a correct design with the phase structure the task's speed-of-light bound needs, and
   not for the simplest one: a phase structure chosen for correctness alone is then frozen
   by the division.
2. **Map** the head's phases into a phase map (below).
3. **Split**, only when the target phases share code in a way that would make workers' scopes
   overlap.
4. **Conquer:** one worker per target phase, in parallel.
5. **Glue:** one integration of the accepted phases, measured once.
6. **Re-map** the new head and go back to 3 or 4. Stages 2 to 6 are one **cycle**. Cycles repeat
   until the budget is spent or the user stops the run; a cycle that finds nothing escalates
   ([When a cycle finds nothing](#when-a-cycle-finds-nothing)) instead of ending the run.

These stages replace steps 1 to 4 of the Loop. In Bootstrap, the default Loop runs as written.

## Ready to divide

Divide when all of these hold, and write the reason into phase map v001:

- The head passes its check and has a measure result.
- Two timeline runs of the head, as separate jobs, name the same phases in the same order, and
  each phase's window agrees within the spread between the two runs, which you write down.
- The design is settled: no plan that is open or chosen would change which phases exist, their
  order, how blocks are assigned to roles, or how phases synchronize. Such a plan finishes first,
  or is parked with a comment that says the division was chosen over it.

When the head cannot get ready (the timelines disagree from run to run, or the design keeps
changing), stay in the default loop, and say why in your next report.

## The phase map

Keep the phase map as numbered versions, as the roadmap is kept in simulator-led mode. Each
version is a file `<parent>/<name>.phases/<task>/phase-map-NNN.md`, beside the worktrees directory
that `vibesys:state` describes. Number the files from 001, and never edit a version: a revision
is the next number. Each version describes one head.

Dispatch a profiler with the Profile brief to map the head. Its question is: the phase map of
`<head SHA>`. It instruments the head on an instrumentation branch, as the timeline domain skill
says, runs the instrumented build in at least two `vibesys-cli submit command` jobs with
`--outputs` (`vibesys:measure`), and hands back one row per phase. You write the version:

```
Phase map v<N>      Date: <YYYY-MM-DD>      Supersedes: v<N-1> | none
Head: <SHA>, measured <value> (job <id>)
Why: <ready to divide | head moved: integration plan <I> | operator: "<the steer, verbatim>">
Timeline: instrumentation branch <branch> at <SHA>; jobs <ids>; span <time>; spread <x%>
Launch totals: <registers per thread, spills, shared memory per block, blocks or grid shape,
  any other per-launch resource>; occupancy <value>
| Phase | Code | In -> out (layout) | Budget | Starts on / ends with | Window | Floor | Gap |
| <name> | <files, functions> | <tensors and layouts> | <what it may use> | <sync> | <t> | <t> | <t> |
Boundaries: <a> / <b>: <the hand-off or sync between them; the wait it shows>
Targets: 1. <phase>, gap <t> (<share> of the span); 2. ...
Cross-phase track: plan <n> (<the idea, one line>) | none
```

- **Window** is the time a phase adds to the critical path: the latest end of the phase over all
  blocks, minus the latest end of anything before it. The windows of all phases sum to the span.
  The timeline domain skill says how to read it.
- **Floor** is the phase's speed-of-light time, from the bytes and operations it must move and the
  hardware's measured peaks, as a planner bounds a part. **Gap** is the window minus the floor.
- **Budget** is the share of each launch-wide resource the phase may use, set so the launch totals
  do not grow. A phase may use less. A phase may use more only when the plan says so and the plan
  is a boundary or cross-phase plan.
- **Targets** are the phases whose gap, as a share of the span, is above the bar, largest first.
  The bar is the task's `min_gain`, or otherwise the noise bar of
  [integration.md](integration.md#when-to-integrate). A phase under the bar is not a target.

## Split

A worker's scope must not overlap another worker's. When two target phases share a file, a
function or a block of code, fund one `kernel` plan first that moves each phase into its own
unit, still compiled into the same program, with no change in what it computes. Its claim is that
the split changes the span by no more than the timeline spread. Accept it on a passing
`vibesys-cli submit check` and a timeline whose windows agree with the map's. A neutral change
cannot be promoted (`within_noise`), so the split stays `chosen` with its `accepted:` comment.
It becomes the first part of the next integration, and every phase worker builds on its accepted
commit.

## Conquer

For each target, dispatch a planner with the nudge `phase: <name>`, and name the phase map file
in the brief. It writes one `kernel` plan for that phase, bounded by the phase's gap as a share of
the span, and depending on the split plan when there is one. A phase can get a second plan when
its first plan is parked. While the split plan is only accepted, `plan list --ready` does not list
the phase plans that depend on it. Start them anyway, built on its accepted commit.

A worker per phase plan, all in parallel up to `--fanout`. Each gets the Implement brief with
these lines filled ([briefs.md](briefs.md)):

- `Scope:` the phase's code from its row, and nothing else.
- `Phase contract:` the row, verbatim: inputs and outputs with their layouts, the budget, and the
  sync it starts on and ends with. The phase must still compute exactly what it hands on.
  A change that needs to break the contract is not made: the worker hands it back as a boundary
  proposal.
- `Phase evidence:` the ladder below. The worker submits no measure.

The ladder, rung by rung:

1. **Check.** `vibesys-cli submit check <sha> --wait --plan <n>` passes on a full commit.
2. **Budget.** The compiler's resource report of the candidate shows launch totals no higher than
   the map's, except where the contract allows it.
3. **Window.** Timeline jobs of the parent and the candidate, with the same instrumentation, the
   same `--gpus` and at least three runs each, or one paired A/B job when the task provides one.
   The phase's window falls by more than the spread between the parent's own runs, and no other
   phase's window grows by more than that spread.

Accept the plan when the last rung passes, with the comment
`accepted: <SHA> built on <parent>; evidence: <job ids for each rung>; cuts <phase> by <t> of a
<t> span`. A rung that fails sends the plan back to its worker. When the window falls but another
phase's window grows by as much, the cost moved instead of going away: park the plan, or turn it
into a boundary plan.

**Boundary plans.** Fusing two phases, replacing the sync between them, or moving one phase's
loads into the one before it is one plan with one worker that owns both phases. Its contract is
the outer edges of the pair, and its window is the sum of the two windows. Fund a boundary plan
when the map shows a wait at a boundary, or when a phase worker hands back a boundary proposal.

**The cross-phase track.** A change that alters which phases exist, their order, how blocks are
assigned, or how they synchronize cannot be split. Keep at most one such plan in flight in its own
workstream, as the bold track, with the default Implement brief. It runs beside the phase workers
and does not wait for them. It is measured on its own. If it wins, it ends the map: the phase
plans built on the old head are combined onto the new head or parked, and you re-map.

Keep one slot for the cross-phase track whenever one is open. Leave every other slot to phase or
boundary workers.

## Glue

Integrate the accepted phase plans as [integration.md](integration.md) says, with the split plan,
when there is one, as the first part, then the phase and boundary parts, the largest proven window
cut first. Compose the prediction by time removed: phases on the critical path give back their
window cuts, so the parts leave `span - (c1 + c2 + ...)`. Write it on the integration plan
before the measure.

The parts touch disjoint code, so applying them is mechanical. A merge goes wrong in the launch
totals and between phases. Add a `Glue checks:` line to the Integrate brief:

- the launch totals of the merged build, against the map's: registers per thread, spills, shared
  memory per block and occupancy;
- one timeline of the merged candidate, with each phase's window beside what its part showed;
- the composition loss: the sum of the parts' window cuts minus the span the merged timeline
  shows removed.

From the handoff, write the composition loss on the integration plan. A part whose cut is gone in
the merge but present on its own interacts with another part: comment both, and send it back to
its worker with the merged timeline, as for a failed check. A new wait at a boundary in the merged
timeline is a boundary plan. The measure and `compare` still decide the promotion. The timeline
only says where to look.

## Re-map

After every promotion, write the next phase map version from a timeline of the new head. Then
rank the targets again. A phase plan that is chosen and was built on the old head goes on: a
combine moves it onto the new head before its integration, as the Rules say.

Number the cycles from 1, and put the cycle number and the budget spent so far in each phase map
version's `Why:` line, so a resume knows where it stands.

## When a cycle finds nothing

A cycle finds nothing when no phase has a gap above the bar, or when its integration is refused
(`within_noise` or `worse`) or never forms because no phase plan was accepted. That does not end
the run while budget is left. Escalate, one step per cycle, and go back to the first step as soon
as a cycle promotes something:

1. **Boundaries.** Fund boundary plans for the largest waits and skews the map shows between
   phases, and re-triage the parked phase plans against the new map.
2. **Cross-phase redesign.** Dispatch planners with the nudges `bold` and `redesign from first
   principles` over the whole kernel, and fund the best cross-phase plan with more slots than one.
   The time left between phases (waits, skew, units left idle) is this kind of work. A design that
   wins ends the map, and the next cycle re-maps it.
3. **A finer bar.** Lower the bar to what the timeline can tell from noise, the spread between
   repeats of the head, and take more samples where a verdict was close. Phases that were under
   the old bar become targets.
4. **A finer map.** Ask the profiler to split the largest phase into sub-phases with more probes,
   and target those.

When all four have run in a row without a promotion, keep cycling through them with fresh
planners, and say in each progress report that the run is past its easy gains, with the gap that
is left between the head and the sum of the floors.

## Budget and the end of the run

The run's budget comes from `--budget time <duration>`, `--budget cycles <n>`, or the operator's
steering note (`budget: time <duration>`, `budget: cycles <n>`), and the newest one wins. Write it,
with the run's start time, into phase map v001 and into every later version. Limits the harness
enforces, such as a cost or token cap, a session time limit or `vibesys-client run update
--max-time | --max-cost` in a run that VibeSys hosts, also bind: they can stop your session at any
point, so keep the state current, as the Rules say, and a later session resumes from the newest
phase map. With no budget given, the run goes on until the user stops it.

The Finish conditions of the skill (bounded gains, no progress) do not end this mode; they are
the signal to escalate. The run ends only when:

- the budget is spent: check the time against it before you fund each new plan, and count cycles
  at each re-map. Fund nothing new once it is spent, let the running workers hand back, integrate
  what is accepted, and give the final report;
- the user or the operator stops it: do the same, at once;
- an external factor you cannot fix stops all progress, as the skill's Reporting section says.

In each case give the final report as `vibesys:report-progress` says,
with the cycles run, the budget spent, and the newest phase map's gaps.

## Failure patterns

- **Dividing too early.** Phase workers polish phases of a design that should change. The design
  must be settled before the first map, and the cross-phase track stays open.
- **Local gain, global loss.** A phase gets faster by taking resources the whole launch shares.
  The budget rung catches it.
- **Moving the cost.** A phase's window shrinks because its wait moved to the next barrier. The
  window rung compares every phase.
- **Trusting the probes.** Probes perturb what they time. A timeline accepts a part, but only the
  measure promotes it.
