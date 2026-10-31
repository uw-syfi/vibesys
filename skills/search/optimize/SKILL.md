---
name: optimize
description: "Use when asked to optimize a repository against a benchmark, unattended, for a fixed time, as `/vibesys:optimize <task> --time <duration> [--fanout N]`, in a repository that has `.vibesys/tasks/<task>/`. Plans competing hypotheses, has subagents try them in parallel git worktrees, measures every commit against the base, and leaves a best commit, a record of every measurement and a report."
---

# Optimize

You are the lead of a time-boxed optimization run. You steer; you never edit candidate code.
Subagents do that, each in its own git worktree. You choose what to try, check what comes back,
and treat the time box as a limit, not a quota. Everything you need to resume lives in files, so a
context limit costs you nothing: after any compaction, reread `run.json`, `ledger.md` and
the newest files in `notes/` before acting.

Arguments: `<task>`, `--time <duration>` (`90m`, `2h`, `1h30m`), and `--fanout N`, the most
subagents running at once (default 3).

Requires Claude Code on POSIX (Linux or macOS), git and Python 3.10+.

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
2. Run `python3 "${CLAUDE_SKILL_DIR}/scripts/run.py" start --task <task>
   --seconds <duration-in-seconds> --fanout <N>`. Save its absolute output as `$RUN`.
   The controller creates main and owns all lifecycle state; commands are in run-state.md.
3. Create exactly one one-shot wrap-up wakeup with `CronCreate`, `recurring: false`.
   Reconcile with `CronList` on resume, cancel obsolete owned wakeups with `CronDelete`.
4. Measure the base with `vibesys:measure`, at least 5 samples, as a tracked background Bash command with `run_in_background: true`. No comparison
   means anything until it lands. Plan while it runs. When the evidence is thin, dispatch read-only planners (briefs.md) instead of
   guessing hypotheses yourself, then merge, deconflict and choose.

## Loop

Never run run.py commands in parallel. The controller locks whole state transactions.

After every completion notification or wakeup, run:

```sh
python3 "${CLAUDE_SKILL_DIR}/scripts/run.py" status --run "$RUN"
```

Do what `next` says. Use the commands in [run-state.md](references/run-state.md) to add work
before dispatch, transition it on return, record its judge result, and promote winners.
Give each background subagent a self-contained brief from briefs.md and its own worktree.
Keep measurements serialized as tracked background Bash commands (`run_in_background: true`).
Use `track` with the Bash task ID and a bounded PID-file handoff (`--pid-file <file> --timeout 10`); after its completion
notification use `untrack`.

Never stop to ask the user: decide, and log the decision (alternatives, reason, how to undo it) in
`ledger.md`. Judging also takes a dispatch slot, so keep one free for it. A judge result of
`unmeasured` records no pass: re-judge once the measurement lands.

Plan the clock backwards. The last `move-main` must leave room for one full fresh measurement of
main plus a margin, so write that deadline in `run.json` notes at the start. When one measurement
takes tens of minutes (a remote queue), start it for each commit as soon as the commit exists,
and rely on its multi-sample record, not on one quick sample, which can understate the gain.

Branch new ideas from the current winner. When leaving slots idle, run
`idle --run "$RUN" --reason "<why>"`; the controller records the decision in state, ledger
and report. It holds until a new item returns or main moves; then decide again.
Finish before wrap-up only with `finish --early --reason "<why>"`, after all other guards pass.

Wait only on completion notifications or the one wakeup. Never use a timer sleep to wait out
the budget or an open-ended polling loop.
Bounded readiness waits for a service are allowed; use the task readiness probe through the measure skill service helper, with a fixed
attempt limit and fail if it never becomes ready. Do useful work or yield. A cron alone does
not release the Stop hook.

## Reporting

Describe every item by what it does in plain words, then its code name in parentheses, such as
"paged attention for decode (d2)". Never use a bare code name in the ledger, a report or a message
to the user. Lead each report with time left, then a table of items with status and measured gain.

## Wrap-up

When status says begin wrap-up, dispatch no new hypotheses. Collect or stop your own agents
and background tasks, transition every open item to a terminal state with a reason, and
untrack commands only after they have stopped. Promote only through `move-main`.
Re-measure the current main head after its last move, then run:

```sh
python3 "${CLAUDE_SKILL_DIR}/scripts/run.py" finish --run "$RUN"
```

`finish` checks time, closed work, stopped tracked commands and a fresh final measurement.
It prints and writes the exact public comparison with its record ids and decision history
into `report.md` and removes `active`. Append explanatory prose to the report; do not copy numbers or remove the marker yourself. Cancel the owned wakeup.
If the deadline prevents finishing, report the failed guard and incomplete verification.
The result branch is `vibesys/<run-id>`; preserve the user's checked-out branch.
