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

Status: verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-05, job 623402.

### Hybrid cache state

Each Gated-DeltaNet layer carries a per-request conv state (kernel 4) and an SSM state; these live in a Mamba cache alongside the paged KV cache the 15 full-attention layers use. See [`ssm-hybrid.md`](ssm-hybrid.md) for the hybrid cache model and [`../algorithms/heterogeneous-kv-cache.md`](../algorithms/heterogeneous-kv-cache.md) for the allocator that must size both pools together.

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

## Measured

| Metric | Pre-kernel baseline (baseline_v1cfg) | Accepted config: fused MoE + skinny GEMM + mixed chunked prefill (defaults) | Scope |
|:--|:--|:--|:--|
| Mean TPOT | 106.8 ms | 21.7 ms | 4x MI300A, TP=4, 48 concurrent multi-turn sessions, admission-aware open-loop schedule (benchmark_version 3) |
| p95 TTFT, turn 2+ | 818 ms (pooled per-turn, n=830) | 455 ms (pooled per-turn, n=830) | same |
| Throughput | ~102 tok/s | ~104 tok/s | same; throughput is fixed by the open-loop schedule's offered rate (every side in the matrix below lands within about 2 percent of the others), so it is not a useful differentiator at this load |

Accepted config: `SGLANG_MXFP4_MOE_HIP=1`, `SGLANG_SKINNY_GEMM=1`, plus mixed chunked prefill (`--enable-mixed-chunk --chunked-prefill-size 1024`); see [`platforms/`](../platforms/) for the backend-specific kernels and [`../algorithms/chunked-prefill.md`](../algorithms/chunked-prefill.md) for the chunked-prefill contract. Baseline: Triton MXFP4 MoE fallback kernel with hipBLASLt default dense GEMMs, all three off/unset.

Four-side pooled per-turn TTFT quantiles under the admission-aware open-loop schedule (job 632958, 5 reps per side pooled; see [`../tooling/serving-benchmark.md`](../tooling/serving-benchmark.md) for why pooled quantiles, not per-rep percentiles, are the metric of record here), p50 / p90 / p95 / p99, plus each side's median mean TPOT and `schedule_bound_fraction` range across its 5 reps:

| Side | p50 | p90 | p95 | p99 | Median mean TPOT | schedule_bound_fraction |
|:--|:--|:--|:--|:--|:--|:--|
| baseline_v1cfg (all off) | 455.1 ms | 630.9 ms | 818.4 ms | 1252.5 ms | 106.83 ms | 0.19-0.38 |
| sched_only (schedule only, kernels off) | 464.1 ms | 618.2 ms | 675.9 ms | 1071.4 ms | 98.18 ms | 0.82-1.00 |
| fused_only (fused MoE HIP kernel) | 254.9 ms | 382.7 ms | 466.9 ms | 870.6 ms | 47.51 ms | 0.99-1.00 |
| defaults (fused MoE + skinny GEMM + mixed chunked prefill) | 209.0 ms | 340.8 ms | 455.0 ms | 865.7 ms | 21.74 ms | 1.00 |

Paired deltas vs. baseline_v1cfg (pooled p95 TTFT turn-2+, median TPOT): sched_only -17.4 percent / -8.1 percent; fused_only -42.9 percent / -55.5 percent; defaults -44.4 percent / -79.6 percent.

Under fixed pacing, TPOT reflects offered load as well as kernel speed: a server fast enough to keep up with the schedule runs smaller batches than one held at the concurrency cap, so a side's own TPOT drops as it moves from load-bound to schedule-bound. The `defaults` stack measures 21.7 ms TPOT at this load (schedule-bound, `schedule_bound_fraction` 1.00) versus about 35 ms for the same kernel stack at a full batch of 16 (load-bound, concurrency-capped pacing). Compare TPOT rows only across sides measured at the same offered load and concurrency cap; see [`../tooling/serving-benchmark.md`](../tooling/serving-benchmark.md).

The custom skinny GEMM kernel alone (without mixed chunked prefill) was found at n=5 to raise p95 TTFT turn-2+ about 38 percent versus fused-only rather than leave it unchanged (an earlier n=3 result had shown the opposite ranking, which does not reproduce). Mixed chunked prefill on top of skinny GEMM reverses that regression, landing close to (about 12 percent above) fused-only's p95 while keeping the full TPOT win. This is a benchmark_version 2 finding: the benchmark_version 3 four-side matrix above has no skinny-GEMM-alone side, so treat it as provisional pending a v3 rerun.

