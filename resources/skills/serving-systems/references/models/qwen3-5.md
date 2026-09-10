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

| Metric | Value | Scope |
|:--|:--|:--|
| Decode step (TPOT) | ~106 ms | 4x MI300A, TP=4, Triton MXFP4 MoE fallback kernel, 16 concurrent multi-turn sessions |
| p95 TTFT, turn 2+ | 662 to 704 ms (five samples) | same |

Status: verified. sglang-v0.5.18-rocm700-mi30x, 2026-09-05.

Candidate: the MoE grouped GEMMs dominate the decode step. Unprofiled; a roofline for 17B active parameters at 4 bits over 4 devices is under 10 ms, well below the measured step time. What would verify it: a kernel-level profile attributing decode-step time to the MoE GEMMs versus the mixer and collectives. The fallback kernel this was measured with is a platform-specific implementation detail; see [`platforms/`](../platforms/) for the selected backend's kernel notes, not repeated here.

## See also

- [`ssm-hybrid.md`](ssm-hybrid.md): the hybrid SSM+attention cache model this model uses
- [`text-moe.md`](text-moe.md): the MoE routing and dispatch pattern this model uses
- [`../algorithms/heterogeneous-kv-cache.md`](../algorithms/heterogeneous-kv-cache.md): allocator for mixed KV + Mamba state
- [`../algorithms/moe-routing-dispatch.md`](../algorithms/moe-routing-dispatch.md): routing and dispatch mechanics
- [`../algorithms/parallelism.md`](../algorithms/parallelism.md): TP sharding of MoE experts on the intermediate dim
- [`../algorithms/quantization-schemes.md`](../algorithms/quantization-schemes.md): MXFP4 hardware support
