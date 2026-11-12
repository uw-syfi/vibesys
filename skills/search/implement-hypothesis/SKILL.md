---
name: implement-hypothesis
description: "Use when a vibesys optimize brief gives you one hypothesis to implement in your own git worktree. Makes the smallest change that yields a measurement, commits it, measures it with vibesys:measure (vibesys-cli jobs), and hands back the claim, the commit, the verdict, what was learned and the next step. Never edits .vibesys/ or the task's checker or benchmark."
---

# Implement a hypothesis

You own one hypothesis (a plan) in one worktree. You never write the plan; you submit jobs linked
to it. Your brief names the claim, the pass criteria, the parent commit, the scope, the CLI context
flags and your budget. Details of the steps and the handoff are in
[handoff.md](references/handoff.md).

**Which CLI.** Where this skill writes `vibesys-cli`, run `"$VIBESYS_CLI"`, never the one on
`PATH`: your shell's `PATH` may hold another build than the one this run means to use. A run
started by the orchestrator sets it. If it is unset or empty, stop and say `VIBESYS_CLI is not
set; export it as the absolute path of the vibesys-cli to use`.

## Start

1. Run the first command of your brief and confirm the worktree and branch. Do not go on if they
   differ. Everything you do happens in that worktree.
2. Read `OBJECTIVE.md` in the task directory. Its rules bind you, whatever a measurement says.
3. Load `vibesys:commit`, `vibesys:measure` and the domain skills the brief names.

## Work

1. **Start from the first measurable step**: the smallest change to the claim that you can
   finish and measure in this turn. If you cannot finish a step, do not stop on partial work:
   name the concrete blocker (the file, function, check or error) and the narrower step that
   would get past it.
2. Build, then run the cheapest check that covers what you changed. Once it builds and one local
   check passes, make the candidate commit and measure; do not polish first. After a failure, change the code
   before you run the check again. When output is long, write it to a file and read the file.
   When the claim is about the cost of one unit of work and a measure is expensive, first
   microbenchmark that unit on the parent and on your change. A change that does not move its own
   microbenchmark is not worth a measure: report that instead. The number is evidence for your
   handoff, never the task's metric.
3. Commit in your worktree as `vibesys:commit` says: often, with `wip` checkpoints as you go, and the
   candidate as a full commit with `Checks: pass`. A cheap check on a commit that needs no number:
   `vibesys-cli submit check <sha> --wait --plan <n>`. For the number, follow your brief: if it
   grants a budget, `vibesys-cli submit measure <sha> --samples 5 --plan <n>` and wait for it
   (`vibesys:measure`; a measure without `--plan <n>` is refused), otherwise return the SHA as
   `ready for measurement`. Then
   `vibesys-cli compare <sha>`, and add `--against <sha>` when you built on a candidate, to see the
   gain over your parent.
4. With the verdict in hand, build the next step on it, or stop.

Any run that drives load, including a short sanity or tuning run, is a `vibesys-cli submit command
<sha> --image harness -- <argv...>` job on a committed `wip` checkpoint (`vibesys:measure` says
which image; on a task with `[candidate]` the harness command runs beside the candidate's server,
and on another task `--service` and `--probe` start a server). Do not run benchmarks or servers in your own shell (unit tests and the in-process
microbenchmarks of Work step 2 are the exception), and do not queue more jobs than your
budget allows.

## Never

- Edit anything under `.vibesys/`, the task's checker or its benchmark.
- Amend, rebase or force-push a commit that was measured (`vibesys:commit`).
- Add a stub, a special case for the benchmark's inputs, a skipped check or any shortcut that
  raises a number without the program doing the work. Obey every rule in `OBJECTIVE.md`.
- Report a number that `vibesys-cli` did not print.

## Finish

Every turn ends the same way, in this order, and no turn ends any other way:

1. **Measure.** If your brief grants a budget and you have a candidate commit,
   `vibesys-cli submit measure <sha> --samples 5 --plan <n>`, wait for it, and run
   `vibesys-cli compare <sha>` (`vibesys:measure`). A commit you did not measure is
   `ready for measurement`, and your handoff says so. Prose about how fast it should be is not a
   measurement.
2. **Hand back**, in the format of [handoff.md](references/handoff.md): claim, commit SHA, job ids,
   the verdict (the `compare` output), what you learned, and the step you would take next. If you
   made no candidate: the blocker and the narrower step.
3. **Deliver the handoff through the harness's handoff tool, as the last thing you do.** In a run
   whose tools include `post_handoff`, call it with the handoff; your session closes when your
   turn ends and no mail waits for you, so a summary written as plain text reaches nobody. Where
   there is no such tool, the handoff is your final message.

Never end a turn with a prose summary in place of the handoff, with a measurement still to take,
or with the handoff written and not delivered. Before you stop, ask whether step 1 was done or
excused in the handoff, and whether step 3 was done.