Status: verified (four-side benchmark_version 3 matrix, 5 reps per side pooled); skinny-GEMM-alone reversal is a benchmark_version 2 finding, not yet rerun under v3. Stamp: sglang-v0.5.18 fork (`moe/mxfp4-fused` + `gemm/skinny` + `bench/admission-schedule`), benchmark_version 3, 2026-09-11, job 632958.

### Load dependence

At unlimited concurrency (48 sessions, benchmark_version 4, the 16-slot admission cap lifted, job 632990), the accepted configuration holds the schedule (`schedule_bound_fraction` 1.00): it paces 1.17 turns per second with pooled p95 TTFT turn-2+ 535 ms and mean TPOT 61 ms at 218 tok/s. The all-off baseline cannot hold the schedule at this load (`schedule_bound_fraction` 0, every rep): it degenerates to a closed-loop capacity measurement, 175 ms TPOT, 168 tok/s, pooled p95 TTFT turn-2+ 922 ms. The same accepted configuration at a 16-session cap on the same node holds pooled p95 TTFT turn-2+ at 338 ms and TPOT at 21.4 ms.

TPOT and p95 TTFT are both functions of offered load and the concurrency cap, not of kernel speed alone: compare rows only across sides measured at equal load and cap. See [`../tooling/serving-benchmark.md`](../tooling/serving-benchmark.md).

Status: verified. Stamp: sglang-v0.5.18 fork, benchmark_version 4, 2026-09-11, job 632990.

Decode-step time is dominated by the MoE expert FFN, not by the mixer or the collectives: MoE work accounts for roughly three-fifths of decode-step device time, with most of the remainder in the model's other dense (non-expert) GEMMs; collective and mixer time is small by comparison. This was confirmed by a kernel-level profile, reproduced twice. The specific kernel names and per-component percentages are implementation details of the selected backend's MoE and GEMM kernel choice; see [`platforms/`](../platforms/) for the selected backend's kernel notes, not repeated here. With the fused HIP MoE kernel in place, MoE is still the largest decode-step term, but a counter-level profile shows it is latency-bound on the decode ALU dependency chain (LUT lookup, exponent add, bf16 cast, feed into MFMA), not bandwidth-bound; see [`platforms/`](../platforms/) for the counters that separate the two.

Status: verified (three-fifths-of-decode-time finding reproduced twice; fused-kernel latency-bound finding verified once). sglang-v0.5.18-rocm700-mi30x, 2026-09-11.

### Outcome: resident-fp8 / MXFP4-dequant hybrid MoE weights (superseded)

The gather-dequant hybrid design (keep MXFP4 weights resident, gather-dequant only the experts a batch actually touches into a faster-precision scratch buffer per layer) was carried through to a real end-to-end test under the campaign's numerics policy: only changes that compute the same numbers as production (bf16-rounding-level differences) are admissible, since the 13-probe accuracy gate cannot itself catch a numerics-changing regression. The bf16 target passed that bar (rel L2 0.23 percent vs. production's 0.40 percent) but was a marginal, mixed result end to end: p95 TTFT turn-2+ improved 4.85 percent (649.0 to 617.6 ms) while TPOT regressed 1.58 percent (107.3 to 109.0 ms), because the dequant-into-CK's-preshuffled-layout write traffic largely canceled the CK kernel's own speed advantage. The fp8 target failed the pre-benchmark accuracy gate (6 of 13 probes, garbage output on history and arithmetic probes) because per-token activation quantization changes the computed numbers, which the exact-only policy excludes regardless of speed.

Both hybrid targets are superseded by the from-scratch fused HIP MoE kernel (see the Measured table above and [`platforms/`](../platforms/) for the kernel), which computes exact bf16-rounding-level numbers and wins outright rather than trading TTFT against TPOT.

Scope: rocm, gfx942, aiter d9e5ef7ce0. Status: refuted as a serving candidate (bf16: exact but not a net win; fp8: excluded by the exact-only numerics policy). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-11, job 632232.

## See also

- [`ssm-hybrid.md`](ssm-hybrid.md): the hybrid SSM+attention cache model this model uses
- [`text-moe.md`](text-moe.md): the MoE routing and dispatch pattern this model uses
- [`../algorithms/heterogeneous-kv-cache.md`](../algorithms/heterogeneous-kv-cache.md): allocator for mixed KV + Mamba state
- [`../algorithms/moe-routing-dispatch.md`](../algorithms/moe-routing-dispatch.md): routing and dispatch mechanics
- [`../algorithms/parallelism.md`](../algorithms/parallelism.md): TP sharding of MoE experts on the intermediate dim
- [`../algorithms/quantization-schemes.md`](../algorithms/quantization-schemes.md): MXFP4 hardware support
