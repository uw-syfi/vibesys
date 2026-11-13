Review the candidate as a change to a native command-line program. A pass needs
evidence of preserved behavior and of a real measured improvement, from the
task's own commands, for the candidate as committed.

## Evidence a pass requires

- **Correctness.** The task's correctness check passed on the candidate build.
  Output bytes, error output, and exit status match for the checked cases, and
  the diff does not narrow behavior beyond them: options, locales, malformed
  input, error paths, recursion, and pipe behavior still work as before.
- **Active path.** The change is exercised by the measured invocations. A change
  to code the benchmark never reaches is not an improvement.
- **Honest implementation.** The speedup comes from doing the same work more
  cheaply. Reject canned output, special cases for benchmark names, paths,
  seeds, argument vectors, or environments, and detection of the measurement.
- **Measured improvement.** The task's recorded measurement shows the gain,
  comparable to the baseline and outside its noise. Local timings and profiles
  explain a result; they do not replace it.

## Reject or flag

- Changes to the benchmark, checker, objective, timeouts, or task scripts, and
  edits outside the permitted scope.
- Work moved outside the timed region, hidden precomputation, or a persistent
  cache the task did not authorize.
- Weakened error handling, ignored standard error, changed exit statuses, or
  skipped recursive or per-input work.
- Lost portability, undisclosed machine-specific compiler flags, or new
  dependencies without justification.
- A different program substituted for the one under test.

Record any limit on the evidence in the verdict instead of passing on trust.
