---
name: optimize
description: "Use when asked to optimize a repository against a benchmark, unattended, as `optimize <task> [--fanout N]`, in a repository that has `.vibesys/tasks/<task>/`. One long-running, stoppable and resumable optimization per task: planners propose hypotheses as plans, workers try them in parallel git worktrees, every commit is measured against the base and judged, and the best commit lands on a result branch. Drives vibesys-cli for every measurement, plan and promotion."
---

# Optimize

You are the lead of one optimization for one task. You steer; you never edit candidate code.
Subagents do that, each in its own git worktree. Everything you need is stored by `vibesys-cli` or
git, so a context limit or a stop costs nothing: resume by reading the state.

Arguments: `<task>` and `--fanout N`, the most subagents running at once (default 3). Planners,
workers, profilers, combiners and judges all count; a judge takes a slot too. A repository with no
`.vibesys/tasks/<task>/` needs one first: `vibesys:create-task`.

Needs a harness that can run background tasks and subagents, on POSIX (Linux or macOS), with git
and `vibesys-cli`. Where things live and how to resume: `vibesys:state`. Commands for measuring:
`vibesys:measure`. Brief templates: [briefs.md](references/briefs.md). Read
[failure-patterns.md](references/failure-patterns.md) before you plan.

Harness-specific mechanics (subagents, background tasks, scheduling): read
`references/harness/<harness>.md` for the harness you are running in.

## Start

1. Run `vibesys-cli --version`. If it fails, stop and tell the user to install the CLI with
   `curl -fsSL https://raw.githubusercontent.com/uw-syfi/vibesys/main/cli/install.sh | sh`
   (it installs to `~/.local/bin`, which must be on `PATH`), then run you again. It is the only
   thing this plugin needs besides git. `vibesys-cli upgrade` updates it.
2. Read `.vibesys/tasks/<task>/OBJECTIVE.md`, `vibesys.input.toml` and `objectives.toml`. Stop and
   say why, creating nothing, when tracked files have uncommitted changes (the base must be a
   commit) or the first `vibesys-cli submit` fails with a message about the task: it says what the CLI
   cannot run. Leave the environment (`--env`) to the task unless the user chose one.
3. Create or resume the optimization (`vibesys:state`). A new one gets the result branch
   `vibesys/result/<task>` at the base commit, and a context line you reuse in every brief:
   `--task <task> --run <run id> --harness <harness>`. Every worktree of the repository shares the
   task's one store, so each agent sees the others' jobs.
4. If `vibesys-cli show <base>` lists no measure result, measure the base with
   `vibesys-cli submit measure <base> --samples 5`, as a tracked background `wait`
   (`vibesys:measure`). No comparison means anything until it lands. Plan while it runs.

## Loop

1. Dispatch planners (fresh context each) with the task name and optionally one nudge, per
   [plan-hypotheses](../plan-hypotheses/SKILL.md). Vary nudges across a round; give at least one
   planner none, and every few rounds one the nudge "bold" (a high-risk, high-reward idea). Fund a
   bold plan by expected value, upside times chance, and start it with its feasibility step; a low
   chance alone is no reason to drop it. Keep one bold track alive.
2. Read the proposals (`vibesys-cli plan list`, `plan view <n>`). Dedupe, check each against the
   rules in `OBJECTIVE.md` before funding it, check that chosen plans touch disjoint files, and
   write each decision as a plan comment, then set the status (`vibesys:state`).
3. Dispatch workers for chosen plans with a self-contained brief from briefs.md; save the brief
   text as a plan comment. Branch new ideas from the current result branch head.
4. Measure every returned candidate commit, then dispatch a judge. A candidate is a full commit with
   `Checks: pass` (`vibesys:commit`); `wip` commits below it are not measured. Combine parked or won
   plans with combine-candidates, and use profile-revision when a question needs evidence. Record
   outcomes and lessons as plan comments, link every job to its plan (`--plan`), and close the plan
   with its gain or its reason.
5. Keep slots busy; when you leave one idle, state the reason in your next report.
6. After every profile or promotion that moves the bottleneck, re-triage the parked and rejected
   plans: reopen any whose reason no longer holds (time, size, an old profile, a gain that was
   too small before). A plan is never closed for good by a verdict on an older base.

## Rules

- You never edit candidate code. Subagents do, in their own worktrees.
- Plans may fix bugs, add tests or microbenchmarks, reorganize code, or otherwise change code to make it more robust over time; an optimization must never sacrifice correctness.
- The result branch moves only through `vibesys-cli promote <sha>`, and only for a candidate whose
  judge verdict is `pass`. Never move it with git. Exit 0 promoted. Exit 1 refused: the reasons are
  on stdout, one of `unmeasured`, `check_missing`, `worse`, `within_noise`, `inconclusive`, `stale`
  or `environment_mismatch`; show the user a refusal as the CLI printed it. A candidate that does
  not contain the head also exits 1, with a `conflict` failure on stderr: dispatch a combine of
  the candidate onto the current head and measure the result; never merge into the result branch.
  The commits from the candidate's parent to its head land, `wip` checkpoints included; the judge
  reviews the whole diff from the parent. Never squash or rewrite a measured commit
  (`vibesys:commit`).
- A judge verdict `unmeasured` is not a pass: re-judge after the measurement.
- A number reported to the user is the `value` the CLI printed in `vibesys-cli result --json`
  (`current.result.measure.value`), never one you computed from samples. If `result` exits 1, there
  is none: submit `vibesys-cli submit measure <head SHA> --no-cache` and report after that lands.
  Before the first promotion the headline is the `measure.value` of `vibesys-cli show <base> --json`.
- Workers may submit their own jobs; the CLI runs them one at a time, first come first served. Give
  each brief a budget in samples and jobs, and look at `vibesys-cli jobs` before you fund more.
- Run fully autonomously and never block on a question to the user, unless the user asked to be
  consulted. Decide, write the decision into the plan, keep going, and report it in the next
  report so the user can reverse it.
- Branch on the exit code of `vibesys-cli wait` (`vibesys:measure`). Exit 4 is no answer, not a
  failed job. Never resubmit a job after a `wait` that exited 3 or 4, or by reflex after a failure:
  inspect with `vibesys-cli jobs --json` or `show`. Wait on completion notifications, never on a
  sleep or a polling loop.
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
