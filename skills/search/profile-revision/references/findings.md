# Findings

## Evidence over assertion

Every number in the answer comes from a capture you can point to. Say how it was taken
(command, workload, duration, machine). Say how much it varies: run the capture more than once
when the question depends on a small difference, and report the spread.

## Choosing a capture

- Where time goes in the process: a sampling profiler or a built-in profiler of the
  language or runtime, over a representative run.
- What a request or step does end to end: the system's own tracing or logging if it has any, or
  timestamps you add in a scratch copy outside the checkout.
- What limits a kernel or hot loop: counters or a roofline estimate from the domain skill.
- Whether a phase is worth optimizing: its share of the total, not its absolute time.

Prefer a capture that finishes in minutes. A profile that disturbs the run so much that the
numbers change is itself a finding; say so.

## Writing the answer

- Lead with the answer to the question, then the evidence.
- Put shares and absolute values side by side.
- Separate what you measured from what you infer.
- If two readings of the evidence are possible, give both and say what would tell them apart.

## What not to do

- Do not edit tracked files, commit, or leave processes running.
- Do not report a number from a run that failed its check or crashed.
- Do not widen the question. If you saw something else that matters, put it in one line at the
  end.
