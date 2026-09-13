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

## Closed: pack the layer axis into the update kernel's grid

**Feasibility check, done before building anything:** read the call site (`gdn_backend.py`'s per-layer verify path, called once per `RadixLinearAttention` layer module's own `forward()`) and the decoder stack. The update kernel's 45 per-layer inputs are never simultaneously available at any single point in the decode path: each layer's call consumes that layer's own freshly-computed q/k/v (built from that layer's own conv update on that layer's own input hidden state) and returns `o`, the attention output threaded straight into that layer's residual stream, which becomes the input the next layer's own conv/projections need. Layer *i*+1's inputs do not exist until layer *i*'s own forward, including this call, has finished. The fold kernel, by contrast, runs once per round after every layer has already finished its own verify step, purely on data that already lives as `[num_layers, ...]` stacked tensors in the Mamba pool, with no live dependency on any other layer's forward at the point it runs. Packing 45 layers into one launch is therefore blocked by a sequential residual-stream dependency across decoder layers, not by an engineering-cost item, and no change confined to this kernel or its call site can reach it. The destination ring buffers the update kernel writes are already the same all-layers-stacked tensors the fold kernel reads, so storage layout is not the blocker; the blocker is that the update kernel also computes and returns `o`, a value with no fold-kernel analog, and deferring its ring write until all layers finish would require keeping every layer's per-token intermediates alive across the loop (no cheaper than running the kernel per layer) or recomputing them later, which changes the numerics path.

**Isolated diagnostic, never wired into the production call site:** built the layer-packed kernel behind a standalone entry point only, exercised from a microbenchmark and unit tests, to check whether the mechanism itself (fewer, larger launches) delivers the predicted gain in isolation. Exact match against the 45-separate-launch baseline (`torch.equal`) and 6.17x / 2.50x / 1.82x faster at batch 16 / 48 / 64 respectively, the same shrinking-with-batch shape the byte-floor model predicted for launch-count reduction. This confirms the launch-granularity mechanism is real; it does not make the change deployable, since the call site it would need to run at is the one shown infeasible above.

**Pitfall found while building the diagnostic kernel:** the production call site flattens q/k/v to a leading batch dim of 1 and carries the true per-round request count in `cu_seqlens`, not in the tensor's own batch dimension. A first version of the packed-grid decomposition derived the layer and per-item grid indices by dividing out the tensor-shape constexpr `B`, which is silently wrong whenever `B` (the flattened dim, 1) differs from the true request count carried in `cu_seqlens`, i.e. always in production. Fix: pass a separate `GRID_N` constexpr equal to the true batch, set explicitly by the caller, and never derive a grid index from a tensor's own leading dimension when a call site may flatten that dimension into a `cu_seqlens` layout. See [`../../frameworks/triton.md`](../../frameworks/triton.md) for the general form of this pitfall.

Status: closed (production integration infeasible; isolated mechanism confirmed exact and matching the predicted magnitude). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.

## Rejected: widen the launch's grid instead of packing layers (V-tile split)

**Hypothesis:** since packing the layer axis is blocked at the call site, subdivide the update kernel's existing V-tile (more grid programs, more waves in flight) within the current 45-launch structure, to add latency-hiding without changing launch count.

**Result: exact but strictly slower at every batch, worse as the split factor grows and as batch grows.** Batch 16: 1.02x/1.06x/1.09x slower at split factor 2/4/8; batch 48: 1.09x/1.16x/1.35x slower; batch 64: 1.07x/1.18x/1.40x slower, the opposite of both predicted trends. `torch.equal` holds at every split factor.

**Why:** launch count stays fixed at 45; each extra V-slice program reloads and re-derives q, k, gate, and beta and redoes the L2-norm and gating-scalar math for its own slice, work that used to run once per (n, hv) program and now runs once per slice. This adds real cost with no offsetting benefit, because occupancy at these batches was already adequate under the unmodified grid (VALUBusy already rises smoothly with batch, the same latency-hiding signature seen without this change). Grid-widening and launch-count reduction are not the same lever for a launch-granularity-bound kernel: only cutting the number of launches removes fixed per-launch cost, and widening a launch's own grid without changing launch count instead adds real, avoidable per-program setup cost.

**`num_warps` in {2, 4}: rejected on the exactness gate.** Raising warp count changes this kernel's own reduction order and breaks bit-identity against the single-warp baseline (max abs diff 1.9e-6 to 9.8e-4, growing with batch) at every batch tested. This is a separate finding from the fold kernel's own previously-documented `num_warps` sensitivity above: two different kernels, each with its own reduction tree, each independently found warp-count-sensitive.

Status: verified (refuted as a performance change; num_warps rejected on exactness). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, microbench-verified.

## Prefill: chunked-scan capture is blocked at the design level; the underlying sync is not itself a lever

