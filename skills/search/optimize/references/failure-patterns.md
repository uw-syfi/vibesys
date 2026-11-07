# Failure patterns

Read before planning. Each pattern says what it looks like and what to do.

**Chasing noise.** A candidate "wins" by 1-3% on one run. Trust only a `better` verdict from
`vibesys-cli compare`. For a close call, take more samples of the commit
(`submit measure --samples 5 --plan <n>`); do not reword the claim.

**Measuring a commit without the change.** The change is not on the path the benchmark runs (a
flag, a registration, an import that is really used), or the commit lacks a file the change
needs. The CLI runs the exact commit, so what is not committed is not measured. Check that the
change is on the path the benchmark runs.

**Gaming the benchmark.** A special case for the benchmark's inputs, a cache keyed on the
benchmark's seed, a skipped check, a stubbed component, a lowered precision the objective
forbids. The judge reads the diff for this and for every rule in OBJECTIVE.md. A fast number
with a failing or weakened check is a fail.

**Losing results to context limits.** Findings that stay in your head are gone after a
compaction. Put each decision, result and expectation in the plan as a comment as it happens.

**A merge measured against the wrong head.** The result branch moved while a combine was running.
Base every combine on the exact head SHA you name, and compare the result with
`vibesys-cli compare <sha> --against <head SHA>`. A promotion the CLI refuses as stale needs a
fresh measurement against the new head, not a retry. A candidate that does not contain the new
head (a `conflict` failure) needs a combine onto that head and a new measurement.

**Parallel fragments of one idea.** Three subagents each doing a third of one change. Plan
hypotheses that can each succeed or fail alone.

**Reassigning a stuck step unchanged.** If a subagent ended without a candidate, its brief was
too large. Name the exact blocker and give the narrower step it proposed. Two tries in a row
without convergence: park the direction and say why.

**Idle slots.** A free slot with no reason. Dispatch a new planner or a worker branched from the
winner, or state why not in the next report. Re-decide after an item returns or the head moves.

**Resubmitting by reflex.** A wait that exited 4 or 3, or a job that looks stuck, gets a second
submission. Exit 4 is no answer, not a failed job. Run `vibesys-cli jobs --json` or `show` first;
the job may be finished, queued or still running.

**Piling up the queue.** Several workers each submitting measures at once; the last waits an hour,
since a measure holds its placement alone however many jobs the daemon runs at once. Give each
brief a budget, and read `vibesys-cli jobs` (it says why each queued job waits) before you fund
more.

**Long waits in the foreground.** A 25-minute `wait` in your own turn. Use the tracked background
recipe in `vibesys:measure` and go back to the other slots.

**Trusting a handoff.** "Tests pass" without a job, "measured" without a verdict. Run
`vibesys-cli show <SHA>` and read it.
