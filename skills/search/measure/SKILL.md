---
name: measure
description: "Use when you must measure a commit for a vibesys optimization, compare it with another commit, or read the current best number. Submits check, measure and command jobs to vibesys-cli, which builds and runs the exact commit in a clean checkout and owns the queue, the cache, the verdict and the saved results. Says how to wait for a job, what each wait exit code means, and how to read what came back."
---

# Measure

A number is a job that `vibesys-cli` ran from a clean checkout of one commit and saved. Never
report a number that `vibesys-cli` did not print. The CLI decides what a check, a sample, a verdict
and a promotion are; this skill says how to use it and how to read what comes back. The lead
installs and checks the CLI (`vibesys:setup`).

**Which CLI.** Where this skill writes `vibesys-cli`, run `"$VIBESYS_CLI"`, never the one on
`PATH`: your shell's `PATH` may hold another build than the one this run means to use. A run
started by the orchestrator sets it. If it is unset or empty, stop and say `VIBESYS_CLI is not
set; export it as the absolute path of the vibesys-cli to use`.

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
| The official number: check, then benchmark samples | `vibesys-cli submit measure <sha> --samples 5 --plan <n>` |
| Any other work that drives load (a sanity or tuning run, a profile) | `vibesys-cli submit command <sha> --image harness\|candidate -- <argv...>` |

Each prints a job id and returns at once. A command names the image it runs in, with no default:
`--image harness` is the task's image, where its check and benchmark run, and on a task with
`[candidate]` it runs beside the candidate's server exactly as the benchmark does
(`VIBESYS_SERVICE_URL` set), so it is where a load goes; `--image candidate` is the image built
from the commit's `[candidate]` dockerfile, with no server, for a version print, an import or a
microbenchmark there, and only a task with `[candidate]` has it. A profile is a harness command
whose `--wrap '<profiler>'` starts the candidate's server under the profiler:
`submit command <sha> --image harness --wrap '<profiler>' -- <load argv>`. A measure says what it tests: `--plan <n>` links it to
your plan, so the number is read against that idea, and `--no-plan` says it tests none, as the
baseline and a re-measure of the head do. A measure from a run that gives neither is refused,
naming both flags; give both and it is refused too. `--plan` also links a check or a command job.
Useful flags: `--note "<text>"` says why you ran it (notes are for readers; they change no result), and
`--service` with `--probe` runs the command against a service the CLI starts and stops (not
beside a candidate's server, which the CLI already starts).
`--no-cache` and `--recheck` force fresh work; only a reason to doubt a saved result justifies
them. Use the task's own commands through `check` and `measure`; do not rebuild them by hand.

The candidate's dependencies (its ROCm or CUDA userspace, torch, kernel libraries, profilers) live
in the candidate Dockerfile that the task's `[candidate]` section names, inside the repository. To
change one, edit that Dockerfile, commit, then run a cheap `submit command --image candidate` (for
example a version print or an import) on the commit: the job builds the image from the commit and smoke-tests it
before a measure pays for the build.

On a placement where jobs wait hours in a cluster queue, hold capacity with a standing allocation and
submit with `--alloc any`: `vibesys:allocations`.

Everything that puts load on the machine goes through a job, so the daemon decides what runs
together and the numbers stay clean: a measure job runs alone on its machine (it holds its
placement alone, or, where each job gets a whole node of its own, its node), while checks and
command jobs may share a placement up to its `max_jobs`. Do not start a benchmark, a profiler
or a server in your own shell. Unit tests and in-process microbenchmarks that finish in seconds
are checks, not load: run them in your worktree.

## Wait

Run `vibesys-cli wait <job>...` as one tracked background task of your harness and take its
completion notification; add `--timeout` to bound it. `submit ... --wait` blocks the same way and
is for short jobs only. Never wait with a foreground `sleep` or a polling loop. Read the exit code
before the output:

| Code | Meaning | Do |
| --- | --- | --- |
| 0 | every job succeeded (with `--any`, the first one did) | read the result |
| 1 | the daemon said no: a job failed, was cancelled or was lost | `vibesys-cli logs <job>`, fix, commit, resubmit the new commit |
| 2 | invalid input, or a job or commit that does not exist | fix the call |
| 3 | the wait timed out; no job was cancelled | the jobs still run; wait again |
| 4 | no answer: no daemon, an incompatible one, an internal failure, or the end of the jobs is unknown | says nothing about the jobs, which may still succeed; do not resubmit, inspect (see [jobs.md](references/jobs.md)) |

With `--json`, `wait` prints one `WaitOutcome`: `kind` (`WAIT_OUTCOME_KIND_ALL_SUCCEEDED`,
`WAIT_OUTCOME_KIND_SOME_FAILED`, `WAIT_OUTCOME_KIND_FIRST_SUCCEEDED`, `WAIT_OUTCOME_KIND_FIRST_FAILED` or
`WAIT_OUTCOME_KIND_TIMED_OUT`), `decided_by` (the job that ended the wait) and
`jobs` with each job's last state. `submit --wait --json` prints the submit response first and
the `WaitOutcome` on the last line. The exit code follows the `kind`; use it to branch.

A failed check or build saves nothing; put the failing output in the hand-back, with the SHA.

## Read

- `vibesys-cli show <sha> --json` lists what was saved for a commit. A measure result's
  `measure.value` is its headline number, computed by the CLI. Never average samples yourself, and
  never report a number from anywhere else.
- `vibesys-cli compare <sha> [--against <sha2>]` prints one verdict and its numbers, against the
  result branch head or against `<sha2>`. It exits 0 for every verdict; read the verdict, which
  `--json` has as `comparison.verdict.kind` (`VERDICT_KIND_BETTER`, `VERDICT_KIND_WORSE`,
  `VERDICT_KIND_WITHIN_NOISE`, `VERDICT_KIND_INCONCLUSIVE` or `VERDICT_KIND_TRADEOFF`). Only `better` is a gain. `worse` and `within_noise` are
  not. On `inconclusive`, add samples within your brief's budget, unless the line says `not comparable`: the two commits were measured under different task versions or metrics, which more samples cannot fix, so report it. Never compute a verdict yourself.
- Samples add up: `submit measure <sha> --samples 8 --plan <n>` runs only the missing ones. Identical readings
  on both sides stay inconclusive however many you add.
- `vibesys-cli result --json` has the number to report for the result branch, in
  `current.result.measure.value`: the head's latest checked measurement, the one a promotion rests
  on included. It exits 1 when there is none (`none.reason` says why); for `NO_CURRENT_RESULT_REASON_HEAD_NOT_MEASURED`,
  `vibesys-cli submit measure <head SHA> --no-plan` gives it one.
- Tell the reader the base, the sample counts and the environment from the output, never from memory.
