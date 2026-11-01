---
name: measure
description: "Use when you must measure a commit's benchmark result for a vibesys optimization, compare it with the base, or find the best commit so far. Builds and runs the exact commit from a clean git worktree, checks correctness first, takes several samples, and records them through a script that is the only writer of the optimization's measurement records."
---

# Measure

A measurement is a commit, a correctness check and several benchmark samples, taken from a clean
worktree of that exact commit and recorded by a script. Never trust a number that is not in the
record.

The script: `measurements.py` in this skill's `scripts/` directory, run with `python3` (standard library
only; how to locate it: [harness reference](../optimize/references/harness/README.md)).
`--dir` is the optimization's state directory, `.vibesys-state/optimize/<task>` (see
`vibesys:state`). Details of every step and flag are
in [measuring.md](references/measuring.md).

## Steps

1. Read the task's `vibesys.input.toml`: the optional `[build]` command, `[accuracy]` command,
   `[benchmark]` command, `[benchmark.result]` (`json_argument`, `metric`) and the direction in
   `objectives.toml`. Use `command` arrays as the executable prefix; apply the service setup and runtime
   arguments described in measuring.md.
2. `git rev-parse <ref>` for the full SHA. Make a clean worktree of it:
   `git worktree add --detach <state dir>/worktrees/measure-<work-id>-<attempt> <sha>`. Never measure a working copy.
3. Get the lead's measurement slot (one check/benchmark/record sequence at a time).
   In that worktree: build if the task has a build step, start the service if required, then run the correctness check. If the
   build or the check fails, stop: record nothing, save the output to a file in
   `scratch/` and report the failure with the commit SHA.
4. Run the benchmark N times (default 5; the base at least 5). Each run appends the task's
   `json_argument` and a fresh output path; read the `metric` field from that JSON.
5. Record: `measurements.py record --dir <dir> --commit <sha> --role base|candidate --label <text>
   --metric <name> --direction max|min --command "<benchmark command>" --sample V ... --check
   pass|fail --exclusive yes|no`.
6. Ask: `measurements.py compare --dir <dir> --commit <sha>` (add `--base <sha>` to compare with
   another commit, such as main). `best --dir <dir>` names the best verdict-backed commit.
   `show --dir <dir> [--commit <sha>]` prints the records of all shards. Add `--json` for
   machine output. `check --dir <dir> [--branch vibesys/<task>]` is read-only: it exits non-zero
   unless the branch head has a passing `better` record, and lists unrecorded commits.

## Reading a verdict

`better`, `worse`: the gap is beyond the spread of the samples. `within_noise`: there is no
evidence of a gap. `inconclusive`: too few samples (fewer than 3 on a side), samples that are
both constant, or samples of a different metric or task version. Add samples only within the
brief's budget; identical readings on both sides remain inconclusive even after repeats. Exit
code 2 means the input was invalid and the message says which part.

Run each measurement's build/check/benchmark/record sequence as one background task your harness
tracks and reports on when it finishes (see [harness reference](../optimize/references/harness/README.md)).
Never detach the whole sequence with `nohup`, `setsid`, shell `&` or `disown`: the harness must
track completion and notify the lead. Serialize by starting the
next measurement only when the previous task's completion notification arrives. Do not
launch a home-made chain script of measurements. While it runs, do other useful work or yield
for its notification; never wait with foreground `sleep`.

Use `set -euo pipefail` in the measurement shell. Preserve failure with
`measure-command > "<state dir>/scratch/measure.log" 2>&1` as the last command; read the log in a separate
call after notification. Never append `; tail ...` to a measurement. Record the exact
executed benchmark command, including dependency flags, port and output argument. Build
that command once and execute the same value you record; do not reconstruct it from memory.
Use `scripts/service.py --service '<JSON argv>' --probe '<JSON argv>'
--client '<JSON argv>' --limit <seconds>` to own services, readiness and cleanup.
Keep the service in foreground mode; disable daemonization.
It kills and verifies the whole process group, including wrapper descendants; readiness
exhaustion fails before the client runs. Do not write `&`/PID/sleep recipes.

Every load-generating command, recorded or unrecorded (including sanity and tuning runs),
must execute through `measurements.py run -- <command>`. Its host-wide flock prevents overlap.

Use `record --observations <JSON file>` for descriptive auxiliary measurements such as
latency: map metric names (including units) to numeric sample arrays. `compare` and the
report print the pooled observed ranges for the exact contributing revisions and evaluator.
Derive prose ranges only from that output; omit ranges that were not recorded.

## Slow or remote measurements

When one measurement takes long (a cluster queue), submit one job per commit, cache its results
by commit SHA, and record from the cache. After any relaunch, list the queue and cancel a
duplicate job for the same commit before assuming the first died. Start the job as a tracked
background task, never with shell `&`. Do not use `pkill -f` from the shell that matches
its own command line; kill by the saved PID. Judge and compare on the multi-sample official
record; a single development sample is only a smoke check.
