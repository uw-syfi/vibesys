# Review in detail

## Reading the diff

- Read the whole diff, including tests, build files and scripts. A gaming change often sits in a
  file that looks unrelated.
- Find the benchmark's entry point and trace from it to the changed code. If you cannot reach
  the change, say so: that is a fail on "active".
- Look for: constants or branches keyed on benchmark inputs, seeds or sizes; results returned
  without computing them; timeouts, retries or checks relaxed; weakened correctness requirements; work moved outside the timed region; a second
  implementation that is used only when the benchmark runs.
- Check `git diff --stat <parent> <candidate> -- .vibesys` is empty.
- Check the commit contains what the implementer says it does, not more: unrelated edits are a
  reason to ask for a narrower candidate.

## Reading the measurement

`vibesys-cli show <candidate> --json` lists what was saved. Look at: the metric and direction are
the task's; the environment is the same as the parent's; the check passed; the sample count is
enough for the claim. `vibesys-cli compare` gives the verdict and the ratio; it refuses to mix
environments, and its message says why.

- `better`: the gain is real by the recorded samples.
- `within_noise`: no evidence of a gain. A claim of gain fails on "real".
- `worse`: the candidate is slower or lower than its parent. It fails.
- `inconclusive`: return `unmeasured` on Real. The lead decides whether another measurement
  fits the budget; identical readings on both sides cannot be settled by repeating them.

## Verdict format

```
Verdict: pass | fail | unmeasured
Candidate: <SHA>      Parent: <SHA>
Correct: yes/no - <reason>
Active: yes/no - <the path from the benchmark to the change>
Honest: yes/no - <reason>
Real: yes/no/unmeasured - <the compare output>
Reasons: <the shortest list that explains the verdict>
```

A pass needs every line to be yes. One no is a fail. `unmeasured` on "Real" is neither: say what
measurement would settle it.
