# Handoff

The lead acts on your handoff without seeing your work. Write it so it can be checked.

## Format

```
Plan: <n>      Outcome: candidate | no candidate
Claim: <the claim as you tested it; say if you narrowed it>
Commit: <full SHA>    Branch: <branch>    Parent: <full SHA>
Checks: <commands you ran and their result>
Measurement: <job ids, and the compare output for the commit with the base value>
Learned: <what the result says about the hypothesis, in one or two sentences>
Next: <the step you would take, and why>
Unapplied / open: <anything in the brief you did not do, and why>
```

Deliver it with `post_handoff` in a run that has it: `claim`, `learned`, `next` and `open_items`
are its common fields, and the rest goes in the `worker` field, as `candidate` (`commit`,
`parent_commit`, `branch`, `checks`, `measurement`) when you produced a commit, or as
`no_candidate` (`blocker`) when you did not.

## Rules for what you may claim

- A gain is a `better` verdict. `within_noise` is no gain; say so. `inconclusive` means you
  needed more samples: take them only within the brief's budget, then report inconclusive
  if unresolved. Identical readings on both sides cannot be resolved by repeating them.
- A claim about speed or accuracy rests on a `vibesys-cli` measurement of a commit that contains
  the change. A local timing is a hint for you, not evidence.
- If a check fails on your commit, the candidate is not a win whatever the benchmark says.

## When there is no candidate

Say which turn ended without one and why: the exact file, function, check or error. Name the
narrower step that would get past it. The lead narrows the brief from this; a vague blocker gets
the direction parked.

## Evidence

Keep long output out of the handoff: give the job id (`vibesys-cli logs <job>` reads it) or the path
of a file in the scratch directory your brief names. The handoff stays short.
