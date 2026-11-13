# What the evaluator proves, and what it does not

An optimization is only as trustworthy as the evaluator behind it. This reference
lists what a sound evaluator for a command-line program provides, so that an agent
can tell which claims the evaluator already backs and which ones it must check by
reading the diff, and so that a task author can see what to build. Read the task's
own description first: it says what its evaluator actually does.

## Properties a sound evaluator has

- **Every timed run is also a checked run.** The same invocation that is timed
  is compared with the expected behavior, so a program cannot behave differently
  "under the benchmark". There is no separate benchmark mode to detect.
- **The comparison covers the whole recorded behavior:** exit status or signal,
  standard output bytes, standard error bytes, the final state of files the
  original wrote, and no files written that the original did not write. If standard
  error is not compared, say so: that is a gap an agent must not use.
- **Candidate and baseline are interleaved** on the same machine, with the baseline
  rebuilt or recorded the same way, so drift cancels. The score is a ratio against
  the baseline, not an absolute time.
- **The environment is replayed**: arguments, working directory, environment
  variables, locale, input files and how input and output are connected.
- **The scope is enforced mechanically.** A check refuses any change outside the
  permitted source area, to the evaluator, or to the task's files, before building.
  A told rule that nothing enforces will eventually be broken by accident.
- **Evaluator inputs are protected or their integrity is checked.** A changed
  expected output, recorded invocation or baseline binary is detected.
- **A noise floor comes from measurement**, from running the baseline against itself,
  and a result must beat it. Sample counts, the subset and the seed are fixed by the
  task so every candidate faces the same workload.
- **Two tiers of measurement.** A quick tier is a smoke test and is diagnostic. The
  full tier, over the whole workload, is what supports a claim, with failures and
  errors at zero and an interval that excludes "no change".
- **Failures are loud.** A build error, a failed entry, a sandbox error or an
  unreproducible baseline gives no score, never a score with a warning.

## What the evaluator cannot prove

These stay the agent's and the judge's job, because a recorded workload does not
exercise them:

- Options, locales, encodings, input shapes and error paths the workload never uses.
- How much of its standard input a program reads, and its behavior as a pipeline
  consumer beyond the recorded cut-offs.
- File metadata (times, ownership), directory entry order, and anything the
  comparison deliberately ignores.
- Behavior on other platforms, other libraries and other CPUs than the evaluator's.
- Whether the change is honest. Reading the diff for special cases is the only check.

Close the gap with the project's own test suite run through the task's commands,
differential tests against the baseline ([semantic-preservation.md](semantic-preservation.md)),
and review ([review-checklist.md](review-checklist.md)).

## Proving that an evaluator works

Before trusting a task, run known-bad candidates and confirm each one is caught.
Useful kinds, each applied to a copy of the program:

| Candidate | Must be caught by |
| --- | --- |
| A wrong answer on a common option (off-by-one numbering, an ignored flag) | output comparison |
| Skipped work (recursion that skips some directories) | output comparison |
| A changed exit status for a normal outcome | exit-status comparison |
| A changed error message | standard-error comparison |
| Output that stops after some bytes when the output is a pipe | output comparison and the pipe rules |
| An edit outside the permitted scope, including the evaluator | the scope check, before building |
| An edit to the task's own scripts | the task-integrity refusal |
| A deliberate slowdown of about twice the measured spread | a `worse` verdict, which proves the measurement is sensitive |

The unmodified baseline must pass every check, and its repeated measurement gives the
noise floor. An evaluator that passes a known-bad candidate is not ready for a run.
