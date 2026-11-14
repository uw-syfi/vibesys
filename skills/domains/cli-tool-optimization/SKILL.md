---
name: cli-tool-optimization
description: >-
  Disciplined optimization of a locally built native command-line program (C,
  C++, Rust or similar) whose observable behavior must stay identical: the
  skill for tasks of the cli-tools domain. Activate when speeding up a CLI such
  as a text search, stream editor, directory lister, sorter, differ, or
  repository tool; when planning or bounding hypotheses for one, choosing which
  invocations to profile, separating process start-up from per-input work,
  deciding whether a measured gain is real, or reviewing a change for narrowed
  semantics, benchmark-specific code, portability loss, or work moved out of
  the timed region.
---

# cli-tool-optimization

A process for making a native command-line program faster without changing what
it does. It is not a list of low-level tricks: what to change comes from
profiles of the workload you are scored on, and whether the change counts comes
from the task's own evaluator.

Three things hold throughout:

- **The contract is the whole observable interface**, not the cases the task
  measures. A faster program that behaves differently elsewhere is a regression.
- **The task's evaluator is authoritative.** Your local timings and profiles
  explain a result; only the measurement the task names supports a claim.
- **One mechanism per experiment.** Change one thing, so a result can be
  attributed and a failure can be learned from. The exception is a bundle of
  mechanisms that are each too small to measure alone (step 10).

## What to find in the task first

Before planning, find these in the task's objective. Where it is silent, take the
conservative reading as the rule and raise the question in your plan or report.

- The editable scope, and the build, correctness and measurement commands.
- The metric and how it aggregates invocations (a sum of times, or a geometric
  mean of per-invocation ratios).
- Which measurement supports a claim, and any extra gate before a result counts.
- The policy on compiler and linker flags, hardening, static linking,
  profile-guided builds, machine-specific flags and new dependencies.
- Whether standard error, file metadata and how much input is read are compared,
  and whether standard output and standard error share one stream.
- How to reproduce and profile one recorded invocation, and where CPU-heavy local
  runs are allowed.

## Workflow

**Frame the problem**

1. Establish the observable program contract: the options, inputs, outputs,
   error behavior and side effects the program has today
   ([semantic-preservation.md](references/semantic-preservation.md)).
2. Establish the permitted edit scope and the task's build, correctness and
   measurement commands. Edits outside the scope, or to the evaluator, are never
   an optimization.
3. Understand the workload distribution: which invocations are scored, how many
   of each, on what inputs, and **how the score aggregates them**. A sum of
   times is dominated by the long invocations; a geometric mean of
   per-invocation ratios counts a 1 ms invocation as much as a 3 s one.
4. Select representative invocations, weighted the way the score weights them,
   and reproduce them exactly: arguments, working directory, environment,
   locale, inputs, how standard input and output are connected, inherited signal
   dispositions, umask, open descriptors and resource limits. Prefer the
   evaluator's own way of running one recorded invocation; a hand-made
   reproduction usually differs in libraries, locale data, filesystem and cache
   state, so say how yours differs
   ([profiling.md](references/profiling.md)).

**Baseline and diagnosis**

5. Establish a correct baseline: build with the task's command, pass the
   correctness check, and record the baseline measurement
   ([measurement.md](references/measurement.md)).
6. Profile before modifying code.
7. Separate fixed start-up cost from input-dependent cost. A run on trivial
   input shows the floor no per-input optimization can go below.
8. Identify the dominant cost center, whether it lies in code you may edit or in
   a library you may not, and classify the workload as CPU-bound,
   syscall-bound, I/O-bound, allocation-bound, or start-up dominated. The
   [workload archetypes](references/workload-archetypes.md) say which questions
   fit which kind of workload.

**Experiment**

9. Form one falsifiable hypothesis: this mechanism costs X, this change removes
   Y, so this measurement moves by about Z.
10. Estimate the ceiling in the score's own terms. In a geometric mean of
    ratios, saving s_i on an invocation of time t_i gives a score of the
    geometric mean of (t_i - s_i) / t_i, so a gain close to the average of
    s_i / t_i when the savings are small. This estimate is the bound to give when
    the loop asks a plan for one. A mechanism whose ceiling is below the noise
    floor cannot be shown alone: bundle it with related mechanisms into one
    candidate above the floor (name each part, and check each with a steadier
    proxy such as instruction or syscall counts), or drop it.
