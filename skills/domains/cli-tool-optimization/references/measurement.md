# Measurement

How to tell whether a command-line program got faster, and how much weight a
number can carry. The task's authoritative evaluator produces the results that
count; this reference is for the diagnostic measurements you take along the way
and for reading the evaluator's output critically.

## What a local number is worth

A timing from your own loop is **diagnostic evidence**. It helps you pick a
hypothesis, size an effect, and notice a regression early. It is not promotion
evidence, because your loop differs from the evaluator in input selection, cache
state, process launch path, output sink, environment, and statistics. Take the
evaluator's recorded measurement for any claim, and do not impose a statistical
method of your own on top of the task's.

Every timed result needs validated output behind it. A harness that records only
elapsed time will reward a program that exits early, prints nothing, or crashes.
Check the exit status and the output, or run the correctness check, alongside
every timing you intend to believe.

## CPU time and wall-clock time

- **Wall-clock** time is what a user waits for. It includes waiting on disk,
  pipes, child processes and the scheduler, and it is the noisier of the two.
- **CPU time** (user plus system) is steadier and shows work done, but it hides
  waiting. It sums across threads and across children the process waited for, so
  it can exceed wall time. A child that is never waited for, or that outlives the
  process, is not counted at all, so detaching work hides it.
- Record both. A large gap between wall and CPU means the program waits (I/O,
  pipes, locks, sleeps); a CPU time that matches wall means the work is on-CPU.
- Use the metric the task scores. Optimizing the other one is not progress.

## Process start-up is part of the measurement

For a command that finishes in a few milliseconds, most of the time can be spent
before and after the interesting work: `execve`, the dynamic loader resolving
shared libraries and relocations, runtime and locale initialization, reading
configuration, parsing arguments, allocator set-up, and teardown. This cost is
fixed per invocation and puts a floor under every run.

- Measure the floor: run the same program with trivial input or a no-op option.
- Inspect dynamic loading with the loader's own diagnostics where available
  (for example `LD_DEBUG=statistics` on Linux).
- Compare against trivial programs run the same way. A minimal statically linked
  program that exits at once measures the harness and `execve` floor, which no
  change to your program can go below. A minimal dynamically linked one (for
  example one that reads a tiny file) adds loader, libc and locale start-up, which
  your program also pays and may be able to reduce. Use the first as the floor and
  the second as a reference point; do not read the second as the floor, or the
  start-up headroom looks far smaller than it is.
- Estimate the ceiling of any per-input change: the saving cannot exceed the
  per-input share of the measured time, and a program that is mostly start-up
  gains little from a faster inner loop.

## Warm-up and cache state

Page cache, directory and inode caches, CPU caches and frequency scaling all
change a timing. Decide which state the workload really has (cold files or warm,
fresh process each time) and reproduce that state, rather than whichever is
convenient. Do not mix states within one comparison. Prepare inputs outside the
timed region, run one or more untimed warm-up repetitions when the evaluator
does, and say what you did.

## Which measurement supports a claim

The task says which measurement supports a claim. In a loop with a job system that
is usually the verdict comparing the candidate's measure job with its parent's, plus
any extra gate the objective names (for example a pass over the whole workload with
zero failures). Evaluators often also offer a quick tier (a small subset, few
repetitions): it is a smoke test that shows nothing broke and the effect has the
right sign, never a claim. Measurements are expensive and may be budgeted, so spend
them on hypotheses whose ceiling is above the noise floor.

## How the score aggregates

Find out how the task turns many invocations into one number, because it decides
what is worth optimizing. A total of times rewards the long invocations. A
geometric mean of per-invocation candidate-over-baseline ratios weighs every
invocation equally, so a 2% saving in start-up shared by all of them can beat a
large saving in a few. Estimate ceilings and compare local results in that same
aggregate, not in raw seconds.

## Comparing a candidate with the baseline

In a loop where all load goes through jobs, a timing run of the whole program is
load: run it as a job, or only the way the objective allows (for example under the
task's machine lock). Only an in-process microbenchmark of one unit runs in your own
shell. The method below applies wherever you are allowed to time.

- **Interleave** baseline and candidate runs (A, B, A, B, ...) and alternate which
  goes first, so drift in machine load, temperature or cache state hits both.
- **Pair** runs on identical inputs and environment; compute a ratio per pair.
- **Repeat** enough times that the spread is small relative to the effect you
  want to detect. A few repetitions of a millisecond-scale process say little.
- Report a **median** (robust to outliers) or a **geometric mean of ratios**
  across invocations (so one long invocation does not dominate), and the spread.
  For a ratio, give an interval, such as a bootstrap interval over the paired
  ratios, not a single point.
- Know the **empirical noise floor**: compare the baseline with itself (an A/A
  run) under the same procedure. A candidate difference smaller than what A/A
  produces by chance is not a result.

## Millisecond-scale processes

Short processes need extra care. Timer resolution, process-creation cost and
scheduler placement are the same size as the signal. Time many launches and
divide, or use instruction counts (hardware counters where permitted, otherwise
callgrind), which are far steadier than time, as a cross-check. Instruction counts
cover user space only: take system time and page faults from the process's resource
usage (`/usr/bin/time -v` or `getrusage`), because fewer instructions is not less
time when faults or syscalls grow. Launching through a shell, wrapper or harness adds
its own cost to both sides; keep it identical.

## Contamination to rule out

- Background load, parallel builds, other tenants, thermal throttling.
- A different binary than you think: stale build, wrong path, different flags
  (confirm with the version string, a checksum or the build id).
- Different environment or locale, a different working directory, or a different
  output sink (a terminal, a pipe and a file can each behave differently).
- Tracing, sampling or instrumentation attached while timing, including your own
  profiling or builds running while the evaluator measures.
- A debug or sanitizer build timed against an optimized one.
- Inputs that changed between baseline and candidate runs.
- Code layout. Byte-identical or trivially different builds can time differently
  through code alignment, link order or page placement, often by a percent or more.
  A sub-percent gain needs a mechanism-level cross-check (instruction or syscall
  counts that move in line with the claim) and an A/A of identical builds, not only
  a timing.

## Reporting

State the command, the build identity, the inputs, the repetition count, the
metric, and the spread. Keep diagnostic numbers labelled as diagnostic, and quote
the evaluator's result separately.
