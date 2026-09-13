# Qwen3.5-397B-A17B

Hybrid Gated-DeltaNet plus full-attention MoE decoder: structure, the tensor-parallel forward-step shape, memory footprint, and known serving gates.

## Structure

| Property | Value |
|:--|:--|
| Layers | 60 |
| Layer pattern | 45 linear-attention (Gated DeltaNet) + 15 full-attention, `full_attention_interval` 4 (one full-attention layer every 4th layer) |
| Hidden size | 4096 |
| Routed experts | 512, top-10 |
| Shared expert | 1, intermediate 1024 |
| MoE intermediate size | 1024 |
| Full-attention heads | 32 query heads, 2 KV heads, head_dim 256 (GQA 16:1) |
| Gated DeltaNet heads | 16 k heads, 64 v heads, head_dim 128, conv kernel 4 |
| Vocab size | 248320 |
| Max position embeddings | 262144 |

Status: verified. Source: `config.json` (`model_type qwen3_5_moe_text`), amd/Qwen3.5-397B-A17B-MXFP4, 2026-09-05.

## Serving implications

### Forward step under tensor parallelism

Experts are sliced on the 1024-wide `moe_intermediate_size` dimension, not assigned whole to a rank: at TP=4 each rank holds a 256-column shard of every expert and runs every selected expert's shard for every token it sees. At batch 16 with top-10 routing, a rank runs up to 160 distinct experts per layer. Each layer contributes two all-reduces, one after the mixer's row-parallel output projection (attention or Gated DeltaNet) and one after the MoE down-projection, for 120 all-reduces per forward step across the 60 layers.

Status: verified. Source: fork (uw-syfi/sglang, branch vibesys-task-v3 @ f05b78609a), 2026-09-05.

### Memory

The MXFP4 checkpoint (amd/Qwen3.5-397B-A17B-MXFP4) is 239 GB on disk. At TP=4 it keeps about 212 GB resident (roughly 53 GB per device). It does not fit on fewer than three 128 GB devices.

Status: verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-05, job-verified.

### Hybrid cache state

Each Gated-DeltaNet layer carries a per-request conv state (kernel 4) and an SSM state; these live in a Mamba cache alongside the paged KV cache the 15 full-attention layers use. See [`ssm-hybrid.md`](ssm-hybrid.md) for the hybrid cache model and [`../algorithms/heterogeneous-kv-cache.md`](../algorithms/heterogeneous-kv-cache.md) for the allocator that must size both pools together.

### Speculative decoding

The checkpoint ships its own MTP draft head: `mtp.fc.weight` plus a full 512-expert MoE at `mtp.layers.0` (`mtp_num_hidden_layers` 1), matching a target layer's width and expert count. No separate draft model is needed. NEXTN (MTP) speculative decoding with this head, k=3 draft steps (4 verify tokens), `eagle-topk` 1, greedy verify, and the linear-replay SSM fast path for the hybrid Gated-DeltaNet layers is accepted at both an uncapped and a 16-session concurrency cap; see the Measured section below for the numbers and [`platforms/`](../platforms/) for the accepted launch flags and the checkpoint's load-path pitfalls (the draft head is absent from this model's TP-sharded fast-path artifact). The accepted stack on top of this (PyTorch TunableOp-tuned dense GEMMs, a register byte-permute rewrite of the fused MXFP4 MoE kernel's decode16 lookup, and dword-wide packed-weight loads in that same kernel) is in the Measured section below.

Mechanism: decode-step time here is latency-bound on the MoE kernel's per-step activation gather, not on raw arithmetic (see "Decode-step time..." below), and that per-step cost grows only sublinearly with the number of rows verified together (about 26 ms at 16 rows, calibrated exponent ~0.31, so M rows cost roughly `26*(M/16)^0.31` ms; see [`../tooling/performance-modeling.md`](../tooling/performance-modeling.md) for the padding-floor mechanism behind this scaling). Verifying k+1 rows per session per step is therefore nearly free next to running k+1 separate decode steps, and every accepted draft token amortizes that one verify step over more emitted tokens. Speculative decoding lowers the per-token time floor; it does not change bandwidth efficiency or the measured-time-over-roofline ratio recorded elsewhere in this file.

Status: verified (accepted at two concurrencies; mechanism explained by the calibrated sublinear-scaling exponent, itself flagged low-confidence in the source memo). Stamp: sglang-v0.5.18-rocm700-mi30x, benchmark_version 4, 2026-09-12, job-verified at both the uncapped and the 16-session-cap runs.

## Pitfalls

```
Symptom: An accuracy probe that compares generated content directly scores
         inconsistently across otherwise-identical runs (one gate run 6/13,
         a later run on the same checkpoint 13/13).
Cause:   The chat template turns thinking mode on by default; reasoning text
         is emitted ahead of the content and pollutes any comparison that
         expects content only.
Fix:     Send `chat_template_kwargs: {"enable_thinking": false}` on every
         request an accuracy probe issues.
Scope:   Qwen3.5-397B-A17B chat template; backend-independent.
Status:  verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-05.
```

The same template also renders a past assistant turn differently from
the turn currently being generated (an empty think block on the live
generation prompt, stripped from history), which caps turn-2+ prefix
reuse at "previous prompt only" regardless of any radix-cache tracking
knob; see [`../algorithms/radix-prefix-caching.md`](../algorithms/radix-prefix-caching.md)'s
chat-template pitfall for the mechanism and the check.

### Accuracy evaluation

GSM8K 8-shot accuracy for this checkpoint on the accepted serving stack is
94.1 percent (pooled across two 500-question passes), not the ~30 percent
an earlier evaluation reported; that earlier number was an
evaluation-harness bug (a raw completion prompt against this thinking chat
model with no chat template), not a property of the model or the stack.
Greedy generations are also not run-to-run text-identical on this stack,
even at concurrency 1, from intrinsic per-request kernel and
speculative-decoding-verify numerics rather than batch composition; use
task accuracy with a stated standard error, not text agreement, as the
correctness signal. See
[`../tooling/accuracy-checker.md`](../tooling/accuracy-checker.md) for the
evaluation protocol, the nondeterminism measurement, and the acceptance
tolerance template.

Status: verified. Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13,
job-verified.

