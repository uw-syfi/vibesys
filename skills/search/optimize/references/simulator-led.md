# Simulator-led mode

Use this mode when one measurement costs far more than one simulation. A full check and measure
of a serving engine on an accelerator can take hours, while a simulator answers in minutes on a
CPU. In this mode the simulator plans the work and cheap evidence accepts each plan. A full check
and measure is spent only on an integration. The loop of the skill otherwise stays as written:
Start, Rules, Finish, Reporting, the harness file, and the briefs all hold. This page replaces
steps 1 to 4 of the Loop.

The mode needs a domain skill that provides a simulator with calibration records and design
studies: for a serving task, the simulator skill named in the task's domain skills. Such a skill
keeps its records in a records directory outside the repository: a **calibration record** (the
factors fitted to a measured head, with the residual before and after the fit) and a **design
study** (a sweep of the system's design axes, with a cumulative ladder of changes from today's
engine to the best design found). Plans that it backs carry a **frozen prediction**, written
before the change exists. If Start step 3 finds no such skill, or the skill's setup check fails,
say so in your first report and run the default loop. Never run this mode without a simulator.

A simulator that is installed is not yet one that can lead. Before the first planner, have a
planner or profiler run the domain skill's deployment step for the task (its model, precision,
hardware and workload) and say whether the simulator models it: a match, or a proxy the domain
skill accepts, and a simulation of the task's workload that runs to the end. When it does not (a
model or a hardware the simulator has no preset for, or a preset that refuses the workload for
missing kernel timings), the simulator cannot lead yet. Say so in your first report, and run
[Before the simulator can lead](#before-the-simulator-can-lead).

## The design target and the roadmap

The **roadmap** is the ordered list of plans that lead from today's head to the design target.
The plan store holds each plan's kind and dependencies (`plan create|edit --kind --depends-on`),
and `plan list --ready` says which plans can start. The roadmap adds two things the store does not
hold: the order, and the **design target**. The target is the best design that the newest design
study found, with its simulated value at each point the task scores.

Keep the roadmap as numbered versions. Each version is a file
`<parent>/<name>.roadmap/<task>/roadmap-NNN.md`, beside the worktrees directory that
`vibesys:state` describes. Number the files from 001. Never edit a version: a revision is the next
number. The directory is outside the repository, so writing it is not editing the repository. It
outlives the run, and a resume starts from the newest version.

```
Roadmap v<N>      Date: <YYYY-MM-DD>      Supersedes: v<N-1> | none
Why: <calibration <id> | timings of plan <n> (job <id>) | integration plan <I> measured (job <id>)
  | operator: "<the steer, verbatim>" | study <id>>
Calibration: <the calibration record's id, or uncalibrated>
Design target: study <id>; <metric> <simulated value> at <each scored point>, by <the design's
  changes, one line>; head measured <value> (job <id>)
Budget: <rounds or time left, and who set it: the run's limits or the operator>
Order:
  1. plan <n> (<kind>) predicts <+x%> under <calibration id>; <ready | after plan <m>>
  2. ...
Waiting: plan <n> (<kind>) predicts <x%>: <why it does not rank yet>
Parked: plan <n> predicts <+x%>, under the bar
Changes from v<N-1>: <each move or new item in one line, or "none: the order held">
```

**Start.** Once the simulator can lead (above), and before planners propose single ideas,
dispatch one planner with the Plan brief and the nudge `design study`, and name the records directory in its brief. That planner runs the domain
skill's design study and proposes one plan for each step of the study's ladder, each with its kind,
its dependencies and its frozen prediction for that step alone. From those plans, write roadmap
v001. Further planners fill gaps in the roadmap: give them the nudge `roadmap: <the step or the
kernel to plan>`.

**The order rule.** Rank the plans whose prediction under the newest calibration exceeds the bar,
largest predicted gain first. The bar is the task's `min_gain` when it declares one, and otherwise
the noise bar of [integration.md](integration.md#when-to-integrate). Dependencies decide when a
plan can start, not where it ranks: a plan that ranks first may still wait for the plan it depends
on. A plan whose prediction is not over the bar does not rank:

- A **layout** plan that loses in the simulator is listed under Waiting, not parked. A different
  parallel layout often wins only after enough kernels improve, so it is predicted again each
  time new kernel timings arrive.
- Any other plan under the bar is parked, with its prediction in the comment.

**When to revise.** Write a new version after every calibration, even when the order holds (then
"Changes: none"). Also write one when a plan's microbenchmark timings arrive, when an integration
is measured, when a study is recorded, and when the operator steers. Each time, predict every open,
waiting or parked plan again under the newest calibration, with every timing imported so far. Then
apply the order rule. On every plan that moved, entered or left the order, comment
`roadmap v<N> (<date>): <the move and why>`, so that `plan view` holds the plan's own history. A
calibration is the domain skill's to make: after each measure of a new head, ask a profiler to fit
one with the domain skill's calibration procedure, and name the records directory in its brief.

## Before the simulator can lead

Adding a model or a hardware to a simulator is the domain skill's setup, done before the run with
the user's approval: a model description, a measured timing for every kernel at every shape the
workload reaches, and a check against measured numbers. It takes far longer than one plan.
Without an operator steering note that approves it, run the default loop for the whole run, and
name in your final report what the simulator was missing.

When the operator approves building it during the run:

- **Run the default loop meanwhile.** The simulator work is plans of their own: a `simulator`
  plan for the preset, and `probe` plans for the kernel timings it needs, with the simulator plan
  depending on them. They predict no gain.
- **Rank every other plan by measured evidence, labeled.** Keep the roadmap, with
  `Calibration: uncalibrated` and `Design target: none yet (<what the simulator still lacks>)`.
  Order the other plans by the gain their device evidence supports (a microbenchmark, a timed
  self-test, a measured head), and write the source beside each, as
  `predicts <+x%> from <job ids>, uncalibrated`. Never present that arithmetic as a simulated
  prediction.
- **Ask what the simulator can already answer.** A preset that refuses the whole workload may
  already model one phase from the timings it has (the decode step, for example). A planner may
  freeze a prediction of that phase alone, named as such, as the domain skill allows; it is never
  a prediction of the task's metric.
- **Switch when it can lead.** When the simulation of the task's workload runs to the end, start
  this mode as [Start](#the-design-target-and-the-roadmap) says: dispatch the design-study
  planner, and write the next roadmap version with
  `Why: simulator ready (plan <n>, plan <m>)`. Plans funded before the switch keep their
  evidence; the study predicts each of them again.
- **Budget it.** On a time budget, say in the first roadmap how much of it the simulator work may
  take. When that is spent, finish the run in the default loop.

## The loop

1. **Fund from the top.** Take the highest-ranked plan that `plan list --ready` lists. Mark it
   `chosen` and brief its worker with the Implement template. On a simulator-led run, the template
   has `Evidence:` (the plan's acceptance ladder from [acceptance-ladder.md](acceptance-ladder.md))
   and `Device jobs:` (each job the worker may submit, with its question and prediction).
2. **Parallel where scopes are disjoint.** Kernel, probe and simulator plans that touch disjoint
   files run at the same time, up to `--fanout`. A layout plan, a system plan whose scope overlaps
   another, and every integration run one at a time. Kernel plans may go on beside such a plan.
   Brief their workers to build on the plan's commit once it is accepted.
3. **Accept on the cheapest evidence that settles the plan**, rung by rung, as
   [acceptance-ladder.md](acceptance-ladder.md) says for the plan's kind. A rung that fails sends
   the plan back to its worker, or parks it. Do not climb past the rung that settles the plan.
4. **Spend full measurements only on integrations.** Workers submit no `submit measure`, and no
   `submit check` of the task's full check. An accepted plan reaches the head only through an
   integration plan ([integration.md](integration.md)). An integration may hold one part. Its
   measure takes the task's declared `samples` (leave out `--samples`). `compare` and `promote`
   judge it by the task's declared `relative_noise` and `min_gain`. Under those numbers, the
   verdict decides, not the prediction. Compare the measured gain with the composed prediction
   on the plan. After a promotion, the new head's measure leads to a calibration, and the
   calibration leads to a roadmap version.
5. After every handoff and every finished job, check the device jobs
   ([acceptance-ladder.md](acceptance-ladder.md#device-jobs)), then revise the roadmap if one of
   the triggers above fired.

## Steering

The operator steers a running lead through the channels the run already has. This mode adds no
new channel. It says which messages it acts on, and it records each one:

- In a run that VibeSys hosts (`VIBESYS_RUN` set): the steering note in your first brief
  (`vibesys-client run start --brief`), and messages that arrive mid-run (`vibesys-client run
  steer`). The operator changes the run's hard limits with `vibesys-client run update --max-time
  | --max-cost | --fanout`, which the orchestrator enforces and tells you about.
- In any other harness: the user's messages in your session.

| The operator says | You do |
| --- | --- |
| `budget: rounds <n>` or `budget: time <duration>` | Fund new plans only while the budget lasts. A round is one pass of step 1 over the ready plans. When the budget is spent, integrate what is accepted and finish. A time budget never extends the run's own limit. |
| `roadmap: plan <n> before plan <m>`, `roadmap: hold plan <n>`, `roadmap: drop plan <n>` | Reorder, hold (list under Waiting, `operator`) or park the plan. The operator's order outranks the order rule until the operator withdraws it. |
| `pause for a study` (optionally, what to study) | Let the running jobs and workers finish, and fund nothing new. Dispatch a planner with the nudge `design study` (and what to study). When the study is recorded, revise the roadmap and resume. |
| `resume` | Resume funding from the roadmap's order. |

Write each steer you act on into the next roadmap version: put the steer, verbatim, in `Why:`,
with the date. A steer read from mail is gone after a resume or a change of lead. The roadmap
version keeps it. Say in your next report what each steer changed. Treat any other message as the
skill's Rules say: decide, record and report it.