11. Make the smallest real implementation change. Prefer a change a reviewer can
    read and verify in one sitting.
12. Rebuild through the task-provided commands, not a private build. Build
    changes are experiments of their own
    ([native-builds.md](references/native-builds.md)).

**Verify and record**

13. Run the correctness checks before trusting any timing. Faster and wrong is a
    failure, and a crash is fast. Before the first measurement run the task's
    check and a quick differential test of the behavior you changed; after a
    favorable verdict, and before handing the candidate to a judge, run the full
    differential, sanitizer and project test-suite pass
    ([semantic-preservation.md](references/semantic-preservation.md)).
14. Use the measurement the task names for any claim of improvement. Treat
    local microbenchmarks as diagnostic evidence only.
15. Review the change for narrowed semantics, benchmark specialization,
    portability loss, new dependencies, and work moved outside the timed region
    ([review-checklist.md](references/review-checklist.md)).
16. Record why the experiment succeeded or failed, and what it ruled out.

## Using it in a multi-agent loop

When planners, workers, judges and profilers are separate agents, each reads this
skill for its own part. The loop's lead owns steps 2 and 5 already.

- **Planner:** steps 3 to 10. Plan hypotheses from profiles and ceilings, state
  each bound honestly, and do not re-propose one the record shows failed.
- **Worker:** steps 9 to 16 on one hypothesis. Commit a small change, review it
  against the [review checklist](references/review-checklist.md), and hand back
  the evidence, including the wider tests of step 13.
- **Judge:** step 15, read-only. Read the whole diff with the review checklist;
  judge correctness and honesty from the source, the saved check and the
  worker's test evidence, and speed only from a recorded measurement.
- **Profiler:** steps 4 to 8, ending in a named cost center and its share.

If the task requires that all measurement and load go through its job system, a
timing or profiling run of the whole program is load: run it as a job, or only
the way the task allows (for example under the task's machine lock), and never
alongside a running measurement, which it would contaminate
([measurement.md](references/measurement.md)). Where the task fixes the workload
or hardware, specializing to that is fine. A fast path chosen by a property of
the input that guarantees the same result for every input with that property is
fine too; a branch keyed on the identity of the benchmark's files, seeds or
argument lists is not ([review-checklist.md](references/review-checklist.md)).

## Keep a record

For each experiment write down the hypothesis, the ceiling you estimated, the
change, the correctness result, the recorded measurement, and the verdict. A
failed hypothesis with its evidence is worth keeping: it stops the next attempt
from repeating it, and a pattern of failures often points at the real cost
center. Keep it where the task's loop keeps plans and results, if it has such a
place. Do not delete the record of a failure when a later change succeeds.

## When to read which reference

| Question | Read |
| --- | --- |
| Is this timing difference real? What is start-up and what is per-input? | [measurement.md](references/measurement.md) |
| How do I rebuild correctly? May I change flags, link statically, add a library? | [native-builds.md](references/native-builds.md) |
| Where does the time go? Which tool answers that? | [profiling.md](references/profiling.md) |
| What must not change? How do I test beyond the benchmark? | [semantic-preservation.md](references/semantic-preservation.md) |
| Is this change honest? What would a reviewer reject? | [review-checklist.md](references/review-checklist.md) |
| What does the evaluator already prove, and what must I check myself? What should a task author build? | [evaluator-design.md](references/evaluator-design.md) |
| What does this kind of program usually spend time on? | [workload-archetypes.md](references/workload-archetypes.md) |

## Limits of this skill

It teaches a process and the questions to ask. Expect modest gains from a mature
program: shaving start-up cost and hot loops while preserving every byte is a
real result, and a large gain on one pathological case at the price of changed
behavior elsewhere is a failure. Single mechanisms in a mature program rarely
bound above a few percent of the headline. A loop that stops when no plan's bound
reaches a threshold needs a threshold that fits (often 1%), which the operator
sets; a planner states the honest bound and never inflates it to keep a run
alive. This skill does not name a fix for any particular program: a technique
that helps one workload can do nothing, or harm, for another, and the profile
decides. Where the task states a rule that differs from this guidance, follow the
task.
