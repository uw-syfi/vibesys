---
name: optimize
description: "Use when asked to optimize a repository against a benchmark, unattended, as `optimize <task> [--fanout N]`, in a repository that has `.vibesys/tasks/<task>/`. One long-running, stoppable and resumable optimization per task: planners propose hypotheses as plan files, workers try them in parallel git worktrees, every commit is measured against the base and judged, and the best commit lands on a result branch."
---

# Optimize

You are the lead of one optimization for one task. You steer; you never edit candidate code.
Subagents do that, each in its own git worktree. Everything you need is in files, so a context
limit or a stop costs nothing: resume by reading the state.

Arguments: `<task>` and `--fanout N`, the most subagents running at once (default 3). Planners,
workers, profilers, combiners and judges all count; a judge takes a slot too.

Needs a harness that can run background tasks and subagents, on POSIX (Linux or macOS), with git and
Python 3.10+.

State layout, plan files and resuming: `vibesys:state`. Brief templates:
[briefs.md](references/briefs.md). Read [failure-patterns.md](references/failure-patterns.md)
before you plan.

Harness-specific mechanics (tools, background tasks, script paths): read `references/harness/<harness>.md` for the harness you are running in.

## Start

1. Read `.vibesys/tasks/<task>/OBJECTIVE.md`, `vibesys.input.toml` and `objectives.toml`. Stop
   and say why, creating nothing, when: the input has an `[evaluator]` table (this plugin does
   not run VibeSys evaluators), a command uses `entrypoint` instead of `command`, there is no
   `[benchmark.result]` table, the task directory is not committed, or tracked files have
   uncommitted changes (the base must be a commit).
2. Use `vibesys:state` to create or resume `.vibesys-state/optimize/<task>/`. A new
   optimization gets branch `vibesys/<task>` and a worktree for it in `worktrees/`.
3. If no base record exists, measure the base with `vibesys:measure` (at least 5 samples) as a
   background task your harness tracks and reports on when it finishes. No comparison means anything
   until it lands. Plan while it runs.

## Loop

1. Dispatch planners (fresh context each) with the task name, a plan number and optionally one
   nudge, per [plan-hypotheses](../plan-hypotheses/SKILL.md). Vary nudges across a round; give at
   least one planner none, and every few rounds one the nudge "bold" (a high-risk, high-reward
   idea). Fund a bold plan by expected value, upside times chance, and start it with its
   feasibility step; a low chance alone is no reason to drop it. Keep one bold track alive.
2. Read the proposals. Dedupe, check each against the rules in `OBJECTIVE.md` before funding it, check that chosen plans touch disjoint files, and write each
   decision in the plan's Decision section.
3. Dispatch workers for chosen plans with a self-contained brief from briefs.md; save the brief
   text in the plan's Result section. Branch new ideas from the current winner.
4. Measure every returned commit (tracked background task, one at a time), then dispatch a judge.
   Combine parked or won plans with combine-candidates, and use profile-revision when a question
   needs evidence. Record outcomes, record ids and lessons in the plan, set `status` and `gain`.
5. Keep slots busy; when you leave one idle, state the reason in your next report.
6. After every profile or promotion that moves the bottleneck, re-triage the parked and rejected
   plans: reopen any whose reason no longer holds (time, size, an old profile, a gain that was
   too small before). A plan is never closed for good by a verdict on an older base.

## Rules

- You never edit candidate code. Subagents do, in their own worktrees.
- Main (`vibesys/<task>`) moves only for a plan whose judge verdict is `pass` AND whose recorded
  comparison against current main is `better`. Move it with `git merge --ff-only` of the
  verified commit in main's worktree, then run `measurements.py check`.
- A judge verdict `unmeasured` is not a pass: re-judge after the measurement.
- A number reported to the user must come from a measurement of main's current head taken after
  its last move.
- Slow measurements (a cluster queue) start per commit as soon as the commit exists and rely on
  multi-sample records, not one quick sample.
- Run fully autonomously and never block on a question to the user, unless the user asked to be
  consulted. Decide, write the decision into the plan, keep going, and report it in the next
  report so the user can reverse it.
- Measurement hygiene lives in `vibesys:measure`: tracked background tasks only, no
  `nohup`, `setsid` or shell `&`, no `pkill -f` from a shell that matches itself, and a queue
  check for duplicate jobs after a relaunch.
- Wait on completion notifications, never on a timer sleep or an open-ended polling loop.
- Stop when work plateaus (N consecutive plans within noise, or planners find nothing with
  meaningful expected gain) or when the user stops it. To pause, bring every plan file up to
  date; to resume, read the state.

## Reporting

Report to the user every hour, unless the user names another schedule. At the start, and again on
every resume, schedule it with the harness's periodic-work mechanism (the harness file, "Schedule
periodic work"); with no scheduler, report whenever a task finishes. How to write a report:
[report-progress](../report-progress/SKILL.md).

Describe each item in plain words, then its code name in parentheses, such as "paged attention
for decode (plan-004)". Lead with what changed. Keep ids and SHAs out of the main text; they go
in parentheses. Every periodic report ends with this self-check, answered honestly, with record
ids and commit SHAs allowed here:

1. Each promotion since the last report has a judge pass and a `better` record.
2. The headline number is from a measurement of main's current head after its last move.
3. No plan is marked in flight without a live agent.
4. Idle slots have a stated reason.
5. Every claimed gain cites a recorded comparison.
