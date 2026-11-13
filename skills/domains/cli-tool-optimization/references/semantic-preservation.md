# Semantic preservation

A command-line program's behavior is everything an invoker or a script can
observe. An optimization may change how fast the program does its job. It may not
change what the job is.

## The observable interface

Treat each of these as part of the contract:

- **Arguments and options**: every option, abbreviation, ordering rule, `--`
  handling, option arguments, and how invalid usage is reported.
- **Standard input**: read, not read, partially read, interactive or piped, and
  behavior at end of input or when input is a terminal.
- **Standard output bytes**: exact content, ordering, line endings, buffering
  that is visible to a pipeline, and behavior when output is a terminal versus a
  pipe or file.
- **Standard error bytes**: exact message text including the program name,
  ordering relative to output, and when nothing is printed.
- **Exit status** for every outcome, and **signals** the program raises, handles
  or dies from.
- **Timeouts and long-running behavior**: interruptibility, responsiveness to
  signals, behavior on closed or full output.
- **Filesystem reads and writes**: which files are opened, in what mode, which are
  created or modified, atomicity, permissions, ownership, timestamps and other
  **metadata**, and other **side effects**.
- **Locale and environment variables**: collation, character encoding, number and
  date formatting, and variables that change behavior (for example locale
  selection, terminal and color settings, `TZ`, and compatibility switches).
- **Malformed and hostile inputs**: invalid encodings, NUL bytes, very long lines,
  empty input, enormous input, binary data, missing or unreadable files,
  directories where files are expected, loops and dangling links.
- **Determinism**: output order for directory listings, hash-based containers and
  concurrent work; a faster change must not introduce run-to-run variation.
- **Pipelines**: behavior as a producer and as a consumer, and **broken-pipe**
  behavior (does the program die quietly from the signal, report an error, or
  carry on) because many scripts depend on it.
- **Recursive traversal**: order, depth handling, symlink and mount policies,
  error continuation, and what is skipped.

## Recorded cases versus complete semantics

The evaluator checks the cases it recorded: those invocations and those inputs.
That is necessary, and it is not enough. A change can pass every recorded case and
still be wrong on the next invocation: a fast path that assumes ASCII, an option
that stopped being honored, an error that is no longer reported, a file that is
no longer closed or flushed. Both standards matter: match the recorded cases
because that is what is proven, and preserve complete program semantics because
that is what users depend on, even where nothing proves it directly.

## How to test beyond the benchmark

- **Differential testing against the unmodified baseline build.** Run baseline and
  candidate on the same invocations and compare stdout, stderr, exit status and
  filesystem effects. Extend well past the scored cases: every option, the
  locales, malformed and empty inputs, missing files, permission errors.
- **Generated and randomized differential tests.** Vary arguments and inputs under
  a fixed seed and compare. Include sizes at buffer and block boundaries.
- **The project's own test suite**, where one exists (run it through the task's
  commands or the project's documented target). Run it on a clean build of the
  candidate.
- **Sanitizer builds** (address and undefined-behavior checkers) on the tests, to
  catch memory errors and undefined behavior that an optimization may expose. Do
  not time a sanitizer build.
- **Error paths** by forcing failures: unreadable files, a full or closed output,
  a closed input, interrupted calls, out-of-memory where practical.
- **Held-out cases.** If the task provides cases it does not score, run them. If it
  does not, build your own, and keep them out of anything you tune against.
- **Reason about reach.** For each function you changed, list the behaviors it
  influences, then test those, including the ones the benchmark never touches.

## Rules of thumb

- A change that is faster because it does less work is suspect until you can say
  which work, and why the result is unaffected.
- Do not trade semantics for speed unless the objective says you may. If a
  difference is unavoidable (for example, ordering that was never guaranteed),
  state it explicitly in your record so a reviewer can decide.
- Where the program is specified by a standard or documentation, the specification
  is the contract; where it is not, current behavior is.
- Never fix a correctness failure by editing the check, the recorded cases, or the
  expected output.