The decode-side kernels above are captured under NEXTN's replayssm-spec fast path; the prefill-side chunked-scan pipeline (`chunk_local_cumsum`, `chunk_gated_delta_rule_fwd_intra`, `chunk_gated_delta_rule_fwd_h`, `chunk_fwd_o` in `kernels/ops/attention/fla/{cumsum,chunk_fwd,chunk_delta_h,chunk_o}.py`, plus `causal_conv1d_fn`'s own grid axis) is not, and cannot be folded into the breakable prefill CUDA graph without a multi-file kernel redesign.

**Why it is blocked.** These kernels size their launch grid, and `chunk_delta_h.py` additionally sizes an intermediate tensor allocation (`h = k.new_empty(B, NT, H, V, K)`), from `NT`, the batch's total chunk count at a fixed 64-token chunk size. `NT` is computed via `prepare_chunk_indices(cu_seqlens, 64)` (`kernels/ops/attention/fla/index.py:16-26`), which calls `.tolist()`, a hard device-to-host synchronization that CUDA graph capture does not tolerate (a capture-time error, not a performance issue). `NT` depends on how a batch's tokens split across requests, not on the padded token bucket alone (a 100+156-token co-batch at the same 256-token bucket as a single 256-token request gives `NT=5` versus `NT=4`), so it cannot be fixed as a bucket constant the way a graph capture needs. Two smaller per-request dependencies (`real_num_tokens` slicing, `has_initial_states`/`cache_indices`) are already stage-able, and three ops (`fused_gdn_gating`, `fused_qkv_split_gdn_prefill`, `l2norm_fwd`) are already bucket-constant; the blocker is specific to the four chunked-scan kernels and `causal_conv1d_fn`'s grid axis, which together are the majority of a GDN layer's own prefill compute. Fixing it would need computing chunk indices on-device (no host round-trip) into a static per-bucket worst-case `NT_max` buffer, rewriting all four kernels to launch `NT_max` programs with a per-program early-exit on padding slots while preserving bit-exact reduction order, plus the equivalent fix for `causal_conv1d_fn`: a multi-day kernel redesign with its own exactness suite, not a plumbing change.

**The host sync itself, measured, is not a lever worth chasing even if it were cheap to remove.** `prepare_chunk_indices` and `prepare_lens` are wrapped in a 4-entry, identity-keyed `@tensor_cache` (`kernels/ops/attention/fla/utils.py:103-142`). `ChunkGatedDeltaRuleFunction.forward` calls `prepare_chunk_indices` once per GDN layer, but `cu_seqlens` (`forward_metadata.query_start_loc`, set once per forward and read, not reassigned, by all 45 layers) and the chunk size (a CPython int singleton) are the same objects on every call within one forward, so only the first GDN layer is a cache miss; layers 2-44 hit the cache and never sync. Re-parsing existing torch-profiler traces for `hipMemcpyWithStream` events mapped against every `ChunkGatedDeltaRuleFunction` span confirms this directly: 3 memcpys (41.7 microseconds total) inside GDN layer 0 only, zero inside layers 1-44, at every extend-token size checked. A hypothesis that this mechanism cost 20-40 ms per forward (roughly 44x more syncs than actually exist) is refuted by three to four orders of magnitude; removing this sync is not a lever, independent of whether the capture blocker above is ever fixed.

Scope: rocm, this fork's chunked delta-rule kernels (chunk size 64, the varlen/`cu_seqlens` batching convention); the `NT`/chunk-count mechanism is intrinsic to the algorithm and batching convention, not specific to this checkpoint's shapes. The `@tensor_cache` identity-based deduplication is generic FLA-ops-library behavior, so the "only layer 0 syncs" finding should generalize to any GDN/KDA model on this stack reusing one `query_start_loc` object per forward. Status: capture blocker verified at the design level (code read, no job run: this is a "refuted before any implementation" finding per the campaign's own early-stop rule for exactly this blocker class); host-sync-cost refutation verified (re-parsed existing production traces, no new job). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

## Remaining lever

Fusing the update kernel with its neighbouring ops inside the same layer (its own conv/projection preamble, or the residual-add that follows) is the only lever the above does not rule out. It is a larger change than a kernel round, since it touches the per-layer op sequence rather than only a kernel's grid shape, and its predicted gain is below this campaign's own detectable-delta floor (about 1.5 ms/round). Not scheduled.

## See also

- [`speculative-decoding.md`](speculative-decoding.md): the replayssm-spec recipe these kernels run under
- [`../../models/qwen3-5.md`](../../models/qwen3-5.md): the decode-round remainder table this file's classification feeds
- [`../../tooling/performance-modeling.md`](../../tooling/performance-modeling.md): the launch-granularity-bound pitfall pattern this kernel pair is a worked example of
- [`../../tooling/profiler.md`](../../tooling/profiler.md): the issue-bound-vs-latency-bound discriminator this classification applies

## Out of scope: kernel implementation

For writing new kernels (not using this library): see agent-gpu-skills's
triton-skill / cutlass-skill / cuda-skill.
