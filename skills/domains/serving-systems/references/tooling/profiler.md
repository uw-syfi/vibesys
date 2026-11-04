# Profiling — contract

Profiling answers one question: *where does the time go?* The discipline is portable; the toolchain is entirely platform-specific, so the concrete tools live under `platforms/<backend>/profiler.md`.

## Altitudes

Every platform's profilers fall into three altitudes. Pick by what you already know, not by what's installed:

| Altitude | Answers | Use when |
|:--|:--|:--|
| **System timeline** | host/device overlap, gaps, launch and transfer cost | a new problem — this classifies it |
| **Framework / op** | which op or Python frame dominates | the timeline says host-bound |
| **Kernel-internal** | occupancy, memory throughput, stalls | the timeline says one kernel dominates |

**Always classify before descending.** Kernel-internal tools have high overhead and answer a question you may not have. If a system-level profile shows the accelerator idle 60% of the time, no amount of kernel tuning helps.

## Invariants

1. **Profile the steady state.** Skip warmup, compilation, cache population, and first-call allocation. A profile that includes them describes startup, not serving.
2. **Profile a realistic workload.** The bottleneck at batch 1 and batch 64 are different bottlenecks. Profile the shape you intend to serve.
3. **One variable at a time.** A profile taken after three simultaneous changes cannot attribute the delta.
4. **Export metrics, don't read screenshots.** Comparisons across runs need numbers.
5. **Benchmark hygiene first.** A profile of a badly-constructed benchmark faithfully describes the wrong thing. See [`tooling/serving-benchmark.md`](serving-benchmark.md).

## Classification → fix

1. **Profile steady state, not startup.** Exclude import, CUDA-context init, JIT / compile warmup, first-iteration cold effects.
2. **Constrain capture aggressively** — `--delay` / `--duration`, `cudaProfilerStart/Stop`, NVTX-triggered capture. A 2-minute full trace is almost always unreadable.
3. **Annotate with NVTX** so the timeline is self-describing: one range per iteration / forward / backward / dataloader / eval.
4. **Diagnose before editing code.** Produce: bottleneck class + evidence + bounded change set + acceptance metric.
5. **Verify every recommendation** by re-running the same benchmark and comparing the same metrics.
6. **Measure observer overhead.** Compare the profiled run with an uninstrumented
   control at the same shape. Never call `torch.cuda.synchronize()` at every
   annotated scope boundary when diagnosing synchronization or overlap: that
   creates the serialization being measured. CUDA events around asynchronous
   host scopes are ordering markers, not exclusive attribution, unless a
   timeline proves which queued device work lies between them. If the headline
   metric changes by more than 10%, classify the profile as perturbed: use it
   for activation, ordering, graph coverage, fallback, or presence evidence,
   but not phase shares, removable milliseconds, Amdahl bounds, or hypothesis
   ranking. If no comparable control exists, apply the same restriction and
   call the capture uncalibrated.

## When CUPTI or external profilers are unavailable

Record one capability artifact, then stop retrying the same unavailable
permission/runtime pair. `CUPTI_ERROR_NOT_INITIALIZED`, an `nsys` daemon/export
failure, or missing container tracing privileges is a measurement blocker, not
evidence about the serving bottleneck.

For decode-forward device-time ranking, fall back to a shape-faithful isolated
microdriver with CUDA events:

1. Derive batch size and context lengths from an uninstrumented production row.
2. Warm the exact model, KV layout, and kernels before timing.
3. Bracket a small set of mutually exclusive forward buckets with CUDA event
   pairs on the executing stream; do not synchronize at bucket boundaries.
4. Record several iterations, synchronize once after the complete window, and
   compute event elapsed times afterward.
5. Compare total microdriver wall time with an uninstrumented control at the
   same shape. Reject quantitative attribution above the 10% perturbation band.

Use this fallback only for within-forward device-time ranking. It cannot reveal
CPU launch gaps, API synchronization, kernel names, or end-to-end phase shares.
For CUDA-graph serving, time whole graph replay separately and use an eager
same-shape microdriver for sub-forward buckets; do not present eager bucket
fractions as graph-era end-to-end shares. Keep event recording gated out of the
production service path.

The mapping from finding to remedy is portable even though the tools aren't:

| Finding | Likely fix |
|:--|:--|
| Gaps *between* steps | [`algorithms/async-scheduling.md`](../algorithms/async-scheduling.md) — host scheduler stall |
| Gaps *between* kernels | the backend's launch-overhead remedy (graph capture where it exists) |
| One kernel dominates | kernel-internal tools; consider a different kernel library first |
| Memory-bandwidth bound at decode | expected — check KV layout, quantization, batch size |
| Collective-bound | [`algorithms/parallelism.md`](../algorithms/parallelism.md) — topology and sharding |
| High device utilization but low throughput | utilization ≠ efficiency; descend to kernel altitude |

## Anti-patterns

- Descending to kernel altitude before a system-level diagnosis.
- Profiling startup and calling it representative.
- Comparing different input shapes across runs.
- Comparing a compiled run to an eager run without separating cold-start from steady-state.
- Overly broad traces that are impossible to interpret.
- Per-scope synchronization in a manual timer, then diagnosing the resulting
  profiler-induced gaps as application host overhead.
- Using ncu on every kernel before a systems-level diagnosis.
- "Increase batch size" without bottleneck evidence.
- Treating high utilization as proof of efficiency.
- Profiling a run that includes compilation or cache warmup.

## Platform toolchains

| Backend | System | Framework | Kernel |
|:--|:--|:--|:--|
| `cuda` | Nsight Systems (`nsys`) | torch profiler | Nsight Compute (`ncu`) |
| `rocm` | `rocprofv3` / `rocprof-sys` | torch profiler | `rocprof-compute` |
| `trainium` | `neuron-explorer` | torch profiler | NKI profile tooling |
| `metal` | Instruments | MLX metal trace | Instruments GPU counters |
| `cpu` | `perf` / Instruments | torch profiler | `perf annotate` |

## See also

- [`tooling/serving-benchmark.md`](serving-benchmark.md) — the benchmark is the profiler's input; get it right first
- [`platforms/`](../platforms/) — the selected backend's `profiler.md`
