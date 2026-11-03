# Validating a task

Each recipe is a job, so the result is what the optimization will see. Pass `--task <task>` to
every call. Wait as `vibesys:measure` says and branch on the exit code.

## The base

Prove the result path first. Before any long measurement, bring-up or tuning, one job of one
sample must leave its number in `vibesys-cli show <base> --json` (`measure.value`). A benchmark
whose number reaches only its logs, or a wrapper that loses the result file, wastes every run made
before the break is found.

```
vibesys-cli submit check <base> --wait --json
vibesys-cli submit measure <base> --samples 5 --wait --json
vibesys-cli show <base> --json
```

- A check that fails on the base is a task bug, or a real bug in the code: read
  `vibesys-cli logs <job>`, and ask the user before you call the code wrong.
- Read the samples and `measure.value` from `show`. The spread is the standard deviation over
  the mean. Under 2% is good, up to 5% is usable with more samples, above that fix the cause first:
  a workload too short to time, a cold cache, a background service, a timer of too coarse a grain.
- One sample over a few minutes makes every comparison slow; shrink the workload while it still
  exercises the same path.
- When the workload must stay as it is and the base itself is far too slow (a server that takes
  many minutes per request, a default configuration nobody runs in production), dispatch a
  bring-up pass before any measurement: a subagent applies the obvious fixes (configuration,
  build flags, a known fast path) on a branch, and the check must still pass. Its commit becomes
  the base. This leaves the task's version alone, and every later number is measured from the
  faster base.
- Set `relative_noise` in `objectives.toml` to the spread you saw, rounded up, if you declare it.
  That edit changes the task's version: measure the base again after it.

## Honest variants

Before the mutants, check that the gate admits correct code that is not bit-identical to the
reference: the same program at another batch size or thread count, another build or another CPU.
A numerical gate with fixed tolerances that only the reference itself can pass blocks every real
optimization. Measure the error between such honest variants on the checker's own probes, and
set tolerances above it, scaled where the output is uncertain, before you prove they still catch
the mutants below.

Per-token or per-output limits set above that noise also admit small systematic damage, because
one short output cannot tell a shift from noise. Add at least one aggregate statistic over many
tokens or outputs (a mean error over a varied corpus), whose noise shrinks with the count. Build
long probes from varied text, never one repeated snippet, which hides damage that depends on
distance, such as a shortened attention window.

## Mutants

A mutant is a commit on a throwaway branch that breaks the code under optimization in a way an
optimizing agent might, by accident or on purpose. Write at least three, each one small:

| Kind | Example |
| --- | --- |
| Wrong answer | off by one, a dropped element, a wrong rounding |
| Skipped work | return early on large inputs, process a sample of the input |
| Benchmark gaming | detect the benchmark's input or seed and return a stored answer |
| Broken guarantee | drop a lock, reorder writes, lose durability the scope requires |
| Small systematic damage | one layer at lower precision, a shorter attention window, added noise |

```
git switch -c vibesys-task/<task>-mutant-1 <base>
# edit the code under optimization, never the task
git commit -am "test: mutant 1, <what it breaks>"
vibesys-cli submit check HEAD --wait --json
```

Each must exit 1 with a failed check. A mutant that passes is a hole: strengthen the checker,
commit the task again, and rerun every recipe from the base, since the task's version changed.
Delete the mutant branches when done (`git branch -D`). Their jobs stay in the store, harmless.

## Sensitivity

Commit a deliberate slowdown of about twice the spread (an extra pass over the data, a sleep of
that size in the hot path) on a throwaway branch, measure it with five samples, and run
`vibesys-cli compare <slow> --against <base> --json`. The verdict must be `worse`. If it is
`within_noise` or `inconclusive`, the optimization cannot see a gain of that size either: reduce
the noise or tell the user how large a gain must be to count.
