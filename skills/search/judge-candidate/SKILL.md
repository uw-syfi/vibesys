---
name: judge-candidate
description: "Use when a vibesys optimize brief asks you to independently review a candidate commit that claims a gain, before the lead treats it as a win. Read-only: compares the diff with its parent against the claim, checks that the change is on the measured path, looks for gaming of the check or benchmark and for broken rules in OBJECTIVE.md, and returns pass, fail or unmeasured with reasons."
---

# Judge a candidate

You decide whether a candidate's claim is supported. You did not write it and you edit nothing.
The checklist and the verdict format are in [review.md](references/review.md).

## Start

Run the first command of your brief and confirm the worktree. It is a checkout of the candidate
commit. Read `OBJECTIVE.md` in the task directory: its rules decide pass or fail as much as any
measurement does. Load `vibesys:measure`.

## Decide

Start from the diff against the parent (`git diff <parent> <candidate>`) and the claim. Answer:

1. **Correct?** Does the code do what the claim says, and does it still do everything it did?
   Run the task's declared correctness command yourself, in your own candidate worktree,
   using your own allocated port. Worker evidence is not a substitute. Coordinate the slot
   with the lead. Return `unmeasured` if the budget cannot cover the check.
   Start any required service yourself, save its PID, and stop only that PID in cleanup.
   Never use `pkill -f` or `pkill -x`: process names do not establish ownership.
2. **Active?** Is the change on the path the benchmark runs? A change that the benchmark never
   reaches cannot explain a gain.
3. **Honest?** Does it special-case the benchmark's inputs, cache on its seed, skip or weaken a
   check, stub work, or touch `.vibesys/`, the checker or the benchmark?
4. **Within the rules?** Does it break any rule or constraint that `OBJECTIVE.md` states?
5. **Real?** Is the gain beyond noise? The recorded verdict decides this:
   `measurements.py compare --run <dir> --commit <candidate> --base <parent>`. Fail a claim
   about speed only when a recorded result shows it is not there. If no comparable verdict
   exists, return `unmeasured` and ask the lead to schedule measurement. Never run a long
   measurement in the judge; the lead supplies its result before requesting a new judgment.

Source reading decides correctness and rule violations. Only a recorded measurement decides a
claim about speed. Do not run other benchmarks. The required correctness check is the exception to the short-command budget.

Passing the check is not enough: reject a candidate that violates a rule in `OBJECTIVE.md`.

## Return

`pass`, `fail` or `unmeasured`, and the reasons, each tied to a file and line or a recorded
number.
