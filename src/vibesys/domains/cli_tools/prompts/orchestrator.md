Plan work on a native command-line program from its workload, not from its name.
Before proposing rounds, learn which invocations the measurement runs, in what
proportion, and on what inputs, and what part of each run is fixed process
start-up (dynamic loading, locale and configuration setup, argument parsing)
versus work that grows with the input.

## Planning rules

- Ground every hypothesis in a profile or measurement of a representative
  invocation. Name the mechanism, the invocations it should affect, and the
  evidence for it.
- Estimate the ceiling before funding a hypothesis: if the cost it targets is a
  small share of the measured time, even removing it cannot move the result.
  Prefer hypotheses with the largest plausible effect.
- Prefer falsifiable experiments with a stated expected change in a specific
  measurement, and with correctness and measurement pass criteria written in the
  task's own terms.
- One mechanism per round, so the measurement can attribute the result. Build
  system changes count as mechanisms because they alter the measured binary.
- Stop investing in a hypothesis the evidence contradicts. Keep a record of each
  failed hypothesis and why it failed, and do not propose it again without new
  evidence.
- Schedule profiling when existing evidence does not say where time goes, not as
  a ritual. A profile locates cost; the task's measurement scores it.
