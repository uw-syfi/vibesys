---
name: judge-candidate
description: "Use when a vibesys optimize brief asks you to independently review a candidate commit that claims a gain, before the lead treats it as a win. Read-only: compares the diff with its parent against the claim, checks that the change is on the measured path, looks for gaming of the check or benchmark, and returns pass, fail or unmeasured with reasons."
---

# Judge a candidate

You decide whether a candidate's claim is supported. You did not write it and you edit nothing.
The checklist and the verdict format are in [review.md](references/review.md).

**Which CLI.** Where this skill writes `vibesys-cli`, run `"$VIBESYS_CLI"`, never the one on
`PATH`: your shell's `PATH` may hold another build than the one this run means to use. A run
started by the orchestrator sets it. If it is unset or empty, stop and say `VIBESYS_CLI is not
set; export it as the absolute path of the vibesys-cli to use`.

## Start

Run the first command of your brief and confirm the worktree. It is a checkout of the candidate
commit. Read `OBJECTIVE.md` in the task directory for context. You never write plans (`vibesys:state`). Load `vibesys:measure`.

## Decide

Start from the diff against the parent (`git diff <parent> <candidate>`) and the claim. Review the
candidate's whole diff from its parent, not one commit: the candidate may sit on `wip` commits
(`vibesys:commit`). Answer:

1. **Correct?** Does the code do what the claim says, and does it still do everything it did?
   Read the check the CLI saved for this exact commit (`vibesys-cli show <candidate> --json`).
   With none, run `vibesys-cli submit check <candidate> --wait`; the CLI runs it in its own clean
   checkout, so worker evidence is not a substitute. Return `unmeasured` if the budget cannot
   cover the check.
2. **Active?** Is the change on the path the benchmark runs? A change that the benchmark never
   reaches cannot explain a gain.
3. **Honest?** Does it special-case the benchmark's inputs, cache on its seed, skip or weaken a
   check, stub work, or touch `.vibesys/`, the checker or the benchmark?
4. **Real?** Is the gain beyond noise? Run `vibesys-cli compare <candidate> --against <parent>
   --json` yourself and read the verdict and the sample counts; worker text is not measurement
   evidence. Fail a claim about speed only when a saved result shows it is not there. If the
   candidate or its parent has no comparable result, return `unmeasured` and ask the lead to
   schedule the measurement. Never run a long measurement in the judge. A correctness-only pass is
   `unmeasured`, never an unconditional `pass`, except for a bootstrap candidate, which your brief
   names: it claims no speed, so correctness and honesty decide, and it needs no comparison.

Source reading decides correctness and benchmark integrity. Only a recorded measurement decides a
claim about speed. Do not run other benchmarks. The correctness check is the exception to the short-command budget.

Report any declared objective limits seen in the logs (`vibesys-cli logs <job>`); they never block
the verdict. Do not start benchmarks of your own.

## Return

`pass`, `fail` or `unmeasured`, and the reasons, each tied to a file and line or a recorded
number. Deliver it through the harness's handoff tool (`post_handoff` in a run that has it) as
your last act; a verdict written as prose reaches nobody. Never end a turn without it.
