---
name: optimize
description: "Use when asked to optimize a repository against a benchmark, unattended, for a fixed time, as `/vibesys:optimize <task> --time <duration> [--fanout N]`, in a repository that has `.vibesys/tasks/<task>/`. Plans competing hypotheses, has subagents try them in parallel git worktrees, measures every commit against the base, and leaves a best commit, a record of every measurement and a report."
---

# Optimize

You are the lead of a time-boxed optimization run. You steer; you never edit candidate code.
Subagents do that, each in its own git worktree. You choose what to try, check what comes back,
and keep every slot busy until the deadline. Everything you need to resume lives in files, so a
context limit costs you nothing: after any compaction, reread `run.json`, `ledger.md`,
`decisions.md` and the newest files in `notes/` before acting.

Arguments: `<task>`, `--time <duration>` (`90m`, `2h`, `1h30m`), and `--fanout N`, the most
subagents running at once (default 3).

File formats are in [run-state.md](references/run-state.md). Brief templates are in
[briefs.md](references/briefs.md). Read [failure-patterns.md](references/failure-patterns.md)
before you plan and again at each wrap-up.

## Start

1. Read `.vibesys/tasks/<task>/OBJECTIVE.md`, `vibesys.input.toml` and `objectives.toml`. Stop
   and say why, creating nothing, when: the input has an `[evaluator]` table (this plugin does
   not run VibeSys evaluators), a command uses `entrypoint` instead of `command`, there is no
   `[benchmark.result]` table, the task directory is not committed, or tracked files have
   uncommitted changes (the base must be a commit).
   Verify background Agent execution and session scheduling are available before creating state.
2. Create the run: id, directory, `run.json` with the deadline, the exclude line, the branch
   `vibesys/<run-id>` and its worktree, and `.vibesys-optimize/active`. Commands are in
   run-state.md.
3. Keep exactly one pending wakeup, for the next milestone: wrap-up first, then the deadline.
   For a normal skill invocation use `CronCreate` with `recurring: false`, and record its ID.
   Only a self-paced `/loop` uses `ScheduleWakeup`; each call replaces its pending wakeup,
   so never schedule wrap-up and deadline together. Re-arm after every wake, checking the
   actual clock before advancing phases (cron may fire early or after a busy turn).
   Use `CronList` to reconcile on resume and `CronDelete` to cancel obsolete run-owned tasks.
   If scheduling or background agents are unavailable, remove this run's `active` marker,
   cancel any wakeup already created, and explain the blocker.
4. Measure the base with `vibesys:measure`, at least 5 samples, as a tracked background Bash command with `run_in_background: true`. No comparison
   means anything until it lands. Plan while it runs.

## Loop

1. **Plan** a portfolio of distinct, falsifiable hypotheses from the objective, the code and any
   profiles. Never split one change into parallel fragments. Four kinds of work:
   - *implement*: one hypothesis, built and measured in its own worktree.
   - *profile*: answer one question that the next decision depends on, changing no code.
   - *combine*: apply several candidates' changes into one new candidate.
   - *merge*: move winners into main. Main moves only to a commit that passes the check and
     does not measure worse than main.
2. **Dispatch** each item as a background subagent with a self-contained brief from briefs.md.
   Create its worktree and branch yourself, at an exact base commit. Every implement item begins
   with the smallest step that yields a measurement. Allocate measurement slots as described
   in `vibesys:measure`, so benchmarks and load profiles never overlap. Keep at most the fanout cap running, and
   fill every free slot with work that does not conflict with what runs.
3. **Write a note** in `notes/<work-id>.md` as you dispatch: what you expect, and what you will
   do if it succeeds and if it fails.
4. **When a subagent finishes**: confirm its commit exists; check the handoff (was it measured,
   not just written; any shortcut); measure every `ready for measurement` commit before
   interpreting its gain; send each candidate that claims a gain to an independent
   judge (`vibesys:judge-candidate`) before treating it as a win; update the ledger and
   decisions; compare what happened with the note. Park or abandon a direction with a stated
   reason. A direction that fails to converge after two tries is parked. A step that ended
   without a candidate is narrowed, never reassigned unchanged.
5. **Refill until wrap-up**: every free slot gets new work. When a direction wins, branch new
   hypotheses from the winner: tune it, combine compatible gains, or profile the next bottleneck.
   Drop work that no longer changes the outcome and replace it; exhausted initial ideas are
   a reason to inspect the winner, not to end the search early.

Run each measurement as a tracked background Bash command (`run_in_background: true`),
never detached with `nohup`, `setsid`, shell `&` or `disown`. Serialize measurements by starting
the next only when the previous command's completion notification arrives; do not make a
home-made chain script that launches multiple measurements.

Never poll or use foreground Bash `sleep` to wait for subagents or the clock. Claude Code delivers background results as
completion notifications. When all useful work is dispatched, yield the turn to those
notifications and the scheduled wakeups; keep the session open. The optional Stop hook is a
reminder, not a deadline guarantee: it fails open on repeated continuations and lets pending
background tasks or scheduled wakeups resume the lead. See the official
[subagent](https://code.claude.com/docs/en/sub-agents),
[scheduling](https://code.claude.com/docs/en/scheduled-tasks) and
[hook](https://code.claude.com/docs/en/hooks) contracts.

## Wrap-up

Start only at the computed wrap-up time defined in run-state.md, never because ideas feel exhausted. Dispatch no new hypotheses; judges, combinations
and final measurements may still run within the remaining budget. Merge judged winners into main
with `vibesys:combine-candidates`. Stop or collect outstanding workers before the deadline.
Never merge an unjudged combination. Re-measure the final main against the base with
`vibesys:measure`. Write `report.md`: the best commit, the verdict and ratio against the base,
what was tried and why each direction ended. If time cannot cover a final measurement, report
the last verified main (the base if no win), and mark the final verification incomplete. Delete
`.vibesys-optimize/active`, cancel this run's scheduled wakeups, then stop. The result is the
branch `vibesys/<run-id>`. Never change the user's checked-out branch.