## Measured

**Current accepted numbers, this stack (padded prefill-M TunableOp plus decode/verify-M TunableOp plus gemm-pad-m plus breakable prefill CUDA graph plus NEXTN k=3 plus overlap off), plain default launch, no manual env or extra server args:** pooled p95 TTFT turn2+ at 48 uncapped sessions is about 207 ms (paired-acceptance pool: 206.1 ms), pooled p50 about 119 ms, median TPOT about 10.1 ms; at a 16-session cap, p95 TTFT turn2+ is about 165-180 ms depending on collapse exclusion and TPOT about 8.8 ms. An earlier pass on this same stack minus prefill-M TunableOp coverage (that candidate's own results were unmeasurable across four boots due to a CRLF bug that silently discarded its results table every time, see the TunableOp section below) measured p95 TTFT turn2+ 256-321 ms at 48 sessions and about 206 ms at a 16-session cap across several rep sets; the numbers above are a real improvement over that range, not a re-measurement of the same stack. Status: accepted (paired end to end, three jobs). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

| Metric | Pre-kernel baseline (baseline_v1cfg) | Accepted config: fused MoE + skinny GEMM + mixed chunked prefill (defaults) | Scope |
|:--|:--|:--|:--|
| Mean TPOT | 106.8 ms | 21.7 ms | 4x MI300A, TP=4, 48 concurrent multi-turn sessions, admission-aware open-loop schedule (benchmark_version 3) |
| p95 TTFT, turn 2+ | 818 ms (pooled per-turn, n=830) | 455 ms (pooled per-turn, n=830) | same |
| Throughput | ~102 tok/s | ~104 tok/s | same; throughput is fixed by the open-loop schedule's offered rate (every side in the matrix below lands within about 2 percent of the others), so it is not a useful differentiator at this load |

Accepted config: `SGLANG_MXFP4_MOE_HIP=1`, `SGLANG_SKINNY_GEMM=1`, plus mixed chunked prefill (`--enable-mixed-chunk --chunked-prefill-size 1024`); see [`platforms/`](../platforms/) for the backend-specific kernels and [`../algorithms/chunked-prefill.md`](../algorithms/chunked-prefill.md) for the chunked-prefill contract. Baseline: Triton MXFP4 MoE fallback kernel with hipBLASLt default dense GEMMs, all three off/unset.

A later chunk-size sweep on top of the accepted stack (NEXTN k=3, breakable prefill CUDA graph) confirmed 1024 as the best of the three sizes tested: 512 regressed pooled p95 TTFT turn2+ 19 percent (a co-batching-rate side effect, see [`../algorithms/chunked-prefill.md`](../algorithms/chunked-prefill.md)'s pitfall), 2048 showed no material change (smaller than its own rep spread), and 1024 reproduced the reference numbers within noise. Keep 1024 on this stack. Status: verified (3 reps per side, one boot). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

Four-side pooled per-turn TTFT quantiles under the admission-aware open-loop schedule (this section's four-side-matrix job, 5 reps per side pooled; see [`../tooling/serving-benchmark.md`](../tooling/serving-benchmark.md) for why pooled quantiles, not per-rep percentiles, are the metric of record here), p50 / p90 / p95 / p99, plus each side's median mean TPOT and `schedule_bound_fraction` range across its 5 reps:

| Side | p50 | p90 | p95 | p99 | Median mean TPOT | schedule_bound_fraction |
|:--|:--|:--|:--|:--|:--|:--|
| baseline_v1cfg (all off) | 455.1 ms | 630.9 ms | 818.4 ms | 1252.5 ms | 106.83 ms | 0.19-0.38 |
| sched_only (schedule only, kernels off) | 464.1 ms | 618.2 ms | 675.9 ms | 1071.4 ms | 98.18 ms | 0.82-1.00 |
| fused_only (fused MoE HIP kernel) | 254.9 ms | 382.7 ms | 466.9 ms | 870.6 ms | 47.51 ms | 0.99-1.00 |
| defaults (fused MoE + skinny GEMM + mixed chunked prefill) | 209.0 ms | 340.8 ms | 455.0 ms | 865.7 ms | 21.74 ms | 1.00 |

Paired deltas vs. baseline_v1cfg (pooled p95 TTFT turn-2+, median TPOT): sched_only -17.4 percent / -8.1 percent; fused_only -42.9 percent / -55.5 percent; defaults -44.4 percent / -79.6 percent.

Under fixed pacing, TPOT reflects offered load as well as kernel speed: a server fast enough to keep up with the schedule runs smaller batches than one held at the concurrency cap, so a side's own TPOT drops as it moves from load-bound to schedule-bound. The `defaults` stack measures 21.7 ms TPOT at this load (schedule-bound, `schedule_bound_fraction` 1.00) versus about 35 ms for the same kernel stack at a full batch of 16 (load-bound, concurrency-capped pacing). Compare TPOT rows only across sides measured at the same offered load and concurrency cap; see [`../tooling/serving-benchmark.md`](../tooling/serving-benchmark.md).

The custom skinny GEMM kernel alone (without mixed chunked prefill) was found at n=5 to raise p95 TTFT turn-2+ about 38 percent versus fused-only rather than leave it unchanged (an earlier n=3 result had shown the opposite ranking, which does not reproduce). Mixed chunked prefill on top of skinny GEMM reverses that regression, landing close to (about 12 percent above) fused-only's p95 while keeping the full TPOT win. This is a benchmark_version 2 finding: the benchmark_version 3 four-side matrix above has no skinny-GEMM-alone side, so treat it as provisional pending a v3 rerun.

Status: verified (four-side benchmark_version 3 matrix, 5 reps per side pooled); skinny-GEMM-alone reversal is a benchmark_version 2 finding, not yet rerun under v3. Stamp: sglang-v0.5.18 fork (`moe/mxfp4-fused` + `gemm/skinny` + `bench/admission-schedule`), benchmark_version 3, 2026-09-11, job-verified.

### Load dependence

At unlimited concurrency (48 sessions, benchmark_version 4, the 16-slot admission cap lifted, job-verified), the accepted configuration holds the schedule (`schedule_bound_fraction` 1.00): it paces 1.17 turns per second with pooled p95 TTFT turn-2+ 535 ms and mean TPOT 61 ms at 218 tok/s. The all-off baseline cannot hold the schedule at this load (`schedule_bound_fraction` 0, every rep): it degenerates to a closed-loop capacity measurement, 175 ms TPOT, 168 tok/s, pooled p95 TTFT turn-2+ 922 ms. The same accepted configuration at a 16-session cap on the same node holds pooled p95 TTFT turn-2+ at 338 ms and TPOT at 21.4 ms.

TPOT and p95 TTFT are both functions of offered load and the concurrency cap, not of kernel speed alone: compare rows only across sides measured at equal load and cap. See [`../tooling/serving-benchmark.md`](../tooling/serving-benchmark.md).

Status: verified. Stamp: sglang-v0.5.18 fork, benchmark_version 4, 2026-09-11, job-verified.

Decode-step time is dominated by the MoE expert FFN, not by the mixer or the collectives: MoE work accounts for roughly three-fifths of decode-step device time, with most of the remainder in the model's other dense (non-expert) GEMMs; collective and mixer time is small by comparison. This was confirmed by a kernel-level profile, reproduced twice. The specific kernel names and per-component percentages are implementation details of the selected backend's MoE and GEMM kernel choice; see [`platforms/`](../platforms/) for the selected backend's kernel notes, not repeated here. With the fused HIP MoE kernel in place, MoE is still the largest decode-step term. A counter-level profile alone could not localize the limiter (neither bandwidth- nor ALU-bound by its counters); in-kernel phase timing then showed it is latency-bound on the per-step scattered activation gather inside mostly-padded 16-row expert blocks, with the MFMA instructions issuing at their native rate, not on the decode arithmetic that feeds them; see [`platforms/`](../platforms/) for the phase breakdown and the counters that separate the two.

Status: verified (three-fifths-of-decode-time finding reproduced twice; fused-kernel latency-bound finding verified once). sglang-v0.5.18-rocm700-mi30x, 2026-09-11.

### Decode round breakdown, driven fixed-batch sweep, measured

A synthetic fixed-batch driver (see [`../tooling/profiler.md`](../tooling/profiler.md)) captured a clean per-round kernel breakdown at four session counts under speculative decoding, avoiding the open-loop benchmark's own capture-window problem. Routed MoE is the largest term at every size measured and its share grows with batch size, from about 65 percent at the smallest size to about two-thirds at the largest, consistent with the "roughly three-fifths" figure above and refining it with real batch-size-swept numbers instead of one operating point:

| Block | N=8 | N=16 | N=32 |
|:--|--:|--:|--:|
| Routed MoE (stage1+stage2) | 64.9% | 66.6% | 66.2% |
| Dense GEMM | 10.0% | 10.0% | 10.3% |
| Collectives | 5.2% | 5.8% | 7.7% |
| CPU-only gap | 5.9% | 4.5% | 3.6% |
| Attention | 1.4% | 2.0% | 2.4% |
| Gated DeltaNet (mixer kernel only) | 0.5% | 5.2% | 5.7% |
| Other (unclassified) | 9.4% | 3.2% | 2.8% |

The N=8 column's Attention, Gated DeltaNet, and Other cells still reflect the pre-fix trace classifier (see the corrected-classifier paragraph below); they were not reprocessed and likely undercount Gated DeltaNet the same way N=16 and N=32 did before the fix. The other four rows are unaffected by the classifier fix at every N.

Against the campaign's HBM-bandwidth floor, routed MoE measures about 2.55x its floor at N=16 (a floor of roughly 14.8 ms) and the whole round measures about 3.09x an 18.3 ms floor at that same point; both ratios shrink as batch size grows. See [`../tooling/performance-modeling.md`](../tooling/performance-modeling.md) for the padding-floor mechanism this confirms across the sweep.

A later audit of the trace classifier (see [`../tooling/profiler.md`](../tooling/profiler.md)) found its kernel-name regexes missed seven Gated-DeltaNet kernels (a `gating_delta` versus `gated_delta` spelling mismatch, an SSM-update kernel name glued inside a longer identifier, a QKVZBA split kernel, a conv-window scatter kernel, and a fused sigmoid-mul kernel, among others) and two attention-adjacent kernels (a RoPE kernel and a segment-reduce companion), all of which had been landing in the unclassified "Other" bucket instead of their own named blocks. Reprocessing the same traces with the corrected classifier moves most of what looked like a diffuse 3.9 to 6.6 ms/round unattributed residual into the Gated-DeltaNet bucket, which grows about 13x at N=16 (0.225 to 2.932 ms/round); the table above reflects this fix at N=16 and N=32. Gated-DeltaNet kernels (mixer bucket plus the reclassified glue kernels) now cost about 5 percent of the round directly, not an estimate derived from a share of "Other". The true unattributed residual shrinks to about 3 percent of the round (1.8 ms/round at N=16, 2.2 ms/round at N=32) and stays diffuse: no kernel in it exceeds a few tenths of a millisecond per round, and about 39 distinct generic elementwise/reduce kernel names split most of what remains. This is architecturally expected: GDN's per-token state update does not fuse into the grouped-GEMM or attention buckets the way dense or MoE compute does, so it shows up as its own family of small kernels rather than as bookkeeping overhead on top of something else.

Status: verified (four clean batch-size points for the round breakdown and floor ratios; classifier fix for Attention, Gated DeltaNet, and Other reprocessed and confirmed at two of the four, N=16 and N=32). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.

### Decode round breakdown, post-permute re-profile, measured

Re-profiled the same driven fixed-batch sweep after the register byte-permute `decode16` kernel below (see the Measured section) was accepted as the default. Pooled medians across ranks and rounds, N=16 (n=116), previous stack alongside:

| Block | Previous (ms) | New (ms) | Delta | Floor at 5.3 TB/s (ms) | New ratio to floor |
|:--|--:|--:|--:|--:|--:|
| MoE stage1 | 27.36 | 23.00 | -15.9% | -- | -- |
| MoE stage2 | 10.33 | 9.46 | -8.4% | -- | -- |
| **MoE routed total** | **37.69** | **32.47** | **-13.9%** | **14.81** | **2.19x** (was 2.55x) |
| Dense GEMM | 5.67 | 5.57 | -1.8% | 3.47 | 1.60x (was 1.63x) |
| Collectives | 3.28 | 3.48 | +6.1% | -- | -- |
| CPU-only gap | 2.56 | 2.75 | +7.2% | -- | -- |
| **Round wall** | **56.54** | **51.11** | **-9.6%** | **18.28** | **2.80x** (was 3.09x) |

Attention, Gated DeltaNet, sampling/verify glue, and the residual "other" bucket are flat within noise and omitted above; see the previous section for their shares. Routed MoE remains the largest single block by a wide margin (63.5 percent of the round at N=16) and still the top optimization target, now at a smaller absolute size and a smaller over-floor ratio.

The same shrinking-ratio shape holds at the other two clean batch sizes swept: MoE routed total measures 23.18 ms at N=8 (2.64x its 8.78 ms floor, was 3.06x) and 44.11 ms at N=32 (1.96x its 22.51 ms floor, was 2.30x); round wall measures 37.67 ms at N=8 and 69.66 ms at N=32. N=48 collapsed again for the same admission-queueing reason documented in the previous section (observed verify batch sizes never reached 48), so it is not reported here.

Status: job-verified (N=16 previous-vs-new comparison and the N=8/N=32 ratio shifts reproduced across two independent boots; bucket-sum cross-check against measured GPU-busy time held). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.

### Speculative decoding (NEXTN, k=3), measured

Paired against the accepted fused-kernel defaults (no speculative decoding), 5 reps per side pooled, gates 13/13 on every rep of every side:

| Concurrency | TPOT (median) | pooled p95 TTFT turn2+ | accept length (median, of 4 draft tokens) |
|:--|--:|--:|--:|
| Uncapped, 48 sessions | 70.86 -> 38.27 ms (-46.0%) | 815.6 -> 759.0 ms (-6.9%) | 2.89 |
| 16-session cap | 22.10 -> 13.49 ms (-39.0%) | 423.6 -> 451.8 ms (+6.7%) | 2.84 |

The 16-session-cap p95 TTFT row uses the steady-state reference reps for the baseline side; see [`../tooling/serving-benchmark.md`](../tooling/serving-benchmark.md) for why one rep is excluded from that reference and reported separately rather than averaged in. k=3 was chosen over a k=2 probe by the lower-median-TPOT rule, both having cleared gates and a 10 percent p95 TTFT budget; see [`platforms/`](../platforms/) for the k=2/k=3 comparison.

Boot cost: 2.6 to 2.8x longer than the non-speculative boot (about 800 to 900 s versus about 300 s), because the draft head loads from the unsharded checkpoint and boot captures extra decode graphs for the draft path. A deployment-time cost only; it does not affect the serving-time numbers above.

Status: verified. Stamp: sglang-v0.5.18-rocm700-mi30x, benchmark_version 4, 2026-09-12, job-verified at both the uncapped and the 16-session-cap runs.

### Overlap scheduler off, on top of NEXTN k=3, measured

Paired against the NEXTN k=3 configuration above with the overlap scheduler on, 5 reps per side pooled, gates 13/13 on every rep of every side:

| Concurrency | pooled p95 TTFT turn2+ | TPOT (median) | accept length (median, of 4) |
|:--|--:|--:|--:|
| Uncapped, 48 sessions | 734.4 -> 557.5 ms (-24.1%) | 37.21 -> 38.45 ms (+3.3%) | 2.852 vs 2.860 |
| 16-session cap | 438.5 -> 313.8 ms (-28.4%) | 13.17 -> 14.06 ms (+6.8%) | 2.843 vs 2.823 |

Accepted for TTFT-weighted multi-turn workloads; keep the overlap scheduler on for throughput-weighted ones. See [`platforms/`](../platforms/) for the mechanism (the overlap scheduler's one-iteration publish lag) and the trade-off rule.

Status: verified. Stamp: sglang-v0.5.18-rocm700-mi30x, benchmark_version 4, 2026-09-12, job-verified at both the uncapped and the 16-session-cap runs.

### PyTorch TunableOp tuned dense GEMM, on top of NEXTN k=3 + overlap off, measured

Paired against the NEXTN k=3 + `--disable-overlap-schedule` configuration above (TunableOp off vs on, identical flags otherwise), 5 reps per side pooled, gates 13/13 on every rep of every side:

| Concurrency | TPOT (median) | pooled p95 TTFT turn2+ | accept length (median, of 4) |
|:--|--:|--:|--:|
| Uncapped, 48 sessions | 38.05 -> 22.86 ms (-39.9%) | 499.0 -> 493.3 ms (-1.1%) | 2.845 vs 2.871 |
| 16-session cap | 14.31 -> 12.44 ms (-13.1%) | 331.5 -> 334.4 ms (+0.9%) | 2.841 vs 2.848 |

Accepted as the default on top of the base configuration above. p95 TTFT is unchanged at both concurrencies (well within each side's own rep-to-rep spread) because prefill runs eagerly outside CUDA-graph capture and almost never lands on one of the tuned exact-M shapes, so the tuned table cannot touch the term that dominates p95 TTFT either way. See [`platforms/`](../platforms/) for the tuning recipe, the per-device filename pitfall the first acceptance attempt hit, and the mechanism behind why the TPOT gain exceeds a single-forward prediction.

Status: accepted. Stamp: sglang-v0.5.18-rocm700-mi30x, benchmark_version 4, 2026-09-12, job-verified at both the uncapped and the 16-session-cap runs.

Raising k from 3 to 4 (5 draft tokens, its own TunableOp table) was probed and refuted: accept_len rose +8.9% (2.85 to 3.10) but median TPOT regressed +6.9% (22.43 to 23.97 ms) because the per-slot accept rate fell 0.618 to 0.527, so k=3 remains the accepted draft length; see [`platforms/`](../platforms/) for the full comparison.

**Follow-on: TunableOp coverage extended to prefill M, also accepted.** The decode/verify-only table above leaves p95 TTFT untouched because prefill runs eagerly and almost never lands on a tuned exact M; a later table adds 13 padded prefill-M buckets and is paired-accepted on top of everything above: pooled p95 TTFT turn2+ -39.7 percent at 48 uncapped sessions and an improvement (not a regression) at a 16-session cap, median TPOT -8.7 and -1.7 percent, GSM8K accuracy inside tolerance. This candidate was refuted four times on earlier boots before a CRLF line-ending bug in the results CSV, which silently discarded the whole tuned table every time, was found and fixed; see [`platforms/`](../platforms/) for the pitfall, the kernel-name-audit method that found it, and the full numbers. Status: accepted. Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified across three paired jobs.

### Permute-based MXFP4 MoE decode16 kernel rewrite, on top of NEXTN k=3 + overlap off + TunableOp, measured

A source-level rewrite of the fused MXFP4 MoE kernel's `decode16` weight-unpack step (register byte-permute lookup instead of a per-element memory gather; no new flag, see [`platforms/`](../platforms/) for the kernel) was paired end to end against the TunableOp stack above, one node per concurrency, 5 reps per side per job, identical harness-default flags on both sides otherwise, gates 13/13 on every rep of every side (20/20 reps total):

| Concurrency | pooled p95 TTFT turn2+ | median TPOT | accept length |
|:--|--:|--:|--:|
| Uncapped, 48 sessions | 466.9 -> 405.3 ms (-13.2%) | 21.35 -> 18.71 ms (-12.4%) | 2.888 vs 2.849 |
| 16-session cap | 381.6 -> 341.9 ms (-10.4%) | 12.45 -> 11.89 ms (-4.5%) | 2.859 vs 2.843 |

Both concurrencies' TPOT improvement exceeds each side's own rep-to-rep spread (per-rep ranges do not overlap: c48 base 20.78-22.30 vs perm 17.99-19.02 ms; c16 base 12.39-12.61 vs perm 11.69-12.21 ms). This was predicted from a kernel microbenchmark (see [`platforms/`](../platforms/)) at about 14 percent for the decode round at batch 16 and about 9 percent for a 337-token prefill iteration; the measured p95 TTFT improvements (13.2 and 10.4 percent) land close to that range, and the measured TPOT improvement at 48 sessions (12.4 percent) is close to it too, with a smaller (4.5 percent) but still non-noise effect at 16 sessions where MoE is a smaller share of a shorter, lower-batch decode step.

Accepted as the default fused MXFP4 MoE decode path, on top of the base configuration and the TunableOp stack above.

Status: accepted. Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.

### Dword-wide packed-weight loads in the fused MXFP4 MoE kernel, on top of the permute-decode stack above, measured

A further source-level change to the same kernel's `decode16` step reads its 16-byte packed weight block as whole dwords instead of the compiler-split eight 2-byte loads the permute fix above still left in place (no new flag; see [`platforms/`](../platforms/) for the design and ISA detail). Paired end to end against the permute-decode stack above, one node per concurrency, 5 reps per side per job, identical harness-default flags otherwise, gates 13/13 on every rep of every side (20/20 reps total):

| Concurrency | median TPOT | pooled p95 TTFT turn2+ | accept length |
|:--|--:|--:|--:|
| Uncapped, 48 sessions, collapse-affected reps excluded | 18.82 -> 13.17 ms (-30.0%) | 423.9 -> 331.4 ms (-21.8%) | 2.886 vs 2.849 |
| 16-session cap | 11.37 -> 9.44 ms (-17.0%) | 288.5 -> 298.3 ms (+3.4%, inside the 10% budget) | 2.84 vs 2.85 |

The 48-session job's raw 5-rep-per-side numbers (median TPOT 21.79 to 13.23 ms, -39.3%; pooled p95 TTFT turn2+ 504.5 to 333.6 ms, -33.9%) are directionally identical but inflated on both sides by a minority of reps hit by an admission-queueing collapse, more severe on the slower base kernel; see [`../engines/sglang.md`](../engines/sglang.md) for that pitfall (candidate status, cause not yet diagnosed). Excluding those reps gives non-overlapping per-rep TPOT ranges (base 18.10-21.79 ms, cand 12.69-14.98 ms) larger than either side's own clean spread (19.6%, 17.4%). The 16-session job had zero collapse-affected reps and its own ranges are non-overlapping on their own (base 11.13-11.54 ms, cand 9.29-9.67 ms). Both concurrencies clear the acceptance rule (gates 5/5 both sides, TPOT improvement exceeds rep spread, p95 TTFT regression within 10%).

Cumulative for the stack: median TPOT at 48 sessions has moved from about 38 ms (the NEXTN speculative-decoding acceptance point above) to 13.2 ms, about 2.4x the roughly 5.5 ms weight-bandwidth floor for this kernel at decode batch sizes; per layer at decode M the MoE kernel now runs at about 1.5x its own floor.

Accepted as the default fused MXFP4 MoE weight-load path, on top of the permute-decode stack above.

Status: accepted. Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

### Stage1 scaffold-versus-template dispatch threshold retune, on top of the dword-wide-loads stack above, measured

Once dword-wide loads (above) applied to both the scaffold and the templated stage1 kernel, the scaffold-versus-template crossover this stack's own dispatch threshold was tuned against disappeared: a sweep found the templated path winning at every measured sorted-block count (160 to 1761), so `STAGE1_SCAFFOLD_BLOCK_THRESHOLD` moved from 1024 to 160, the smallest block count directly measured. See [`platforms/`](../platforms/) for the sweep and the dispatch-flip numerics note (bf16-rounding-level reduction-order noise, not a defect). Paired end to end against the dword-wide-loads stack above, one node per concurrency, 5 reps per side per job, identical harness-default flags otherwise, gates 13/13 on every rep of every side (20/20 reps total), accept_len unchanged:

| Concurrency | median TPOT | pooled p95 TTFT turn2+ |
|:--|--:|--:|
| Uncapped, 48 sessions, turn-1-admission-delay reps excluded | 13.83 -> 12.60 ms (-8.9%) | 374.9 -> 374.4 ms (flat) |
| 16-session cap | 9.91 -> 9.61 ms (-3.0%, inside rep spread) | 295.7 -> 278.9 ms (-5.7%) |

The 48-session TPOT improvement exceeds both sides' own rep-to-rep spread (base range 13.52-14.38 ms, cand range 12.56-12.66 ms) once two cand reps with a self-contained turn-1-only admission delay are set aside; p95 TTFT turn2+ is flat. At the 16-session cap the TPOT improvement is smaller than both sides' own spread and one of five paired reps reverses sign, so it does not clear the acceptance rule there, though no metric regresses.

Cumulative for the stack: median TPOT at 48 sessions has moved from about 38 ms (the NEXTN speculative-decoding acceptance point, about 7x the roughly 5.5 ms weight-bandwidth floor) to 12.6 ms, about 2.3x that same floor. Accepted as the default at 48-session concurrency; not yet an unconditional default, since the 16-session result does not clear the acceptance rule (no regression there either).

Status: accepted at 48-session concurrency (job-verified); not confirmed as an unconditional default at a 16-session cap (no regression, improvement inside rep-to-rep noise). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

### Decode round breakdown and prefill, final re-profile on the accepted stack (H-A + H-C), measured

A final re-profile of the accepted stack (permute decode plus dword-wide loads plus the threshold-160 retune above), pooled medians across ranks and rounds, N=16 (n=160), previous (post-permute, pre-H-A/H-C) stack alongside:

| Block | Previous (ms) | New (ms) | Floor (ms) | New ratio to floor |
|:--|--:|--:|--:|--:|
| MoE routed total | 32.47 | 17.57 | 14.81 | 1.19x (was 2.19x) |
| Dense GEMM | 5.57 | 5.75 | 3.47 | 1.66x |
| Collectives | 3.48 | 3.58 | -- | -- |
| Gated DeltaNet | -- | 2.96 | -- | -- |
| CPU-only gap | 2.75 | 2.74 | -- | -- |
| **Round wall** | **51.11** | **36.86** | **18.28** | **2.02x** (was 2.80x) |

At N=8, MoE routed total is 1.47x its 8.78 ms floor; at N=32 it is 1.01x its 22.51 ms floor, essentially at the floor. **This crosses MoE routed from the "1.3x to 3x: tuning and overhead" band into the "under 1.3x: at the floor for this design" band** in the optimization-loop skill's own stop-criterion table. Round wall stays at 1.9x to 2.3x its own floor at every N: the non-MoE buckets did not shrink along with MoE, so they now make up most of a much smaller round.

At the prefill extend lengths this campaign tracks, GPU kernel time for routed MoE (unaffected by the aiter tuning-config first-touch lock at the 337-token point, see [`../platforms/`](../platforms/)) falls a further 50 percent at 337 tokens (80.84 to 40.09 ms) and 44 percent at 919 tokens (144.67 to 81.52 ms) versus the post-permute stack above; dense GEMM and the small buckets are flat.

**Stop criterion reached for this kernel, current state of the remaining gap.** The remaining decode-round gap over floor (about 18 ms of a 37 ms round at N=16) is now spread over five terms of 2.7 to 5.8 ms each (dense GEMM, Gated DeltaNet, collectives, CPU-only gap, attention plus the small residual), none individually above about 15 percent of the round: no single term dominates the way MoE once did. Further kernel-level work on the MoE decode path itself has reached diminishing returns (see [`../platforms/`](../platforms/) for the kernel-level ISA and counter detail and the fp8-activation design closed for the same reason); the better-targeted next iteration is one of these round-level terms, not another MoE kernel change.

Status: job-verified (N-sweep and prefill breakdown gated and cross-checked; bucket-sum vs. measured GPU-busy time within 0.4 percent). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

### Decode remainder at N=16, term by term, measured and estimated

With the MoE kernel at its floor (round wall 36.86 ms above), the remainder was decomposed term by term, with what each term is bound by and a recoverable estimate:

| Term | ms/round | Bound by | Recoverable estimate | Confidence |
|:--|--:|:--|--:|:--|
| Dense GEMM | 5.75 | TunableOp-tuned hipBLASLt or the skinny kernel; no torch fallback dispatches in production. LM head (62080x4096) is about 83 percent of total dense weight bytes; most other shapes are launch-overhead bound (under 2 MB/call) | up to 1.6 ms/round aggregate (every shape uniformly reaching 1.2x its own byte floor, an implausible upper bound); the LM head alone plausibly accounts for 0.17-0.35 ms of that | medium (byte floor solid; the LM head's production dispatch-count assumption is not) |
| Collectives | 3.6 | 124 launches/round of the aiter custom all-reduce (`aiter::cross_device_reduce_2stage`) at about 19 us/launch, already at the latency floor for this message size (128 KiB at batch 16); a faster all-reduce implementation cannot help a latency-bound message this small | 0.4-0.6 ms/round from cutting launch count (all-reduce plus RMSNorm fusion), not from a faster implementation | medium-high (per-launch latency floor is solid) |
| CPU-only gap | 2.7 | host-side scheduler bookkeeping and CPU racing ahead of the GPU; unchanged from the pre-MoE-fix profile. Largest single sub-item is the scheduler's own Python control loop, about 1 ms/round | effectively 0 from a kernel change; this is host-side Python/scheduling, outside kernel scope | high |
| Gated-DeltaNet: update kernel | 1.36 (45 launches/round) | launch granularity, not bandwidth or instruction issue: FETCH_SIZE matches its computed byte requirement almost exactly, VALU/element is only about 0.3, yet each of the 45 per-layer launches is too small (1024-4096 total waves across 228 CUs) to hide its own HBM round-trip latency | 0 (closed): layer-axis packing is exact and delivers the predicted speedup in an isolated build, but is blocked from the production call site by a sequential per-layer residual-stream dependency; the alternative lever (widening the launch's own grid) is exact but strictly slower, worse with split factor and batch; raising warp count breaks bit-identity | high (mechanism confirmed by ISA and counter audit; two independent fixes tried and closed) |
| Gated-DeltaNet: fold kernel | 0.69 (1 launch/round, all layers) | HBM bandwidth: 1.79-1.82x its own read-plus-write byte floor. The kernel's own source comment records that tuning was tried and rejected (more warps would improve latency-hiding but breaks the bit-identical reduction order the numerics contract requires) | 0 (closed, no lever short of relaxing a correctness requirement) | high |

The two audited Gated-DeltaNet kernels (1.36 + 0.69 = 2.05 ms) do not fully account for that bucket's roughly 3.0 ms total; the remainder is smaller conv/gating/split glue kernels not yet audited at the kernel level. See [`platforms/`](../platforms/) for the byte-floor tables, instruction-density counters, and the two closed kernel-level rounds behind the Gated-DeltaNet rows.

None of the five terms individually clears about 15 percent of the round, and none has an untried lever left: the Gated-DeltaNet update kernel's only concrete fix (layer-axis packing) is closed at the kernel level (mechanism confirmed exact and at the predicted magnitude, production integration architecturally blocked), the fold kernel was already closed, and dense GEMM, collectives, and the CPU-only gap are each bound by something a kernel change cannot move (launch-overhead, a per-launch latency floor, and host-side scheduling respectively). Every remaining term is under about 1.5 ms/round, this campaign's own detectable-delta floor (about 0.5 ms/token); see the stop-criterion summary below.

Status: mixed. Dense GEMM, collectives, and CPU-gap rows: job-verified (read from the same re-profile as the table above). Gated-DeltaNet classification: microbench-verified. Gated-DeltaNet update-kernel fixes: closed (both tried, neither deployable or beneficial). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13.

### TPOT track: stop criterion reached

With the update-kernel packing fix confirmed infeasible in production and its only alternative lever (grid widening) measured strictly slower (see [`platforms/`](../platforms/) for both), every term in the decode-remainder table above is now closed or bound by something a kernel change cannot move: MoE routed total at 1.19-1.2x its byte floor (the "at the floor" band, see the re-profile above); the Gated-DeltaNet fold kernel bandwidth-bound at its own floor with tuning already exhausted in source; the Gated-DeltaNet update kernel launch-granularity-bound with a production-infeasible exact fix and a tested-and-rejected alternative; collectives at the per-launch latency floor for their message size; dense GEMM launch-overhead bound below the byte-floor-dominated regime; and the CPU-only gap host-side scheduler work outside kernel scope entirely. Every one of these terms is under about 1.5 ms/round, so no further per-term kernel change can be distinguished from noise by this campaign's paired protocol.

**Closing number:** median TPOT 12.6 ms at 48 uncapped sessions, about 2.3x the roughly 5.5 ms weight-bandwidth floor for the fused MXFP4 MoE kernel at decode batch sizes (see the stage1 dispatch-threshold retune section above). The TPOT track stops here on this image: the stack accepted through this point (NEXTN k=3, overlap scheduler off, TunableOp-tuned dense GEMMs, the permute-decode and dword-wide-load MoE kernel rewrites, and the stage1 dispatch-threshold retune) is the recommended default, and the next optimization-loop iteration on this workload should target a different metric or a structural change (for example the two-pass decode-forward restructuring the Gated-DeltaNet packing round flagged as out of scope for a kernel round, see [`platforms/`](../platforms/)), not another kernel-level pass over this same decode path.

Status: verified (stop criterion; every constituent term's own status is recorded at its own row above or in [`platforms/`](../platforms/)). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.

### Turn-2+ TTFT decomposition, overlap scheduler off, measured

With the overlap scheduler off (previous section), a five-bucket, request-joined split of turn-2+ TTFT shows queue wait is near zero and rare (zero `NO_TOKEN` admission-budget rejections over 16830 iterations), and the single-request prefill+draft-extend forward is the dominant term, not the queue:

| Bucket | c48 p50 | c48 p95 | c16 p50 | c16 p95 |
|:--|--:|--:|--:|--:|
| client send -> server receipt | 1.9 ms | 3.7 ms | 2.3 ms | 3.6 ms |
| receipt -> scheduler queue arrival | 55.3 ms | 204.2 ms | 22.0 ms | 125.7 ms |
| queue wait | 0.6 ms | 1.1 ms | 0.5 ms | 1.0 ms |
| prefill + draft-extend forward | 186.1 ms | 400.8 ms | 173.3 ms | 260.8 ms |
| publish -> client receipt | 2.7 ms | 4.9 ms | 3.0 ms | 4.2 ms |

See [`../engines/sglang.md`](../engines/sglang.md) for what the receipt-to-queue-arrival term measures, and [`../tooling/performance-modeling.md`](../tooling/performance-modeling.md) for the decomposition method.

Status: verified (request-joined, 100 percent match rate both concurrencies, residual near logging precision). Stamp: sglang-v0.5.18-rocm700-mi30x, benchmark_version 4, 2026-09-12, job-verified.

**Refinement at concurrency 1 (no queueing), on top of the accepted breakable prefill CUDA graph.** The bucket table above was measured under load (c48/c16) with the overlap scheduler off, before the breakable prefill graph landed. A later, exact-rid-joined decomposition at concurrency 1 isolates the "receipt to queue arrival" term's own sub-costs from any queueing effect: scheduler pickup and `ForwardBatch` build are both under 2 ms combined at every size tested, `ModelRunner.sample()` is under 0.2 ms (a much larger profiler-based estimate for this stage does not reproduce with a low-overhead measurement, see [`../tooling/profiler.md`](../tooling/profiler.md)), NEXTN draft-extend-for-prefill is a real 4-7 ms on the critical path, and result processing plus send to detokenizer is 2.5-5 ms. Upstream of the scheduler entirely (in `TokenizerManager`, not stamped by the load-mode table above), chat-template render is a flat 0.4 ms regardless of conversation length, and the remaining 3.44-5.76 ms (256/512/1024 extend tokens) is a double tokenization pass, not IPC: a later split of this span into its 13 real sub-steps found true pickle-plus-ZMQ IPC cost near zero (0.09-0.36 ms) and traced the real cost to `serving_chat.py` decoding an already-computed `prompt_ids` back to text and sending text (because it gates on model capability, not per-request media), which `TokenizerManager` then re-encodes from scratch. Summing every stage this job measured (excluding the forward itself) accounts for essentially all of a 17-33 ms residual an earlier pass had left unattributed, closing it to 2-6 ms per size. See [`../engines/sglang.md`](../engines/sglang.md) for the corrected per-stage table, the double-tokenization mechanism, and the candidate fix, and [`../tooling/serving-benchmark.md`](../tooling/serving-benchmark.md) for the low-overhead host-stamp method.

Status: verified (exact-rid join at concurrency 1, monotonic stamps, stage sum reconstructs the measured total to within 1-2 ms). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

### Turn-2+ prefill forward, kernel breakdown, measured

A single-request (bs=1) turn-2+ prefill+draft-extend forward is compute-bound at these extend-token counts, not bandwidth-bound: 337 extend tokens, 187.2 ms wall / 164.1 ms GPU busy; 919 extend tokens, 282.0 ms wall / 263.2 ms GPU busy. OLS fit (bs=1 only, n=429): `d_ms = 43.2 + 0.341 x extend_tokens` (R^2 = 0.54).

| Block | 337 tok | 919 tok |
|:--|--:|--:|
| Routed MoE (stage1+stage2) | 91.3 ms (49%) | 168.3 ms (60%) |
| Dense GEMM | 34.5 ms | 37.7 ms |
| Collectives (TP=4 all-reduce) | 24.5 ms | 35.8 ms |

Routed MoE dominates and grows fastest with extend length; dense GEMM is nearly flat (weight-read-bound, not compute-bound, at this token range). See [`platforms/`](../platforms/) for the MoE kernel this measures and its own floor comparison.

Status: verified (kernel-named torch-profiler capture, cross-validated against the bucket decomposition above to within a few ms). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.

**Post-permute re-profile.** GPU kernel time by block (mean across ranks, two independent boots, reproduced consistently in both):

| Block | 337 tok, previous | 337 tok, post-permute | 919 tok, previous | 919 tok, post-permute |
|:--|--:|--:|--:|--:|
| Routed MoE (stage1+stage2) | 91.3 ms | about 79 ms | 168.3 ms | about 139 ms |
| Dense GEMM | 34.5 ms | flat within noise | 37.7 ms | flat within noise |
| Collectives (TP=4 all-reduce) | 24.5 ms | unreliable at this point, see pitfall below | 35.8 ms | flat within noise |

Routed MoE falls 11 to 16 percent at 337 tokens and 14 to 21 percent at 919 tokens, consistent across both boots. The 337-token point's own wall-clock, CPU-gap, and collectives numbers are unreliable in both boots: a first-touch aiter tuning-config file lock contended when several ranks resolved an untried batch shape at once, unrelated to the kernel under test; see the aiter tuning-config lock pitfall under [`platforms/`](../platforms/). GPU kernel time by block only counts kernel execution, so it is unaffected by that lock wait and is what is reported here.

Status: job-verified (GPU kernel time reproduced consistently across two independent boots; the 337-token point's wall-clock, CPU-gap, and collectives numbers are separately flagged unreliable, see the pitfall referenced above). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-12, job-verified.

### Prefill kernel-launch census, measured

A per-layer kernel-launch census at 256, 768, and 1024 extend tokens (torch-profiler traces, layer boundaries found via the marker-kernel method in [`../tooling/profiler.md`](../tooling/profiler.md)) counts about 2050-2070 total kernel launches per prefill forward, nearly independent of token count: mean 38.2 launches per Gated-DeltaNet layer (45 layers, range 38-48) and 22-23 per full-attention layer (15 layers), consistent with a fixed launch count per captured graph bucket where only per-kernel duration, not launch count, scales with real tokens. By count, 69.5 percent of launches run under 20 microseconds, but by time these are only 7.5-11.4 percent of the forward (falling as token count rises); `moe_gemm` plus dense-GEMM classes together are 75.5-80.3 percent of kernel time at every size measured. Ranked launch-fusion/removal candidates (all-reduce-plus-residual-norm epilogue fusion, MoE router/topk/align/sort launch reduction) sum to roughly 1-3 ms of removable time against the fixed per-forward cost, both requiring a real kernel change (not a Python-level fusion); this confirms launch-count reduction is a secondary lever behind the MoE and dense-GEMM kernels that already dominate time, not the primary one.

Status: verified (three-size trace census; cross-checked against independent event-timing GPU-busy numbers to within 0.1 percent at one size). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

### Outcome: resident-fp8 / MXFP4-dequant hybrid MoE weights (superseded)

The gather-dequant hybrid design (keep MXFP4 weights resident, gather-dequant only the experts a batch actually touches into a faster-precision scratch buffer per layer) was carried through to a real end-to-end test under the campaign's numerics policy: only changes that compute the same numbers as production (bf16-rounding-level differences) are admissible, since the 13-probe accuracy gate cannot itself catch a numerics-changing regression. The bf16 target passed that bar (rel L2 0.23 percent vs. production's 0.40 percent) but was a marginal, mixed result end to end: p95 TTFT turn-2+ improved 4.85 percent (649.0 to 617.6 ms) while TPOT regressed 1.58 percent (107.3 to 109.0 ms), because the dequant-into-CK's-preshuffled-layout write traffic largely canceled the CK kernel's own speed advantage. The fp8 target failed the pre-benchmark accuracy gate (6 of 13 probes, garbage output on history and arithmetic probes) because per-token activation quantization changes the computed numbers, which the exact-only policy excludes regardless of speed.

Both hybrid targets are superseded by the from-scratch fused HIP MoE kernel (see the Measured table above and [`platforms/`](../platforms/) for the kernel), which computes exact bf16-rounding-level numbers and wins outright rather than trading TTFT against TPOT.

Scope: rocm, gfx942, aiter d9e5ef7ce0. Status: refuted as a serving candidate (bf16: exact but not a net win; fp8: excluded by the exact-only numerics policy). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-11, job-verified.

## See also

- [`ssm-hybrid.md`](ssm-hybrid.md): the hybrid SSM+attention cache model this model uses
- [`text-moe.md`](text-moe.md): the MoE routing and dispatch pattern this model uses
- [`../algorithms/heterogeneous-kv-cache.md`](../algorithms/heterogeneous-kv-cache.md): allocator for mixed KV + Mamba state
- [`../algorithms/moe-routing-dispatch.md`](../algorithms/moe-routing-dispatch.md): routing and dispatch mechanics
- [`../algorithms/parallelism.md`](../algorithms/parallelism.md): TP sharding of MoE experts on the intermediate dim
- [`../algorithms/quantization-schemes.md`](../algorithms/quantization-schemes.md): MXFP4 hardware support
