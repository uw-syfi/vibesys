---
name: create-task
description: "Use when a user describes in their own words what they want to optimize in a repository (the code, the workload, the number that should move, what must stay correct) and there is no `.vibesys/tasks/<task>/` for it yet, or an existing task needs to be made runnable. Turns that description into a task vibesys-cli accepts: a manifest, an objective, a checker and a benchmark, then proves it with real jobs (the base passes, broken code fails, the number is stable) before anyone optimizes against it."
---

# Create a task

A task is the contract every later agent is held to: what may change, what counts as correct,
what number counts as better. An optimization finds every hole in it, so a task is finished only
when real jobs show that the checker rejects broken code and the benchmark gives a stable number.
You write the task; you never optimize the code. Making the base work (a baseline that fails
the check, a build to fix, a numerical gap to trace) is implementation: dispatch it to a subagent
with the evidence, and keep only the task's decisions.

Needs git and `vibesys-cli` (install it as `vibesys:optimize` says). File format:
[task-format.md](references/task-format.md). Validation recipes:
[validation.md](references/validation.md).

## 1. Understand the request

Read the user's description, then the repository: how it builds, its tests, any benchmark or
load generator it already has, how it is run in production. Reuse what exists; a benchmark the
project already trusts beats one you invent.

Pin down six facts. Find each in the code or the description first. Ask the user only for what
you cannot find, in one round of questions, in their words, each with your proposed default:

1. **Scope:** which code may change, and what must not (public interfaces, file formats, dependencies).
2. **Workload:** the inputs that stand for real use, their size, and a fixed seed.
3. **Metric:** one number, its unit, its direction (`max` or `min`), and how the benchmark computes it.
4. **Correct:** what a candidate must still do, checked against a reference the candidate cannot
   change (a frozen copy of today's code, golden outputs, or an invariant checker).
5. **Environment:** host or a Dockerfile, the hardware backend, and what one sample may cost in
   time (aim for 10 seconds to a few minutes).
6. **Rules:** what counts as cheating here (caching across samples, special-casing the
   benchmark's inputs, skipping work the checker does not see).

## 2. Write the task

Name it with one lowercase, hyphenated word or two. Write under `.vibesys/tasks/<task>/`:
`vibesys.input.toml`, `objectives.toml`, `OBJECTIVE.md`, the checker, the benchmark, any
reference data, and a `Dockerfile` if the environment needs one. Follow task-format.md exactly;
the CLI refuses an unknown key.

- The checker and the benchmark live in the task directory, so candidates cannot change them and
  the task's version (a digest of its files) changes when they do.
- The checker compares the candidate with the reference, not with itself, and exits 0 only on a
  pass. It covers what the benchmark exercises and the edge cases the workload skips.
- The benchmark runs the same workload every time, warms up before it times, and writes one JSON
  object with the metric to the path it is given.
- Add `/.vibesys-state/` to the repository's `.gitignore`: the CLI keeps its store there, and a
  candidate commit must never carry it.
- `OBJECTIVE.md` states the goal, the scope, the correctness bar and the rules from step 1, in
  plain words an optimizing agent reads before every plan.

## 3. Prove it

Commit the task on a branch (`git switch -c vibesys-task/<task>`) and run the recipes in
validation.md, all through `vibesys-cli` jobs:

1. The base passes the check, and five samples measure with a spread you can live with.
2. Each mutant (a broken or cheating variant of the code, committed on a throwaway branch) fails
   the check. Delete the throwaway branches afterwards.
3. A deliberate small slowdown is reported `worse` by `vibesys-cli compare`, so a real change
   of that size can be seen.

Fix the task and run the recipes again until all three hold. A task edit after the run starts
makes every saved result incomparable, so every fix happens now.

## 4. Hand back

Tell the user, in plain words: what the task optimizes and checks, the base number with its
samples and spread, what the mutants showed, any fact you assumed instead of asked, and the
command to start: `/vibesys:optimize <task>`. Leave the task committed on its branch; the user
decides where it merges. The optimization starts from a commit that contains the task.

## Rules

- Never weaken a check to make the base pass; fix the checker or ask.
- Never put the reference, the inputs or the expected outputs where a candidate may edit them.
- Never use the network in a check or a benchmark unless the workload is a network service the
  task starts itself.
- Report only numbers `vibesys-cli` printed (`vibesys:measure`).
- Run every workload (a build, a server, a check, a benchmark, a calibration sweep) on the task's
  target hardware, never on the machine that runs the agents: it is shared. Kill every process you
  or a subagent started before handing back, and say both in each subagent's brief for a task
  whose hardware is another machine.
