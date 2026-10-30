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

## One measurement, as a script

```sh
SHA=$(git rev-parse <ref>)
W="$RUN/measure/${SHA:0:12}"
git worktree add --detach "$W" "$SHA"
cd "$W"
<build command>                       # if declared; stop and report if it fails
<accuracy command>; CHECK=$([ $? -eq 0 ] && echo pass || echo fail)
if [ "$CHECK" = pass ]; then
  for i in 1 2 3 4 5; do
    <benchmark command> --output-json "$W/../result-${SHA:0:12}-$i.json"
  done
fi
# read the metric from each result file, then:
python3 <skill dir>/scripts/measurements.py record --run "$RUN" --commit "$SHA" ...
```

A commit whose build or check fails has no samples to record. Record nothing for it; put the
output in a file under the run directory and write the failure in the ledger and the handoff.
Use `--check fail` only when a later re-check of a commit that already has samples fails, with
the samples that commit was measured with: `best` skips any commit that has a failed check.

Pass `--exclusive yes` only when you know no other work shares the machine; otherwise `no`, or
leave it out when unknown. Samples of the base and the candidate should come from the same host.
The compare output warns when they do not.

## Samples and the verdict

The script compares a commit's samples with the base's by Welch's t test. A verdict needs at
least 3 samples on each side that are not all alike. The bar is the Student-t 97.5% quantile at
the Welch-Satterthwaite degrees of freedom (4.30 noises at the fewest samples, down to 2.04).
Everything else is `inconclusive`.

- Samples of one commit pool across records: take two more and record them again.
- Only samples with the same metric and the same task fingerprint (a hash of the task
  directory) are compared. If `OBJECTIVE.md` or the checker changes, old samples stop being
  comparable and the compare says so.
- The oriented `ratio` is above 1 when the commit is better, whatever the direction.
- One run has one base. A second `--role base` record must name the same commit.
- To ask "is this no worse than main", record main's samples as ordinary candidate records and
  compare with `--base <main SHA>`.

## Cleaning up

`git worktree remove --force <worktree>` when the record is written. The worktree is scratch;
the record is the result.
