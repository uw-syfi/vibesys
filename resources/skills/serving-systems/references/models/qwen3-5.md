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

| Metric | Accepted config | Pre-kernel baseline | Scope |
|:--|:--|:--|:--|
| Decode step (TPOT) | 35.6 ms | 106.8 ms | 4x MI300A, TP=4, 16 concurrent multi-turn sessions, scheduled pacing |
| p95 TTFT, turn 2+ | 459 ms | 785 ms | same |
| Throughput | 181 tok/s | 108 tok/s | same |

Accepted config: fused MXFP4 HIP MoE kernel plus custom skinny bf16 GEMM kernel (`SGLANG_MXFP4_MOE_HIP=1`, `SGLANG_SKINNY_GEMM=1`); see [`platforms/`](../platforms/) for the backend-specific kernels. Baseline: Triton MXFP4 MoE fallback kernel with hipBLASLt default dense GEMMs, both kernel flags off.

Status: verified. sglang-v0.5.18 fork (`moe/mxfp4-fused` + `gemm/skinny`), 2026-09-11, job 632503 (accepted config, median of 3 reps) and job 632483 (baseline, median of 3 reps).

Decode-step time is dominated by the MoE expert FFN, not by the mixer or the collectives: MoE work accounts for roughly three-fifths of decode-step device time, with most of the remainder in the model's other dense (non-expert) GEMMs; collective and mixer time is small by comparison. This was confirmed by a kernel-level profile, reproduced twice. The specific kernel names and per-component percentages are implementation details of the selected backend's MoE and GEMM kernel choice; see [`platforms/`](../platforms/) for the selected backend's kernel notes, not repeated here.

Status: verified (reproduced twice). sglang-v0.5.18-rocm700-mi30x, 2026-09-11.

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
