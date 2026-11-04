---
name: results-against-base
description: Read a run's results against the base and the best so far, and judge whether a change is real. Use when asked how well the run is doing, which idea is best, or whether a gain can be trusted.
---

# Results against the base

The base is the unchanged starting code. Every result is meaningful only against it, so say
results as a ratio to the base (for example 1.42x faster) and state the direction: for a
metric where lower is better, a ratio above 1 means the candidate is worse.

- The best is the strongest kept result so far. An idea is kept when its result beats the
  base by more than noise; otherwise it is not better, or it failed.
- A step's value comes from the task's evaluation. Compare steps measured the same way: a
  value from a different workload or machine is not comparable.
- A small gain inside the run's noise is not evidence. When a step reports a comparison, its
  verdict is `better`, `within_noise`, `worse` or `inconclusive` (too few runs to estimate the noise: not noise, wait for more runs); trust the verdict over the raw numbers.
- Results flow from `get_idea` and `list_steps` (value, unit, commit). Do not average across
  units or invent a baseline the tools did not show.

When the operator asks what to do next, ground it in the pattern of results: what was tried
and kept, what repeated failures have in common, what no idea has tried yet.
