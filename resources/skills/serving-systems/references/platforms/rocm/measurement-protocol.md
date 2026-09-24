# Measurement protocol

A ROCm timing or counter number that skips these rules is not evidence — it is
a single sample of a noisy process. This extends [`tooling/profiler.md`](../../tooling/profiler.md)'s
portable invariants with the ROCm-specific mechanics: clock locking, per-chiplet
variance, and how the toolkit's own capture overhead can inflate the thing
you're trying to measure.

## Prerequisites

Read [`tooling/profiler.md`](../../tooling/profiler.md) first for the altitude
discipline (classify before descending). This file assumes you already have a
capture command and need to turn its output into a trustworthy number.

## The rules

| Rule | Value | Why |
|:--|:--|:--|
| **Warm** | discard cold runs | clocks ramp, caches fill, AITER/Triton JIT and TunableOp search resolve on first call |
| **Repeats** | ≥3, prefer 7 | a single sample is dominated by DVFS position |
| **Report** | median + spread, never a lone number | spread is how a reader judges the claim |
| **Noise band** | treat sub-~0.5% end-to-end deltas as unproven until you've measured your own floor | below it, clock and scheduling variance dominate; this repo has not yet published a measured noise floor — establish one before trusting small deltas |
| **Clocks** | locked, or at minimum monitored | see below |
| **A/B** | same session, non-overlapping (or interleaved), reference then candidate back-to-back | never compare across sessions, boxes, or days |
| **Untraced** | time in a pass separate from counter/trace collection | a profiled or counter-replayed run is not a timing run |

## What you are fighting on Instinct

- **Peak ≠ sustained clock.** This repo's own measured MI210 facts make the
  gap concrete: `bf16_tflops_spec = 181` but sustained GEMM throughput under
  continuous serving load measures **115–122 TFLOP/s** (power-limited, not
  compute-limited), while isolated bursts reach **~150 TFLOP/s**. A single
  short timed run can land anywhere in that 115–150 range depending on how
  long the clock has been under load — see
  `examples/model-serving/qwen3.5-9b-mi210/config/platforms/mi210.toml`.
- **Chiplet variance is architecture-dependent.** MI300-family parts (gfx942)
  are multi-die: several XCDs behind Infinity Fabric, each with its own clock
  domain, so repeat-to-repeat spread partly reflects *which* XCD a launch
  landed on. MI210 (gfx90a) is a single monolithic die — there is no
  cross-XCD placement variance to reason about there, but there is still
  ordinary DVFS ramp and thermal drift. Don't import multi-chiplet variance
  explanations onto gfx90a runs.
- **DVFS ramp lag.** A short kernel can finish before the clock ramps to its
  steady-state level. This is what the warmup step exists to hide.

Net rule: **compute achieved TFLOP/s (or GB/s) from measured wall time, never
from an assumed clock.**

## Controlling and monitoring clocks

```bash
# Lock a deterministic performance level before a kernel microbenchmark.
rocm-smi --setperflevel high            # or a fixed sclk/mclk profile via amd-smi
# Monitor during a run when you cannot lock (e.g. shared hardware).
amd-smi metric --gpu 0 --usage --clock --power --temperature
```

At minimum, watch `sclk` / `mclk` / power / temperature across the run and
**reject any A/B where the clock drifted between reference and candidate.**
A number produced while the clock was still ramping describes the ramp, not
the kernel.

## Same-session, interleaved A/B

A single blocked run (all reference reps, then all candidate reps) still
confounds thermal and DVFS drift with the treatment. `rocprof_profiler`'s
`kernel_bench.py` alternates reference and candidate reps within one session
and emits a paired verdict, which is the preferred shape for "did this change
actually help":

1. Warm both variants together, discard.
2. Alternate: ref, cand, ref, cand, ... for the chosen repeat count.
3. Compare medians and require the delta to clear the noise band with the
   clock log showing no drift.

Do not sum per-kernel microbenchmarks as a substitute for an end-to-end A/B —
that misses overlap, cache effects, and occupancy interactions between
neighboring kernels, and routinely disagrees with the end-to-end number in
either direction.

## Untraced timing is a separate pass

Every ROCm capture path perturbs wall time:

- `rocprofv3` system/kernel tracing adds per-dispatch interception overhead.
- `rocprof-compute`'s `profile` phase **replays the workload multiple times**
  to collect its full counter set — timing taken during that phase describes
  the replay, not one execution.
- PMC counter collection can force multi-pass replay when a job requests more
  counters than fit in one hardware pass (see
  [`profiler.md`](profiler.md#pitfalls)).

Run the timing pass with no tracer or counter collector attached
(`kernel_bench.py`'s event-timing mode), and run the attribution pass
(`analyze_rocprof.py`, `counters.py`, `compute.py`) separately. Report the
former as the speed number and the latter as the "where does the time go"
evidence — never quote a profiled run's wall time as the performance number.

A known ROCm 6.4 + torch 2.9.1 pitfall makes this doubly important for
`torch.profiler`: after a capture ends, subsequent async event waits can hang
or fault the GPU (`torch_profiler_post_capture_hang` in this repo's MI210
facts). Drain in-flight work and synchronize **before** ending a capture, and
never chain an untraced timing loop directly after a `torch.profiler` session
in the same process without that drain.

## Reporting format

```
<value> @ <SKU/gfx target>, ROCm <version>, <engine/lib>@<commit or version>, <date>
```

e.g. `+2.1% e2e @ MI210 gfx90a, ROCm 6.4, vLLM v0.3.1.dev190, 2026-09-24`

Median of ≥3 (prefer 7) warm repeats, with spread. Never present the spec
peak as an achievable number; report against the measured sustained ceiling
(see [`roofline.md`](roofline.md)).

## Prove the change is actually live

A measurement of the wrong binary or the wrong kernel is worse than no
measurement:

- The kernel you changed appears in `analyze_rocprof.py kernels` /
  `families` output, not merely in the source tree.
- For an AITER dispatch change: engagement proof per
  [`aiter-engagement.md`](aiter-engagement.md), checked *before* trusting any
  delta from it.
- For a source edit: confirm the compiled kernel actually changed (a "win"
  whose dispatched kernel name and code object are identical to the baseline
  is noise, not a result).

## Failure modes

| Symptom | Cause | Fix |
|:--|:--|:--|
| Sub-noise-band "win" | inside measurement noise | not a result; do not report it |
| Win doesn't reproduce | different session or clock state | same-session, interleaved A/B |
| Timing looks inflated | measured a traced or counter-replayed run | separate untraced timing pass |
| First rep is an outlier | cold cache, unramped clock | warm up and discard |
| Per-kernel sums disagree with e2e | missed overlap and dispatch interaction | trust the paired A/B, not summed microbenchmarks |
| Large spread across reps | thermal/DVFS drift, or (gfx942) XCD placement | lock clocks; report spread; re-run |
| GPU hang/fault right after a capture ends | ROCm 6.4 + torch 2.9.1 post-capture drain bug | synchronize before ending the `torch.profiler` session |
| Delta real but dispatched kernel unchanged | measured the wrong thing | confirm the edit is live before reporting |

## See also

- [`profiler.md`](profiler.md) — the tool-to-question map and capture recipes this protocol governs
- [`counter-triage.md`](counter-triage.md) — what to do with a counter capture once it's trustworthy
- [`roofline.md`](roofline.md) — the measured ceilings this file's "sustained, not spec" rule points at
- [`aiter-engagement.md`](aiter-engagement.md) — engagement proof before believing an AITER-path delta
- [`tooling/profiler.md`](../../tooling/profiler.md) — the portable profiling invariants
