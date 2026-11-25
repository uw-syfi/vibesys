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
| The correctness check on a commit | `vibesys-cli submit check <sha> [--gpus N]` |
| The official number: check, then benchmark samples | `vibesys-cli submit measure <sha> --plan <n>` |
| Any other work that drives load (a sanity or tuning run, a profile) | `vibesys-cli submit command <sha> --image harness\|candidate [--gpus N] -- <argv...>` |

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
A command whose results are files (timings, a profile) adds `--outputs` and writes them under
`$VIBESYS_ARTIFACTS_DIR`; read them back with `vibesys-cli outputs <job> --to <dir>`
([jobs.md](references/jobs.md#output-files-of-a-command-job)).
GPUs: a check or command of a GPU task says `--gpus N` (the fewest the step needs; there is no
default), and a measure takes the whole node. The task's GPU budget, how to check per-device use,
and what to report are in [gpus.md](references/gpus.md).
Useful flags: `--note "<text>"` says why you ran it (notes are for readers; they change no result), and
`--service` with `--probe` runs the command against a service the CLI starts and stops (not
beside a candidate's server, which the CLI already starts).
A command job is killed, and fails, at its `--timeout`, which defaults to the daemon's limit of 30
minutes. For anything that may run longer, such as a device profile, set `--timeout` to the job's
expected length plus a margin (`--timeout 2h`).
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
are checks, not load: run them in your worktree, but not while `vibesys-cli jobs --state running`
shows a measure on a placement on this machine. A local `docker run` takes `--rm --init --user
"$(id -u):$(id -g)"` and a time limit inside the container, never only an outer `timeout`, which
leaves the container running.

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
- A measure without `--samples` takes the task's count: `samples` in its `objectives.toml`, else 5.
  Leave it to the task; a task whose sample is expensive declares 1. Samples add up:
  `submit measure <sha> --samples <count> --plan <n>` with more than the commit holds runs only the
  missing ones. Identical readings on both sides stay inconclusive however many you add.
- The verdict records what it rested on: `relative_noise` (the task's declared noise floor),
  `min_gain` (the smallest gain the task counts as better) and `decided_by`. A `within_noise`
  decided by `VERDICT_BOUND_MIN_GAIN` is a gain the noise test passed that is below the task's
  `min_gain`; it is not a gain, so report it as within the task's bar, not as noise.
- The verdict also says its bar. `threshold` is the gain the deciding bar stands at: `critical`
  noises (2 under a declared `relative_noise`) times `noise`, or `min_gain` when `decided_by` names
  it. `sigma_source` says what set the noise: `SIGMA_SOURCE_DECLARED_FLOOR` (the task's
  `relative_noise`, at least the spread the runs showed) or `SIGMA_SOURCE_OBSERVED_SPREAD`;
  `observed_spread` is that spread. For a `within_noise` gain that is positive and above `min_gain`,
  `samples_to_decide` is how many samples a side would decide it with the same means and sigma
  (zero: no count would). The text line says the same, e.g. `need gain > 10.67% (2 × noise 5.337%,
  sigma from declared relative_noise 0.05; observed spread 0.2401%); at this gain about 6 vs 6
  samples would decide`. Weigh that count against your brief's sample budget before topping up; it
  is an estimate from the samples so far, not a promise of `better`.
- A provisional `relative_noise` that is far above the observed spread across several base samples
  should be raised with the user as a task recalibration (a task edit); agents must not edit it
  themselves.
- `vibesys-cli result --json` has the number to report for the result branch, in
  `current.result.measure.value`: the head's latest checked measurement, the one a promotion rests
  on included. It exits 1 when there is none (`none.reason` says why); for `NO_CURRENT_RESULT_REASON_HEAD_NOT_MEASURED`,
  `vibesys-cli submit measure <head SHA> --no-plan` gives it one.
- Tell the reader the base, the sample counts and the environment from the output, never from memory.
