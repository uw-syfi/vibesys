# Handoff

The lead acts on your handoff without seeing your work. Write it so it can be checked.

## Format

```
Hypothesis: <id>      Outcome: candidate | no candidate
Claim: <the claim as you tested it; say if you narrowed it>
Commit: <full SHA>    Branch: <branch>    Parent: <full SHA>
Checks: <commands you ran and their result>
Measurement: <the compare output for the commit, and the base value; the records are in the
  run directory>
Learned: <what the result says about the hypothesis, in one or two sentences>
Next: <the step you would take, and why>
Unapplied / open: <anything in the brief you did not do, and why>
```

## Rules for what you may claim

- A gain is a `better` verdict. `within_noise` is no gain; say so. `inconclusive` means you
  needed more samples: take them before you report.
- A claim about speed or accuracy rests on a recorded measurement of a commit that contains the
  change. A local timing is a hint for you, not evidence.
- If a check fails on your commit, the candidate is not a win whatever the benchmark says.

## When there is no candidate

Say which turn ended without one and why: the exact file, function, check or error. Name the
narrower step that would get past it. The lead narrows the brief from this; a vague blocker
gets the direction parked.

## Evidence

Keep long output in files in the run directory and give their paths. The handoff stays short.
