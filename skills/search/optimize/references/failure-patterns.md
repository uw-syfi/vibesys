# Failure patterns

Read before planning. Each pattern says what it looks like and what to do.

**Chasing noise.** A candidate "wins" by 1-3% on one run. Trust only a `better` verdict from
`vibesys:measure compare`, which needs 3 samples a side and a gap beyond the spread. For a close
call, take more samples of the commit; do not reword the claim.

**Measuring a commit without the change.** The subagent measured its working copy, or a
different commit, or a build that did not include the edit. Always measure a clean worktree of
the exact commit, and check that the change is on the path the benchmark runs (a flag, a
registration, an import that is really used).

**Gaming the benchmark.** A special case for the benchmark's inputs, a cache keyed on the
benchmark's seed, a skipped check, a stubbed component, a lowered precision the objective
forbids. The judge reads the diff for this and for every rule in OBJECTIVE.md. A fast number
with a failing or weakened check is a fail.

**Losing results to context limits.** Findings that stay in your head are gone after a
compaction. Put each decision, result and expectation in the plan file as it happens.

**A merge measured on the wrong commit.** Main moved while a combine was running, or the
combine measured a branch tip that was not its commit. Merge one at a time, base every combine
on the exact main SHA you name, and compare the result against main's records with
`compare --base <main SHA>`.

**Parallel fragments of one idea.** Three subagents each doing a third of one change. Plan
hypotheses that can each succeed or fail alone.

**Reassigning a stuck step unchanged.** If a subagent ended without a candidate, its brief was
too large. Name the exact blocker and give the narrower step it proposed. Two tries in a row
without convergence: park the direction and say why.

**Idle slots.** A free slot with no reason. Dispatch a new planner or a worker branched from the winner, or state
why not in the next report. Re-decide after an item returns or main moves.

**Long measurements in the foreground.** A 25-minute benchmark loop in your own turn. Use the tracked Bash recipe in `vibesys:measure` and go back to the other slots.

**Trusting a handoff.** "Tests pass" without a command, "measured" without a record. Run
`measurements.py show --dir <state dir> --commit <SHA>` and read it.
