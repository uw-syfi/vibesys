---
name: measure
description: "Use when you must measure a commit for a vibesys optimization, compare it with another commit, or read the current best number. Submits check, measure and command jobs to vibesys-cli, which builds and runs the exact commit in a clean checkout and owns the queue, the cache, the verdict and the saved results. Says how to wait for a job, what each wait exit code means, and how to read what came back."
---

# Measure

A number is a job that `vibesys-cli` ran from a clean checkout of one commit and saved. Never
report a number that `vibesys-cli` did not print. The CLI decides what a check, a sample, a verdict
and a promotion are; this skill says how to use it and how to read what comes back. The lead
installs and checks the CLI (`vibesys:optimize`).

Every call takes the context flags your brief names: `--task`, `--run`, `--harness` and `--agent`.
All worktrees of the repository share one store per task, so a call from your worktree sees the
jobs and results of every other agent. The examples leave them out. Add `--json` wherever you parse the output. Detail on jobs,
exit codes and slow runs: [jobs.md](references/jobs.md).

Harness-specific mechanics (background tasks, waiting): read `references/harness/<harness>.md` for
the harness you are running in.

## Jobs

Commit first: a job runs a commit, never a working copy, so what is not committed is not run
(`vibesys:commit`).

| You need | Run |
| --- | --- |
| The correctness check on a commit | `vibesys-cli submit check <sha>` |
| The official number: check, then benchmark samples | `vibesys-cli submit measure <sha> --samples 5` |
| Any other work that drives load (a sanity or tuning run, a profile) | `vibesys-cli submit command <sha> -- <argv...>` |

Each prints a job id and returns at once. Useful flags: `--plan <n>` links the job to your plan,
`--note "<text>"` says why you ran it (notes are for readers; they change no result), and
`--service` with `--probe` runs the command against a service the CLI starts and stops.
`--no-cache` and `--recheck` force fresh work; only a reason to doubt a saved result justifies
them. Use the task's own commands through `check` and `measure`; do not rebuild them by hand.

Everything that puts load on the machine goes through a job, so the queue runs one at a time and
the numbers stay clean. Do not start a benchmark, a profiler or a server in your own shell.

## Wait

Run `vibesys-cli wait <job>...` as one tracked background task of your harness and take its
completion notification; add `--timeout` to bound it. Never wait with a foreground `sleep` or a
polling loop. Read the exit code before the output:

| Code | Meaning | Do |
| --- | --- | --- |
| 0 | every job succeeded | read the result |
| 1 | a job did not succeed | `vibesys-cli logs <job>`, fix, commit, resubmit the new commit |
| 2 | invalid input | fix the call |
| 3 | timeout | the jobs still run; wait again |
| 4 | says nothing about the job (held, in doubt, daemon unreachable) | do not resubmit; inspect with `vibesys-cli jobs --json` or `vibesys-cli show <sha> --json` |

A failed check or build saves nothing; put the failing output in the hand-back, with the SHA.

## Read

- `vibesys-cli show <sha> --json` lists what was saved for a commit; each result's `value` is its
  headline number.
- `vibesys-cli compare <sha> [--against <sha2>]` prints one verdict and its numbers, against the
  result branch head or against `<sha2>`. Only `better` is a gain. `worse` and `within_noise` are
  not. On `inconclusive`, add samples within your brief's budget. Never compute a verdict yourself.
- Samples add up: `submit measure <sha> --samples 8` runs only the missing ones. Identical readings
  on both sides stay inconclusive however many you add.
- `vibesys-cli result --json` has the number to report for the result branch, in `value`. It refuses when the head has
  no fresh measurement; its message says what to run.
- Tell the reader the base, the sample counts and the environment from the output, never from memory.
