# Gated DeltaNet decode kernels (replayssm-spec fast path)

Kernel-level audit of the two Triton kernels the NEXTN replayssm-spec fast path dispatches per decode round, classifying each as bandwidth-bound or launch-granularity-bound so a kernel-level optimization round can be scoped correctly.

## Prerequisites

- [`speculative-decoding.md`](speculative-decoding.md): the `--enable-linear-replayssm-spec` recipe these kernels belong to.
- [`../../models/qwen3-5.md`](../../models/qwen3-5.md): 45 Gated-DeltaNet layers, 16 k-heads, 64 v-heads, head_dim 128.

## Kernels and call pattern

Two Triton kernels, both `num_warps=1` by construction (the fold kernel's docstring requires it to keep its reduction tree bit-identical to the recurrent baseline):

| Kernel | Launches/round | What it does |
|:--|--:|:--|
| `fused_sigmoid_gating_delta_rule_update_kernel` | 45 (once per layer, verify step only) | Reads the per-layer state once; production call site sets `disable_state_update=True`, so it never writes state back |
| `gdn_replayssm_exact_fold_kernel` | 1 (all 45 layers fused into one launch) | The real, once-per-round state read-and-write: batch-corrects state using confirmed tokens after a verify step |

The update kernel's 45-launch count is 45 layers times one verify call per round (T = `speculative_num_draft_tokens` = 4 tokens), not 45 layers times the 3 separate draft substeps: it fires only on the verify step. Because production sets `disable_state_update=True`, the update kernel's real traffic is close to read-only, about half the naive read-plus-write byte floor an estimate that assumes symmetric read/write would predict. The actual state mutation happens entirely inside the fold kernel's single all-layer launch.

Shapes at TP=4: H=4 k-heads/rank, HV=16 v-heads/rank, K=V=128, 45 layers, `mamba_ssm_dtype=float32` (state and gate/beta rings are fp32; the raw-value rings are the conv dtype, bf16).

Status: verified (production call site and shapes read from source and cross-checked against the compiled kernel's own `constexpr` dump). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.

## Byte floor vs. measured time

Per-rank bandwidth: 5.3 TB/s (same figure as [`floor.md`](floor.md)).

**`fused_sigmoid_gating_delta_rule_update_kernel`** (per layer, one verify call, read-only state):

| B | computed bytes | byte floor | measured | ratio to floor | FETCH_SIZE vs. computed | VALUBusy |
|--:|--:|--:|--:|--:|--:|--:|
| 16 | 17.77 MB | 3.35 us | 29.24 us | 8.7x | 1.00x | 26.1% |
| 64 | 71.1 MB | 13.4 us | 70.48 us | 5.3x | 1.00x | 59.5% |

**`gdn_replayssm_exact_fold_kernel`** (1 launch, all 45 layers, real read+write):

| B | computed read bytes | read+write floor | measured | ratio to floor | FETCH_SIZE vs. computed | VALUBusy |
|--:|--:|--:|--:|--:|--:|--:|
| 16 | 755.1 MB | 0.285 ms | 0.518 ms | 1.82x | 1.01x | 36.6% |
| 64 | 3.02 GB | 1.140 ms | 2.044 ms | 1.79x | 1.01x | 76.7% (mean; noisy, 38-94% range) |

FETCH_SIZE matches the computed-bytes model to within 1 percent at every point for both kernels: the byte-floor model is correct, and neither kernel over-fetches beyond what it needs.

Cross-validation against production: the update kernel's B=16 microbenchmark median (29.24 us) matches the production per-launch figure (30.1 us) to within 3 percent, and the aggregate over 45 layers (1.316 ms) matches the production round figure (1.356 ms) to within 3 percent as well, confirming the microbenchmark's shapes and call convention reproduce the real decode round.

Status: verified. Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.

## Instruction density

| Kernel | n_regs | n_spills | LDS | VALU/element (static) | VALU/element (PMC-derived, B=16) |
|:--|--:|--:|--:|--:|--:|
| `fused_sigmoid_gating_delta_rule_update_kernel` | 145 | 0 | 64 B | 0.300 | ~0.26-0.29 |
| `gdn_replayssm_exact_fold_kernel` | 144 | 0 | 0 B | 0.153 | ~0.12-0.15 |

Zero spills and negligible LDS: neither kernel is register- or LDS-capped. Both VALU/element figures sit far under the roughly 8 VALU/element issue-bound threshold this campaign uses elsewhere (see [`../../tooling/performance-modeling.md`](../../tooling/performance-modeling.md)); by the instruction-density discriminator, neither kernel is issue-bound at any measured batch.

## Classification

**`gdn_replayssm_exact_fold_kernel`: bandwidth-bound, at its floor, no lever available.** Ratio to its own read+write byte floor is a tight 1.79-1.82x at both B=16 and B=64, the "1.3x to 3x: tuning and overhead" band in the optimization-loop skill's stop-criterion table, not the "far over floor" band the update kernel below is in. The kernel's own source carries a prior verdict directly above its state-tile load: tuning was tried and rejected (`num_stages` a no-op, `num_warps > 1` breaks the bit-identical reduction order the fold's own numerics contract requires, `evict_first` loses cold-L2 benefit). Increasing warp count, the one change that would plausibly improve latency-hiding here, is blocked by a correctness requirement, not a performance one. No further kernel-level lever exists for this kernel.

**`fused_sigmoid_gating_delta_rule_update_kernel`: launch-granularity/occupancy-bound, not bandwidth-saturated and not instruction-issue-bound.** FETCH_SIZE matches the computed byte requirement almost exactly (1.00x), but achieved fetch rate is only about 607 GB/s at B=16 (11.5 percent of the 5.3 TB/s peak) and measured time is 8.7x the byte floor: a kernel that had actually saturated bandwidth would not be 8.7x its own floor. VALU/element (0.26-0.30) rules out instruction-issue-bound at either batch size, despite VALUBusy rising with batch (occupancy improving from about 4.5 to 18 waves/CU as batch grows from 16 to 64, the same latency-hiding signature the fold kernel shows, not added compute work). The decisive comparison is against the fold kernel: it does strictly more total memory traffic per call (state read+write vs. this kernel's read-only) yet lands far closer to its own floor (1.8x vs. 5.3-8.7x), because the fold kernel already packs all 45 layers into one launch while the update kernel is dispatched from a per-layer loop as 45 separate single-warp-per-program launches, each too small (1024-4096 total waves across 228 CUs) to hide its own HBM round-trip latency. This is a launch-count/grid-shape problem, not a memory-access-pattern or arithmetic-density problem, and the fold kernel is a working, in-repo existence proof that packing the layer axis into the grid closes most of this gap.

Status: verified (mechanism read from both kernels' source and the fold kernel's own tuning-history comment; cross-validated against production trace to within 3 percent). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.

## Candidate: pack the layer axis into the update kernel's grid

**Hypothesis:** add a third grid axis (`HV * num_layers`, mirroring the fold kernel's own `grid = (cdiv(V, BV), B, HV * num_layers)` pattern) so the update kernel's 45 per-layer launches become one launch per round, amortizing fixed per-launch dispatch and grid-setup cost across 45x more total work, the same way the fold kernel already does.

**Predicted gain:** aggregate byte floor for the update kernel across 45 layers at B=16 (read-only) is 45 x 3.35 us = 151 us. If a fused, layer-packed launch reaches an efficiency similar to the fold kernel's own measured 1.8x-of-floor (a conservative reference point, since the fused launch would have far more total waves than any current single-layer launch, i.e. more latency-hiding headroom than the fold kernel started with), predicted time is roughly 270-300 us versus the current 45-launch aggregate of 1316 us: about **1.0 to 1.05 ms/round recovered at N=16**.

**What would verify it:** a microbenchmark variant of the update kernel with the layer index folded into grid axis 2 (a `stride_*_layer` per tensor, identical to the fold kernel's own layer-offset pattern), called once per round instead of 45 times, holding total work and per-element numerics identical, compared against the current 45-launch baseline measured above. A full acceptance would separately need to confirm the fusion preserves the kernel's per-layer branches (`HAS_EAGLE_TREE_CUSTOM_ATTN_MASK`, `CACHE_INTERMEDIATE_STATES`), which currently assume a single-layer launch.

Status: candidate (mechanism plausible and grounded in the fold kernel's own working grid pattern; not built or tested). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, candidate.

## See also

- [`speculative-decoding.md`](speculative-decoding.md): the replayssm-spec recipe these kernels run under
- [`../../models/qwen3-5.md`](../../models/qwen3-5.md): the decode-round remainder table this file's classification feeds
- [`../../tooling/performance-modeling.md`](../../tooling/performance-modeling.md): the launch-granularity-bound pitfall pattern this kernel pair is a worked example of
- [`../../tooling/profiler.md`](../../tooling/profiler.md): the issue-bound-vs-latency-bound discriminator this classification applies

## Out of scope: kernel implementation

For writing new kernels (not using this library): see agent-gpu-skills's
triton-skill / cutlass-skill / cuda-skill.
