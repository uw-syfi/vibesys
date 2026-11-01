# Measuring in detail

## The task's declarations

```toml
[build]                      # optional: run first, from the root of the checkout
command = ["make"]
[accuracy]                   # the correctness check
command = ["uv", "run", "python", ".vibesys/tasks/default/accuracy_checker/checker.py"]
[benchmark]
command = ["uv", "run", "python", ".vibesys/tasks/default/benchmark/benchmark.py"]
[benchmark.result]
json_argument = "--output-json"   # the benchmark writes its JSON to the path after this flag
metric = "throughput_ops_per_sec" # a top-level numeric field of that JSON
```

`objectives.toml` gives the direction (`max` or `min`) of the metric. `timeout_seconds` on a
command is the limit for that command; honor it with `timeout`.

Run every command from the root of the measured worktree. A task command writes its scratch
output outside `.vibesys/` (for example under `/tmp`).

The result JSON: a top-level field named by `metric` is the value. If the JSON is a list, accept
it only when exactly one entry carries that field. A value that is missing or not finite is a
failed run: do not record it.

## Setup and runtime arguments

Before measuring, the lead writes one reproducible setup recipe into each brief: dependencies,
build command, service start/readiness/stop commands (if needed), and the runtime arguments
appended to the declared check and benchmark prefixes. Use the same workload and settings for
base and candidates. Record the complete benchmark command including runtime arguments, not just
the TOML prefix.

## One measurement

1. The lead serializes check/benchmark/record sequences on a host, including profiling that
   drives load. Workers can implement in parallel, but must receive a measurement slot in
   their brief before measuring. A worker without a slot returns its committed revision as
   `ready for measurement`; the lead measures it in a background job or resumes the worker
   with the slot after the current owner finishes. Never wait inside a worker for permission
   to measure. Do not dispatch overlapping measurement windows. This also
   requires every load command, including unrecorded profiling or sanity runs, to take
   the host-wide `measurements.py run -- <command>` lock. Recorder validation and append
   also take the shard lock.
2. Use the absolute state directory `D` (`vibesys:state`). Resolve the full commit SHA and create a new detached worktree:
   `git worktree add --detach "$D/worktrees/measure-<work-id>-<attempt>" <SHA>`.
   Use a fresh attempt ID for every remeasurement. Make the parent directory first.
3. From that checkout, execute the brief's build and service recipe, then the check. Use `service.py` with JSON service/probe/client argv to bound readiness
   and own cleanup of the entire group. Save logs
   under `scratch/`. Abort on build or correctness failure.
4. Execute the benchmark five times, appending its declared `json_argument` and a distinct
   absolute JSON output path each time. Abort on a nonzero exit, timeout, missing output,
   missing metric. Never reuse output from an earlier attempt.
5. Use `--role base` whenever the measured SHA equals the optimization's base commit (the first base record), otherwise
   `--role candidate`, including remeasurements. Read the metric from each fresh JSON and
   call the recording CLI once with one `--sample`
   flag per value, `--check pass`, and the full command. Store auxiliary numeric samples
   with `--observations <JSON file>` when the report will describe their ranges. A repeat records only new samples,
   never the old samples again. Return the slot and clean up the server and worktree.

The lead runs this sequence as a background Bash job when measuring itself. Bound the whole
sequence by the worker return time, and each command by its declared timeout as well. If fewer
samples fit, record what completed and report `inconclusive` when there are too few for a
verdict.

A commit whose build or check fails has no samples to record. Record nothing for it; put the
output in a file under `scratch/` and return the failure in the handoff.
Use `--check fail` only when a later re-check of a commit that already has samples fails, with
the samples that commit was measured with: `best` skips any commit that has a failed check.

Pass `--exclusive yes` only when you know no other work shares the machine; otherwise `no`, or
leave it out when unknown. Samples of the base and the candidate should come from the same host.
The compare output warns when they do not.

## Samples and the verdict

The script compares a commit's samples with the base's by Welch's t test. A verdict needs at
least 3 samples on each side and nonzero noise (one side may be constant). The bar is the
Student-t 97.5% quantile at the floor of the Welch-Satterthwaite degrees of freedom (4.30 noises
at the fewest samples, down to 2.04). Everything else is `inconclusive`.

- Samples of one commit pool across records: take two more and record them again.
- Only samples with the same metric and the same task fingerprint (a hash of the task
  directory) are compared. If `OBJECTIVE.md` or the checker changes, old samples stop being
  comparable and the compare says so.
- The oriented `ratio` is above 1 when the commit is better, whatever the direction.
- One optimization has one base. A second `--role base` record must name the same commit.
- To ask "is this no worse than main", record main's samples (base role for the initial commit, candidate otherwise) and
  compare with `--base <main SHA>`.

## Shards and `check`

`record` appends to `measurements/<commit-sha>.jsonl` in the state directory; `compare`, `best`
and `show` read every shard. `check --dir D [--branch vibesys/<task>]` is read-only: it asks git
and the records whether the branch head has a passing check and a `better` verdict against the
base, lists the commits since the base that have no record, and exits 1 when the head is not
proven. It records nothing.

## Cleaning up

`git worktree remove --force <worktree>` when the record is written. The worktree is scratch;
the record is the result.
