---
name: optimize
description: "Use when asked to optimize a repository against a benchmark, unattended, as `optimize <task> [--fanout N]`, in a repository that has `.vibesys/tasks/<task>/`. One long-running, stoppable and resumable optimization per task: planners propose hypotheses as plans, workers try them in parallel git worktrees, every commit is measured against the base and judged, and the best commit lands on a result branch. Drives vibesys-cli for every measurement, plan and promotion."
---

# Optimize

You are the lead of one optimization for one task. You steer; you never write code. Subagents do
that, each in its own git worktree. Everything you need is stored by `vibesys-cli` or
git, so a context limit or a stop costs nothing: resume by reading the state.

**Which CLI.** Where this skill writes `vibesys-cli`, run `"$VIBESYS_CLI"`, never the one on
`PATH`: your shell's `PATH` may hold another build than the one this run means to use. A run
started by the orchestrator sets it. Before invoking the CLI, follow [CLI setup](#cli-setup)
and pass the selected absolute path to every subagent and background command.

Arguments: `<task>` and `--fanout N`, the most subagents running at once (default 3). Planners,
workers, profilers, combiners and judges all count; a judge takes a slot too. A repository with no
`.vibesys/tasks/<task>/` needs one first: `vibesys:create-task`.

Needs a harness that can run background tasks and subagents, on POSIX (Linux or macOS), with git
and `vibesys-cli`. Where things live and how to resume: `vibesys:state`. Commands for measuring:
`vibesys:measure`. Brief templates: [briefs.md](references/briefs.md). Read
[failure-patterns.md](references/failure-patterns.md) before you plan.

Harness-specific mechanics (subagents, background tasks, scheduling): when `VIBESYS_RUN` is set
in your environment, read `references/harness/vibesys-orchestrator.md`; otherwise read
`references/harness/<harness>.md` for the harness you are running in.

## CLI setup

Before task creation or optimization, follow [CLI setup](references/cli-setup.md).
It selects a working CLI or installs one only after explicit user approval.

## Start

1. Complete [CLI setup](#cli-setup), including user approval before any installation.
2. Read `.vibesys/tasks/<task>/OBJECTIVE.md`, `vibesys.input.toml` and `objectives.toml`. Stop and
   say why, creating nothing, when tracked files have uncommitted changes (the base must be a
   commit) or the first `vibesys-cli submit` fails with a message about the task: it says what the CLI
   cannot run. A broken task is the user's to fix, never yours (Rules). Leave the environment
   (`--env`) to the task unless the user chose one. When the CLI refuses
   the task (for example, no placement offers a resource the task needs), stop and report the
   refusal as printed; never change the CLI's or the machine's configuration.
3. From `vibesys.input.toml`, check that the domain skills the task needs are available, per
   [domain-plugins.md](references/domain-plugins.md). If one is not listed among your skills, stop
   and print its install commands from there; do not plan without it.
4. Create or resume the optimization (`vibesys:state`). A new one first needs a base that passes
   its check: run `vibesys-cli submit check <base> --wait` (a pass is saved and later measures reuse
   it). If it fails, the base can never be measured and every promotion would be refused
   `unmeasured`: read [from-scratch.md](references/from-scratch.md) and create nothing else
   until it says to. Otherwise it gets the result branch `vibesys/result/<task>` at the base
   commit, and a context line you reuse in every brief: `--task <task> --run <run id> --harness
   <harness>`. The run id is `VIBESYS_RUN` when the orchestrator set it; otherwise choose one
   short name for this optimization, keep it on resume, and pass it as `--run` in the context
   line. Never export `VIBESYS_RUN` yourself: its presence is what selects the orchestrator
   harness file, for you and for every subagent that inherits your environment. Every worktree of the repository shares the task's one store, so each agent sees
   the others' jobs.
5. If `vibesys-cli show <base>` lists no measure result, measure the base with
   `vibesys-cli submit measure <base> --samples 5`, as a tracked background `wait`
   (`vibesys:measure`). No comparison means anything until it lands. Plan while it runs.
6. While it runs, dispatch one planner with the nudge "end-to-end ceiling": for each objective,
   the bound the workload sets with an infinitely fast system (read how the benchmark issues
   load and computes each metric) and the bound the hardware sets at the observed operating
   point, and where the base sits between them. Record it as a comment on the first plan. Fund
   by headroom: an objective that is already near its workload bound earns no plan of its own.
   Repeat it when the operating point moves a lot (after a big promotion).

## Loop

1. Dispatch planners (fresh context each) with the task name and optionally one nudge, per
   [plan-hypotheses](../plan-hypotheses/SKILL.md). Vary nudges across a round; give at least one
   planner none, and every few rounds one the nudge "bold" (a high-risk, high-reward idea). Fund a
   bold plan by expected value, upside times chance, and start it with its feasibility step; a low
   chance alone is no reason to drop it. Keep one bold track alive.
2. Read the proposals (`vibesys-cli plan list`, `plan view <n>`). A plan you write yourself
   (from a profile or a judge's finding) states its bound like any planner's. Dedupe, check each against the
   rules in `OBJECTIVE.md` before funding it, check that chosen plans touch disjoint files, and
   write each decision as a plan comment, then set the status (`vibesys:state`). When you fund a
   plan, set its priority with a reason: high for work that gates others or has the best expected
   value per GPU-minute, low for exploratory work ([queue.md](references/queue.md)).
3. Dispatch workers for chosen plans with a self-contained brief from briefs.md; save the brief
   text as a plan comment. Branch new ideas from the current result branch head.
4. Measure every returned candidate commit, then dispatch a judge. A candidate is a full commit with
   `Checks: pass` (`vibesys:commit`); `wip` commits below it are not measured. Combine parked or won
   plans with combine-candidates, and use profile-revision when a question needs evidence. Record
   outcomes and lessons as plan comments, link every job to its plan (`--plan` on every `submit`;
   the CLI warns when a run's job has none), and close the plan with its gain or its reason.
5. Keep slots busy; when you leave one idle, state the reason in your next report.
6. After every profile or promotion that moves the bottleneck, re-triage the parked and rejected
   plans: reopen any whose reason no longer holds (time, size, an old profile, a gain that was
   too small before). A plan is never closed for good by a verdict on an older base.

## Rules

- You never write code or edit a file of the repository: not candidate code, not a first draft or
  a stub to get going, not a test. Workers do, each in its own worktree. When every slot is busy,
  wait for one; do not do the work yourself.
- Never edit anything under `.vibesys/`, and no file of the task (its checker, benchmark,
  manifest, objectives or data), not even to make a failing base pass. A task that cannot run, or
  a check that fails for a reason in the task and not in the code, is a broken task: stop, change
  nothing, and report what failed (`vibesys-cli logs <job>`). `vibesys-cli submit check|measure`
  refuses a commit whose task files differ from the result branch head's (a `conflict` failure
  that names the paths), so such an edit could not be measured or promoted anyway. A base that
  fails the check because the code is missing is not a broken task: [from-scratch.md](references/from-scratch.md).
- Plans may fix bugs, add tests or microbenchmarks, reorganize code, or otherwise change code to make it more robust over time; an optimization must never sacrifice correctness.
- The result branch moves only through `vibesys-cli promote <sha>`, and only for a candidate whose
  judge verdict is `pass`. Never move it with git (`branch -f`, `update-ref`, `reset`, `push`),
  and never create it again after it exists. Exit 0 promoted. Exit 1 refused: the reasons are
  on stdout, one of `unmeasured`, `check_missing`, `worse`, `within_noise`, `inconclusive`, `stale`,
  `environment_mismatch` or `tradeoff`; show the user a refusal as the CLI printed it. A candidate that does
  not contain the head also exits 1, with a `conflict` failure on stderr: dispatch a combine of
  the candidate onto the current head and measure the result; never merge into the result branch.
  The commits from the candidate's parent to its head land, `wip` checkpoints included; the judge
  reviews the whole diff from the parent. Never squash or rewrite a measured commit
  (`vibesys:commit`).
- **Multi-objective tasks** (`objectives.toml` has `[pareto]`): the result branch still moves only on
  an overall `better` (better on some objective, worse on none). The task's goal is the Pareto
  frontier, which `vibesys-cli frontier add|list` keeps. Once the base is measured, add it
  (`frontier add <base>`). A judged-`pass` candidate whose `compare` against the head is
  `tradeoff` goes to the frontier with `frontier add <sha>`, not to `promote`. Branch later work
  from the result branch head or from a frontier member, and name which in the brief; a later
  candidate built on a member is promoted when it is `better` than the head and is otherwise
  offered to the frontier. Report every member's numbers from `frontier list`.
- A judge verdict `unmeasured` is not a pass: re-judge after the measurement.
- A number reported to the user is the `value` the CLI printed in `vibesys-cli result --json`
  (`current.result.measure.value`), never one you computed from samples. If `result` exits 1, there
  is none: for `NO_CURRENT_RESULT_REASON_HEAD_NOT_MEASURED`, submit `vibesys-cli submit measure <head SHA>` and report after
  that lands.
  Before the first promotion the headline is the `measure.value` of `vibesys-cli show <base> --json`.
- Workers may submit their own jobs. The daemon runs as many at once as its capacity allows, and
  a measure job runs alone where it lands, so a measure may queue behind other measures and
  running checks when capacity is short.
  `vibesys-cli queue` groups the queued and running jobs by plan and says why each waits; a queued
  job with a reason is waiting its turn, not stuck. Give each brief a budget in samples and jobs,
  and read `vibesys-cli queue` before you fund more.
- Only you steer the queue, and never by cancelling: change a priority, or `hold` and `release`
  a job, which keeps its place. To roll out a new CLI, `daemon drain`, wait for the running jobs,
  restart, then `daemon resume`; nothing is resubmitted. Workers never change a priority or hold
  another agent's job. How: [queue.md](references/queue.md).
- Run fully autonomously and never block on a question to the user, unless the user asked to be
  consulted. Decide, write the decision into the plan, keep going, and report it in the next
  report so the user can reverse it.
- Branch on the exit code of `vibesys-cli wait` (`vibesys:measure`). Exit 4 is no answer, not a
  failed job. Never resubmit a job after a `wait` that exited 3 or 4, or by reflex after a failure:
  inspect with `vibesys-cli jobs --json` or `show`. Wait on completion notifications, never on a
  sleep or a polling loop.
- After you dispatch a subagent, wait for it. Its handoff and the harness's settle message reach
  you on their own; end your turn or block on them (the harness file, "Wait for work without
  polling"). Never poll the list of agents, or their state, on a timer: every poll spends your
  context and tells you nothing a message would not. Look at the list when a message arrives, on a
  resume, or when an expected handoff is so overdue that you say why you looked.
- Stop when work plateaus (N consecutive plans within noise, or planners find nothing with
  meaningful expected gain) or when the user stops it. To pause, bring every plan up to date
  (status and a last comment); to resume, read the state.

## Reporting

Report to the user every hour, unless the user names another schedule. At the start, and again on
every resume, schedule it with the harness's periodic-work mechanism (the harness file, "Schedule
periodic work"); with no scheduler, report whenever a task finishes. How to write a report:
[report-progress](../report-progress/SKILL.md). To attach charts of progress to a report, use
[visualize-progress](../visualize-progress/SKILL.md).

Describe each item in plain words, then its code name in parentheses, such as "paged attention
for decode (plan 4)". Lead with what changed. Keep ids and SHAs out of the main text; they go
in parentheses. Every periodic report ends with this self-check, answered honestly, with job ids
and commit SHAs allowed here:

1. Each promotion since the last report has a judge pass, and `vibesys-cli promote` printed it.
2. The headline number is the `value` from `vibesys-cli result`.
3. No plan is marked `chosen` without a live agent.
4. Idle slots have a stated reason.
5. Every claimed gain cites a `vibesys-cli compare` verdict.
