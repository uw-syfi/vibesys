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

Decode-step time is dominated by the MoE expert FFN, not by the mixer or the collectives: MoE work accounts for roughly three-fifths of decode-step device time, with most of the remainder in the model's other dense (non-expert) GEMMs; collective and mixer time is small by comparison. This was confirmed by a kernel-level profile, reproduced twice. The specific kernel names and per-component percentages are implementation details of the selected backend's MoE and GEMM kernel choice; see [`platforms/`](../platforms/) for the selected backend's kernel notes, not repeated here.

Status: verified (reproduced twice). sglang-v0.5.18-rocm700-mi30x, 2026-09-11.

### Candidate: resident-fp8 / MXFP4-dequant hybrid MoE weights

The full expert set does not fit resident on one 4x MI300A node at a faster-than-MXFP4 precision (fp8 resident experts measured at about 388 GB of the ~430 GB free on one 4x MI300A node; see [`platforms/`](../platforms/) for the kernel benchmark this is based on). A hybrid design (keep MXFP4 weights resident, gather-dequant only the experts a batch actually touches into a faster-precision scratch buffer per layer) is under test as a way to get faster-than-MXFP4 compute without the memory cost of full resident conversion. A first (Triton dequant) implementation of the gather-dequant step was slower than the MXFP4 baseline it was meant to replace, so the mechanism is not yet net-positive.

What would verify it: a gather-dequant implementation whose per-layer overhead is smaller than the compute time it saves versus running MXFP4 directly, measured end-to-end against the MXFP4 baseline at production batch sizes.

Scope: any model with a routed-MoE expert set too large to hold resident at a fast precision on the target node; backend-independent in shape, though the specific kernels are platform work. Status: candidate. Stamp: `sglang-v0.5.18-rocm700-mi30x`, 2026-09-11.

## See also

- [`ssm-hybrid.md`](ssm-hybrid.md): the hybrid SSM+attention cache model this model uses
- [`text-moe.md`](text-moe.md): the MoE routing and dispatch pattern this model uses
- [`../algorithms/heterogeneous-kv-cache.md`](../algorithms/heterogeneous-kv-cache.md): allocator for mixed KV + Mamba state
- [`../algorithms/moe-routing-dispatch.md`](../algorithms/moe-routing-dispatch.md): routing and dispatch mechanics
- [`../algorithms/parallelism.md`](../algorithms/parallelism.md): TP sharding of MoE experts on the intermediate dim
- [`../algorithms/quantization-schemes.md`](../algorithms/quantization-schemes.md): MXFP4 hardware support
