---
name: implement-hypothesis
description: "Use when a vibesys optimize brief gives you one hypothesis to implement in your own git worktree. Makes the smallest change that yields a measurement, commits it, measures it with vibesys:measure, and hands back the claim, the commit, the verdict, what was learned and the next step. Never edits .vibesys/ or the task's checker or benchmark."
---

# Implement a hypothesis

You own one hypothesis (a plan) in one worktree. You never edit plan files. Your brief names the claim, the pass criteria, the
parent commit and the scope. Details of the steps and the handoff are in
[handoff.md](references/handoff.md).

## Start

1. Run the first command of your brief and confirm the worktree and branch. Do not go on if they
   differ. Everything you do happens in that worktree.
2. Read `OBJECTIVE.md` in the task directory. Its rules bind you, whatever a measurement says.
3. Load `vibesys:measure` and the domain skills the brief names.

## Work

1. **Start from the first measurable step**: the smallest change to the claim that you can
   finish and measure in this turn. If you cannot finish a step, do not stop on partial work:
   name the concrete blocker (the file, function, check or error) and the narrower step that
   would get past it.
2. Build, then run the cheapest check that covers what you changed. Once it builds and one local
   check passes, commit and measure; do not polish first. After a failure, change the code
   before you run the check again. When output is long, write it to a file and read the file.
3. Commit in your worktree. If your brief grants no measurement slot, return the SHA as
   `ready for measurement`; the lead measures it before judging it. Otherwise measure that commit with `vibesys:measure`: the correctness
   check, then at least 5 benchmark samples, recorded in the state directory (`--dir`; `vibesys:state`). Compare with the
   base: `compare --commit <sha>`. Add `--base <sha>` when you built on a candidate, to see the
   gain over your parent.
4. With the verdict in hand, build the next step on it, or stop.


Use the measure skill's `scripts/service.py` owner for every required service. Supply
`--service`, `--probe`, and `--client` as JSON argv arrays, with a positive `--limit`.
It starts a new process group, fails on exhausted readiness, and stops and verifies the
whole group on every exit. Never use handwritten background-shell PID or startup-sleep recipes.
Run any load, including short sanity/tuning benchmarks, through
`python3 "${CLAUDE_SKILL_DIR}/../measure/scripts/measurements.py" run -- <command>`.
This host-wide lock applies to recorded and unrecorded benchmarks alike.

## Never

- Edit anything under `.vibesys/`, the task's checker or its benchmark.
- Add a stub, a special case for the benchmark's inputs, a skipped check or any shortcut that
  raises a number without the program doing the work. Obey every rule in `OBJECTIVE.md`.
- Report a number that is not in the measurement record.

## Hand back

Claim, commit SHA, the verdict (the `compare` output), what you learned, and the step you would
take next. If you made no candidate: the blocker and the narrower step.
