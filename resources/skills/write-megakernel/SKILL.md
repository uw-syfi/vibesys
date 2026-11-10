---
name: write-megakernel
description: Design and optimize CUDA megakernels that execute a whole model or dependent operator pipeline in one launch. Use for persistent scheduling, cross-CTA dependencies, stage fusion, and Nsight Compute diagnosis while preserving the workload's correctness and timing contract.
---

# Write a good megakernel

Optimize the schedule of dependent work, then the inner loops. A single launch
can still serialize the GPU through small producer sets, long dependency
chains, and poorly placed waits.

Read [CUDA patterns](references/cuda-patterns.md) before implementing readiness
flags, completion counters, attention merging, or asynchronous staging. Read
[measured insights](references/measured-insights.md) for the supporting B200
experiment, failed variants, and limits on transferring its results.

## Establish the contract and a baseline

- Read the actual reference, input generator, checker, and timed entry point.
  Record batch, sequence/cache lengths, every layer, precision and accumulation
  rules, required state writes, output materialization, and launch budget.
- Distinguish public geometry from runtime values. Specialize on allowed
  geometry; consume current weights, tokens, caches, routes, and proposals on
  every call. Keep the evaluator and tolerance unchanged.
- Preserve a correct baseline and its artifacts. Work in a separate candidate
  directory; record source hashes, GPU, compiler, flags, and benchmark command.
- Run GPU checks using the environment's scheduling rules. Use Slurm where
  required; a profiling invocation is also a GPU job.

## Draw the dependency graph before deleting barriers

For each intermediate tile, identify its writer, exact consumers, readiness
event, last reader, and reuse point. Separate these cases:

| Dependency | Schedule to consider |
|---|---|
| Independent heads, groups, experts, or tiles | Publish each useful unit; start its consumers when that unit is ready. |
| A reduction over the full vector | Retain a join, compute private copies when cheap, or form partial reductions and merge. |
| Dense projection across all input columns | Prefetch weights early; optionally accumulate ready input tiles, but finish only after all required tiles. |
| In-place residual or scratch reuse | Wait for every old reader as well as every new producer. |

Replacing `grid.sync()` with a counter that every CTA waits on still creates a
broad stage join. That may be required by the dataflow. Prefer a coarser counter
when finer flags add polling and bookkeeping without starting useful work
earlier. "Local synchronization" describes the dependency scope; its flags can
reside in global memory.

### Prove synchronization and progress together

- Join producer threads before a leader publishes with device-scope release.
  Acquire readiness before reading the payload; distribute a leader's acquire
  to consuming CTA threads with a CTA barrier. Plain flags or `volatile` do not
  provide the required ordering.
- Completion counters need an explicit memory-order argument covering **all**
  producers. Use a proven release/acquire protocol from the CUDA reference.
- Initialize metadata before any reader can poll it. Tag reused flags with
  epochs or use monotonic counters; explain why expected values cannot be
  skipped or confused with a previous invocation.
- An epoch does not protect a payload from overwrite. Prove last-reader
  completion or use separate buffers with a reuse acknowledgment.
- For a cooperative persistent grid, query occupancy for the **compiled kernel**,
  actual block size, and dynamic shared memory. Keep the grid within supported
  cooperative residency and check launch errors. Recheck after resource changes.
- Full residency does not prove progress through circular waits. Draw wait
  edges and show that at least one unfinished producer can advance. For an
  oversubscribed scheduler, do not let spinning consumers occupy all slots while
  their producers remain unscheduled.

## Increase useful parallelism without inflating the critical path

- Count active producers per stage, not just launched CTAs. A 296-CTA grid can
  still run its critical attention stage on only 16 CTAs.
- Split attention over sequence partitions when heads alone expose too little
  work. Preserve a stable merge of partition maximum, exponential sum, and
  weighted value sum; cover tails and masks. Extra partitions add scratch,
  publications, and merge cost. Retune as batch and sequence length change.
- Consider private RMSNorm in each consuming CTA for a small shared vector.
  Compare duplicated reductions and reads against a single producer's latency,
  publication, and reloads. It becomes expensive at other hidden sizes or CTA
  counts; benchmark it in the intended schedule.
