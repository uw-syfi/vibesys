# Validating a task

Each recipe is a job, so the result is what the optimization will see. Pass `--task <task>` to
every call. Wait as `vibesys:measure` says and branch on the exit code.

If the task declares `needs`, check that the first `vibesys-cli submit check` is not refused for a
missing resource before anything else; a refusal names it and is the operator's to fix. A task that
needs GPUs takes `--gpus N` on a check or command where a placement has a GPU pool: ask for the
fewest the check can run on (a refusal names the allowed sizes), and a measure takes none.

## The base

```
vibesys-cli submit check <base> --wait --json
vibesys-cli submit measure <base> --samples <k> --no-plan --wait --json
vibesys-cli show <base> --json
```

`<k>` is how many same-code samples you take to measure the noise: five when one sample takes
seconds to minutes; when one takes most of an hour, at least three, after asking the user for the
hours. It is only for this recipe: the optimization's count is `samples` in `objectives.toml`.

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
- Declare `samples` from the cost of one sample: leave it out (5) when a sample is cheap, and
  declare `samples = 1` when one sample costs most of an hour or the whole machine, together with
  the `relative_noise` you measured here, since without a floor `compare` needs three samples a
  side. Declare `min_gain` when the user names the smallest gain worth having (task-format.md).
- A service-style task also shows its cleanup here: after the check and the samples, nothing the
  wrapper created (a namespace, a pod, a process) is left running.

## A task with no implementation yet

When the task asks for the code to be built (a server for a model that has none on this hardware,
for example), the base fails the check by design and none of the recipes above runs on it. The
first time the checker and the benchmark run end to end would then be in the middle of the
optimization, where a fault in either one costs hours. Prove them now with a **stand-in**: the
smallest code that does the work, committed on a throwaway branch (an earlier port, a thin
wrapper over the reference implementation, or any engine that serves the task's interface on the
budgeted hardware).

- The stand-in passes the check. A failure is either the stand-in's or the task's: read the log,
  and fix the task when the checker, the reference or the environment is at fault.
- Its benchmark runs once to the end with every resource the real run uses (mounts, caches,
  tokenizers, the load generator's own downloads): `submit measure <stand-in> --no-plan`. Its
  number says nothing about the target; that it lands says the benchmark works.
- The mutants below are written against the stand-in.

When no stand-in exists, say so when you hand back, run every part of the benchmark that runs
without one (its data loading, its client against a fake server), and tell the user which parts
were never run.

## A frozen reference agrees with itself

Golden outputs recorded with a closeness measure (a tie margin between two candidate answers, a
tolerance) are only as good as that measure. When the reference is produced by a numerically
sensitive process (floating point, parallel reduction, sampling), check that every point the
checker treats as decisive is one where the reference's own runs agree: compare two independent
passes of the reference over the same inputs (for a model, its token-by-token generation and one
forward pass over the whole output), and record every point where they disagree as a tie. A
point where the reference contradicts itself fails every correct candidate at random. Fix the
reference, not the tolerance, and do it before the run: a task edit after the run starts makes
every saved result incomparable.

## Mutants

A mutant is a commit on a throwaway branch that breaks the code under optimization in a way an
optimizing agent might, by accident or on purpose. Write at least three, each one small:

| Kind | Example |
| --- | --- |
| Wrong answer | off by one, a dropped element, a wrong rounding |
| Skipped work | return early on large inputs, process a sample of the input |
| Benchmark gaming | detect the benchmark's input or seed and return a stored answer |
| Broken guarantee | drop a lock, reorder writes, lose durability the scope requires |
| Selective rejection | on a served-only metric, reject every request above a size the benchmark sends |

```
git switch -c vibesys-task/<task>-mutant-1 <base>
# edit the code under optimization, never the task
git commit -am "test: mutant 1, <what it breaks>"
vibesys-cli submit check HEAD --wait --json
```

Each must exit 1 with a failed check; the selective-rejection mutant is the benchmark's to catch,
so its `submit measure <mutant> --no-plan` must fail instead. A mutant that passes is a hole:
strengthen the checker, commit the task again, and rerun every recipe from the base, since the task's version changed.
For a multi-objective task, add a mutant that trades one objective for another (faster, but a
worse second objective): `compare` must answer `tradeoff`, not `better`.
Delete the mutant branches when done (`git branch -D`). Their jobs stay in the store, harmless.

## Sensitivity

Commit a deliberate slowdown of about twice the spread (an extra pass over the data, a sleep of
that size in the hot path) on a throwaway branch, measure it at the task's sample count
(`submit measure <slow> --no-plan`, since it tests no idea), and run
`vibesys-cli compare <slow> --against <base> --json`. The verdict must be `worse`. If it is
`within_noise` or `inconclusive`, the optimization cannot see a gain of that size either: reduce
the noise or tell the user how large a gain must be to count.
