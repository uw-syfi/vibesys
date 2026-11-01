---
name: judge-candidate
description: "Use when a vibesys optimize brief asks you to independently review a candidate commit that claims a gain, before the lead treats it as a win. Read-only: compares the diff with its parent against the claim, checks that the change is on the measured path, looks for gaming of the check or benchmark, and returns pass, fail or unmeasured with reasons."
---

# Judge a candidate

You decide whether a candidate's claim is supported. You did not write it and you edit nothing.
The checklist and the verdict format are in [review.md](references/review.md).

Harness-specific mechanics (tools, background tasks, script paths): read `references/harness/<harness>.md` for the harness you are running in.

## Start

Run the first command of your brief and confirm the worktree. It is a checkout of the candidate
commit. Read `OBJECTIVE.md` in the task directory for context. You never edit plan files; layout: `vibesys:state`. Load `vibesys:measure`.

## Decide

Start from the diff against the parent (`git diff <parent> <candidate>`) and the claim. Answer:

1. **Correct?** Does the code do what the claim says, and does it still do everything it did?
   Run the task's declared correctness command yourself, in your own candidate worktree,
   using your own allocated port. Worker evidence is not a substitute. Coordinate the slot
   with the lead. Return `unmeasured` if the budget cannot cover the check.
   Start and clean up services with the process-group owner below.
2. **Active?** Is the change on the path the benchmark runs? A change that the benchmark never
   reaches cannot explain a gain.
3. **Honest?** Does it special-case the benchmark's inputs, cache on its seed, skip or weaken a
   check, stub work, or touch `.vibesys/`, the checker or the benchmark?
4. **Real?** Is the gain beyond noise? The recorded verdict decides this:
   Run the script `measurements.py` from the measure skill's `scripts/` directory:
   `python3 measurements.py compare --dir <dir> --commit <candidate> --base <parent> --json` yourself and read its output.
   Confirm the full commit SHA and recorded samples support the claimed gain; worker text
   is not measurement evidence. Fail a claim
   about speed only when a recorded result shows it is not there. If no comparable verdict
   exists, return `unmeasured` and ask the lead to schedule measurement. Never run a long
   measurement in the judge; the lead records it before requesting a new judgment.
   A correctness-only pass is `unmeasured`, never an unconditional `pass`.

Source reading decides correctness and benchmark integrity. Only a recorded measurement decides a
claim about speed. Do not run other benchmarks. The required correctness check is the exception to the short-command budget.

Report any declared objective limits seen in the logs; they never block the verdict.


Use the measure skill's `scripts/service.py` owner for every required service. Supply
`--service`, `--probe`, and `--client` as JSON argv arrays, with a positive `--limit`.
It starts a new process group, fails on exhausted readiness, and stops and verifies the
whole group on every exit. Never use handwritten background-shell PID or startup-sleep recipes.
Run any load, including short sanity/tuning benchmarks, through
the script `measurements.py` from the measure skill's `scripts/` directory: `python3 measurements.py run -- <command>`.
This host-wide lock applies to recorded and unrecorded benchmarks alike.

## Return

`pass`, `fail` or `unmeasured`, and the reasons, each tied to a file and line or a recorded
number.