- Fuse Q/K normalization and RoPE near attention consumers when ownership and
  reuse allow. Compute position-dependent constants once per invocation only
  when the position is actually shared; never hardcode a runtime position.
- For roughly uniform independent work, inspect tile count versus resident
  capacity and the partially filled final wave. For a persistent kernel, also
  inspect per-stage producer counts and uneven work, since launch wave count
  alone cannot explain idle SMs or stragglers.
- Do not assume more CTAs, unrolling, tensor-core instructions, or higher
  occupancy helps. Include packing, precision conversions, duplicated work,
  register/shared-memory pressure, and waiting CTAs in the comparison.

## Stage memory where overlap is possible

Write the intended timeline: issue transfer, do independent work, await
completion, consume, release buffer. An asynchronous copy followed immediately
by a wait offers little overlap by itself.

- Prefetch weights while waiting for activation producers if the weights are
  already known and the prefetch does not compete with critical producers.
- Double-buffer a long stream such as LM-head weight tiles. Account for the
  occupancy cost of the second buffer and handle the final tile explicitly.
- Use unused space in a shared allocation for repeatedly read activations only
  after checking byte ranges, alignment, live intervals, and async-copy bounds.
- Keep transfer-completion barriers and computation/reuse barriers distinct.
  Async engines have visibility rules beyond ordinary thread stores; follow the
  target instruction's completion and proxy-fence requirements.
- Fuse residuals or other epilogues into the tile owner. Track partial argmax
  while producing logits, then reduce partials with the oracle's tie rule.
  Materialize every logit if the contract requires it.

## Use profiles to choose an experiment

Capture the exact candidate and workload; save source hashes with the report.
Read elapsed time, DRAM/L2 traffic, eligible warps, resource limits, spills, and
source-correlated stalls together.

- A high barrier sample fraction can mean CTA rendezvous, load completion, or
  waiting for another stage. Find the sampled instruction and its producers.
  Two grid barriers do not imply negligible synchronization cost.
- Long-scoreboard samples suggest load dependencies. Determine whether loads
  miss cache, arrive too late, repeat unnecessarily, or lack independent work.
- Stall sample fractions are **not fractions of end-to-end wall time**. NCU
  replay/instrumentation can change clocks, cache state, and timing; use the
  original benchmark for latency claims. Stage tracing also perturbs execution.
- Treat a speed-of-light (SOL) estimate as a bound with assumptions. Start with
  required bytes/bandwidth and operations/appropriate compute throughput; count
  active expert traffic, metadata, dequantization, and mandated outputs. Separate
  an algorithmic bound from a bound for the chosen SIMT or tensor-core schedule.
- A whole-model aggregate roofline ignores serial dependencies and underfilled
  stages. Add critical-path bounds and stage bounds; sum stage costs only when
  their non-overlap is established. Report an attainable target from measured
  bandwidth/throughput separately from the ideal bound.

## Accept a change with comparable measurements

State the hypothesis and expected counter or stage change before editing. Use
paired or randomly interleaved baseline/candidate runs and multiple fresh
fixtures for final selection. Keep rejected results. A screen chooses what to
validate; its fastest sample is not the final performance number.

Measure warm CUDA-event and synchronized host latency separately at the task's
original boundary; report median, spread, and tail when relevant. Compilation
and geometry-only setup can occur outside timing when allowed. Input-dependent
model work belongs inside `run`. Host dispatch can affect event measurements if
the start event executes while the host is still preparing the launch.

After synchronization or scheduling changes, check every required output and
state write, repeated calls, input preservation, and launch count. Use memory
and synchronization sanitizers where supported, alongside a protocol proof.
For final validation or an integrity audit, run the candidate before the oracle
on fresh inputs; perturb tensors at unchanged addresses, interleave models,
retain earlier outputs, and exercise relevant boundary/tie cases and streams.
These catch stale state and skipped work that an oracle-first check can miss.

Report the selected source, exact workload, correctness result, timing boundary,
measured gain, and remaining bottleneck. Distinguish a tested transfer from an
idea for another shape or model. Stop optional tuning when the requested result
is verified; do not turn this skill into an unrequested sweep.
