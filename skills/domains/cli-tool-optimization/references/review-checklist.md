# Review and anti-gaming checklist

Use this before submitting a change and when reviewing one. The aim is to catch a
result that is fast but not honest, and a change that is honest but narrower than
it looks. When something on this list applies, reject the change or flag it
explicitly with the evidence; do not wave it through.

## How to review

1. Read the whole diff, including build files, scripts and anything added. Ask of
   every conditional: would this behave the same for a different file name, path,
   seed, argument list, input or environment?
2. Search the diff for literals that look like they came from the benchmark:
   file names, directory names, option combinations, magic sizes, expected output.
3. Ask where the work now happens. Is it still done, in the measured process, in
   the timed region, every run?
4. Run the correctness check and the wider tests from
   [semantic-preservation.md](semantic-preservation.md).
5. Confirm the claimed improvement in the task's recorded measurement, not only in
   your own timings ([measurement.md](measurement.md)).

## Reject or flag

**Special-casing the benchmark**

- Branches on a file name, a path, a seed, input contents, an argument vector or
  an environment variable that only the benchmark uses.
- Detecting that the program is being measured or tested, and behaving
  differently when it is.
- Canned or prerecorded output, or a lookup of results keyed on the benchmark's
  inputs.

**Moving or hiding work**

- Precomputation done at build time, install time or first run from benchmark data,
  and then reused.
- A persistent cache, daemon, server process, shared memory or temporary file that
  carries results between runs, unless the task explicitly authorizes it.
- Work shifted out of the timed region: into a wrapper, a preload, a build step, a
  background process, or the evaluator's set-up.
- Skipped recursive or per-input work that the original performed.

**Weakening behavior**

- Weakened error handling, swallowed errors, ignored or silenced standard error,
  or changed exit statuses.
- Narrowed semantics: an option no longer honored, a locale or encoding path
  dropped, a rare case no longer handled.
- Altered signal, timeout or broken-pipe behavior.

**Touching what you must not touch**

- Modifications to the benchmark, the validator or correctness check, the objective,
  the recorded cases or expected outputs, timeouts, or task scripts.
- Edits outside the permitted source scope.
- Substituting a different program (another implementation, a system binary, a
  wrapper that calls one) unless the objective explicitly allows it.
- Exploiting a gap in the evaluator (something it does not check) instead of
  improving the implementation.

**Portability and dependencies**

- Undisclosed machine-specific compiler flags or instructions, such as a build that
  only runs on the build machine's CPU.
- Lost portability across platforms, compilers, libc or locales the project
  supported before, unless the objective permits it.
- New dependencies or vendored code without a justification, a disclosed license
  consideration, and a measured benefit.
- Profile-guided builds whose profile comes from the scored invocations.

## What to write down when you submit

- The hypothesis and the mechanism that changed.
- The correctness evidence: which checks passed, and what you tested beyond them.
- The recorded measurement, and the local diagnostic evidence kept separate.
- Anything on the lists above that applies, stated plainly: new dependencies,
  machine-specific assumptions, semantic differences, limits of what was tested.

A reviewer who finds something you did not disclose will trust the rest less.
