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
| Device counters land in the ambiguous middle (neither idle nor saturated) | kernel-internal phase timing or an ISA-level instruction audit to tell latency-bound from instruction-issue-bound; see `platforms/<backend>/profiler.md`. One such diagnosis (a decode kernel's ambiguous-middle counters resolved to instruction-issue-bound by ISA audit, fixed by cutting instructions per element, not by touching memory scheduling) closed a paired end-to-end acceptance test at 12 percent TPOT and 13 percent p95 TTFT; see `platforms/<backend>/` for the kernel. |

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
- **Gating a capture window on a bursty open-loop benchmark's own live signal and assuming it lands at the target concurrency.** A window gated on elapsed time plus a running-request threshold can fire exactly as designed and still capture a burst's decaying tail (e.g. batch size 2) rather than the sustained load the benchmark's aggregate numbers reflect, because request arrivals under an open-loop benchmark stay bursty well past ramp-up. Read the trace's own per-step batch size to confirm what load was actually captured; do not trust the gate signal alone. For a per-round kernel profile at a specific batch size, drive a synthetic steady stream at that fixed batch size instead of gating an open-loop benchmark's own traffic. Scope: any open-loop, bursty-arrival benchmark, engine- and backend-agnostic. Status: verified (gate fired at the intended threshold; trace confirmed the window still landed on a decaying burst at bs=2). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job 633974.
- **A driven, fixed-batch synthetic stream fixes the problem above only up to a per-workload concurrency limit.** Driving sessions at a fixed target concurrency (rather than an open-loop benchmark's own traffic) landed every captured round exactly on the intended batch size at four target sizes in one sweep, confirming the method works. At a larger target size in the same sweep it still failed, for a related but distinct reason: admission for that many concurrent sessions took many seconds to drain (per-session time-to-first-token spread from under 2 s to over 30 s), so a gate that waits for every session to individually cross a token-count threshold fired only once the slowest admission straggler finished, by which point most other sessions had already completed their whole token budget. The window again caught the tail of a near-simultaneous cohort finishing together, not a sustained batch, the same failure shape as the open-loop case above with a different root cause. Fix: gate on the server's own concurrent-request count crossing the target instead of a per-session completion signal, or size the per-session token budget well past what the slowest admission straggler needs. Scope: any fixed-batch synthetic profiling driver, engine- and backend-agnostic. Status: verified (four batch sizes captured cleanly; the collapse at a fifth, larger size reproduces the general failure mode above under a different mechanism). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.
- **After an instruction-count fix, re-run the issue-bound-versus-latency-bound discriminator instead of reusing the old classification.** A kernel found issue-bound (near-saturated instruction-issue counters) and fixed by cutting instructions per element does not stay issue-bound just because the fix worked: on one kernel, cutting the per-element instruction count moved the same hardware counter that had been near-saturated down to a comfortably low reading, and the next lever became load shape and bytes in flight, not instruction count. Reclassify with the same counters used to find the first bottleneck before picking the next optimization; treating the earlier classification as still valid after an instruction-count change ships is what causes an unproductive next attempt. Scope: any kernel-internal-altitude profile taken after an instruction-count-reducing kernel change, engine- and backend-agnostic. Status: candidate (observed once, on one kernel family; the mechanism is plausible and general, but a second kernel showing the same flip would confirm it). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.
- **Fewer loads is not faster if it fragments the wait structure.** A load-sharing kernel change cut memory-read instructions by about a third with flat-or-lower VALU work, yet wall-clock time regressed 25 to 60 percent: busy cycles and wait-on-any-instruction cycles rose 1.7 to 2.5x and VALUBusy dropped by about half, because the compiled loop split from one clean load-wait-compute region into three to four smaller regions, each a fresh stall point. Count `s_waitcnt` regions and wait cycles from a hardware-counter profile, not just static instruction or load counts, when judging a load-reduction change. A related trap: reordering the source to hoist independent loads earlier changed nothing here, because the backend's own instruction scheduler had already reordered them; check the compiled output before assuming a source-level reorder will help. Scope: any kernel-internal profile of a load-count-reducing change, engine- and backend-agnostic. Status: verified (reproduced across two independent fix attempts with near-identical compiled loops and indistinguishable timing between them). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.
- **A crossover-interpolation script can silently keep the old default when no crossover exists in the swept range.** A dispatch-threshold sweep tool that assumes a sign change exists between two code paths' timings, then interpolates to find it, has no way to report "no crossover found" and can fall back to reusing the previous default instead, a script fallback that looks like a null result rather than a flagged error. After a kernel change shifted per-block cost on both sides of a scaffold-versus-template dispatch, the templated path won at every measured block count and the interpolator silently kept the stale threshold, making that run's own retuned variant a no-op. Fix: re-derive thresholds from the raw sweep table after any kernel change that could shift per-block cost, and treat a monotonic sweep (no sign change) as a real finding worth recording, not a script failure. Scope: any threshold or crossover tuning step driven by an interpolation script, engine- and backend-agnostic. Status: verified (reproduced: the sweep showed no sign change, the script fell back to the prior default, and a targeted rerun at the sweep's smallest tested point confirmed the templated path still won there). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.
- **Compare the same bucket fold across two profiles, not just the same bucket name.** A summary table that prints a bucket on its own (for example "other" reported bare) is not comparable to a summary table that folds several sub-buckets into that same-named bucket (for example "other" reported as other-plus-norm-plus-sampling/verify-glue-plus-shared-expert): the printed numbers can differ by 3-4x from this alone, with no change in the underlying trace. Reprocess both profiles' raw traces with an identical classifier and an identical fold before computing a delta between two runs' own printed tables. Scope: any trace-summary comparison across two profiling runs, engine- and backend-agnostic. Status: verified (reprocessing two profiles' raw traces with an identical classifier and fold found a like-for-like delta of 0.21 ms, 7.3 percent, where the two runs' own printed tables implied a much larger jump because one folded four sub-buckets together and the other did not). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.
- **A server log with embedded carriage returns from a progress bar breaks line-oriented tools.** `grep`, `awk`, and `sed` split lines only on `\n`; a log that also carries bare `\r` writes from a progress bar (tqdm and similar) miscounts lines and can attribute the wrong print statement to the wrong timestamped event, corrupting anything built on "the Nth line" or "lines between two timestamps." Parse such a log with a small script in a language whose text mode also splits on `\r` (for example Python's universal-newlines mode), not with `grep`/`awk`/`sed` alone. Scope: any server or trace log that interleaves progress-bar output with structured log lines, engine- and backend-agnostic. Status: verified (a claimed 15-distinct-value count inside one window did not reproduce under a line-accurate re-parse of the same window; the true count was 6). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.
- **A residual "other" bucket from a class-by-regex trace summarizer can be mostly misclassified work, not diffuse overhead.** A summarizer that buckets kernels by matching each kernel's name against a list of regexes silently drops any non-matching kernel into a catch-all "other" bucket, and regex misses compound easily: a pattern for `gated_delta` kernels does not also match a `gating_delta`-spelled one, a substring pattern for a state-update kernel needs to catch that name glued inside a longer identifier too, and a split/reshape/scatter helper for a named block can carry a name that looks unrelated at a glance. Reclassifying seven such misfiled kernels here moved the true source of an "other" bucket from 4.8 to 6.6 ms/round (8.4 to 8.6 percent of round wall) down to 1.8 to 2.2 ms/round (about 3 percent), most of the difference being kernels that belonged to a block the summarizer already had a named bucket for. Fix: before treating "other" as diffuse, unattributed overhead, list its top kernels by name and launch count; if a handful of names dominate the bucket and look like they belong to a named block by what they do, not by their exact string, the classifier's regexes are missing them, not measuring real unattributed work. Only trust "other" as genuinely diffuse once its top kernels, by name, don't obviously belong elsewhere. Scope: any class-by-regex trace summarizer, engine- and backend-agnostic. Status: verified (mechanism read from the regex source; reproduced by rerunning the same traces through a corrected classifier). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.

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
