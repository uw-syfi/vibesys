# Validating a task

Each recipe is a job, so the result is what the optimization will see. Pass `--task <task>` to
every call. Wait as `vibesys:measure` says and branch on the exit code.

If the task declares `needs`, check that the first `vibesys-cli submit check` is not refused for a
missing resource before anything else; a refusal names it and is the operator's to fix.

## The base

```
vibesys-cli submit check <base> --wait --json
vibesys-cli submit measure <base> --samples 5 --no-plan --wait --json
vibesys-cli show <base> --json
```

- A check that fails on the base is a task bug, or a real bug in the code: read
  `vibesys-cli logs <job>`, and ask the user before you call the code wrong.
- Read the samples and `measure.value` from `show`. The spread is the standard deviation over
  the mean. Under 2% is good, up to 5% is usable with more samples, above that fix the cause first:
  a workload too short to time, a cold cache, a background service, a timer of too coarse a grain.
- One sample over a few minutes makes every comparison slow; shrink the workload while it still
  exercises the same path.
- Set `relative_noise` in `objectives.toml` to the spread you saw, rounded up, if you declare it.
  For several objectives, read the spread of each from `show` and give each objective its own
  `relative_noise`, with the `[pareto]` one as the default for those that declare none. That edit changes the task's version: measure the base again after it.
- A service-style task also shows its cleanup here: after the check and the samples, nothing the
  wrapper created (a namespace, a pod, a process) is left running.

## Mutants

A mutant is a commit on a throwaway branch that breaks the code under optimization in a way an
optimizing agent might, by accident or on purpose. Write at least three, each one small:

| Kind | Example |
| --- | --- |
| Wrong answer | off by one, a dropped element, a wrong rounding |
| Skipped work | return early on large inputs, process a sample of the input |
| Benchmark gaming | detect the benchmark's input or seed and return a stored answer |
| Broken guarantee | drop a lock, reorder writes, lose durability the scope requires |

```
git switch -c vibesys-task/<task>-mutant-1 <base>
# edit the code under optimization, never the task
git commit -am "test: mutant 1, <what it breaks>"
vibesys-cli submit check HEAD --wait --json
```

Each must exit 1 with a failed check. A mutant that passes is a hole: strengthen the checker,
commit the task again, and rerun every recipe from the base, since the task's version changed.
For a multi-objective task, add a mutant that trades one objective for another (faster, but a
worse second objective): `compare` must answer `tradeoff`, not `better`.
Delete the mutant branches when done (`git branch -D`). Their jobs stay in the store, harmless.

## Sensitivity

Commit a deliberate slowdown of about twice the spread (an extra pass over the data, a sleep of
that size in the hot path) on a throwaway branch, measure it with five samples
(`submit measure <slow> --samples 5 --no-plan`, since it tests no idea), and run
`vibesys-cli compare <slow> --against <base> --json`. The verdict must be `worse`. If it is
`within_noise` or `inconclusive`, the optimization cannot see a gain of that size either: reduce
the noise or tell the user how large a gain must be to count.
