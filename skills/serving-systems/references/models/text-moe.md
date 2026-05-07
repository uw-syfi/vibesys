# Text MoE decoders

Transformer decoders where the MLP is replaced by a sparse mixture of experts: a small gate (router) selects `k` of `N` experts per token; outputs are weighted-summed. MoE gives you more parameters for the same active-compute budget but changes nearly every operational concern — memory pressure, communication pattern, quantization story, speculative drafting, parallelism choice.

## Design spectrum: coarse-grained → fine-grained

| Axis | Coarse-grained | Fine-grained |
|:-----|:---------------|:-------------|
| Experts per layer | 8–16 | 64–256 |
| Top-k | 2 | 6–8 |
| Intermediate size per expert | large | small |
| Shared experts (always-on) | 0 | 1 (DeepSeek / Qwen3-MoE) |
| Total params | few hundred B | 400B – 1T+ |
| Active params per token | ~25% of total | ~3–10% of total |
| Example | Mixtral 8x7B, 8x22B | DeepSeek-V3 (256+1), Qwen3-MoE 235B (128+0) |

Fine-grained MoE is the modern pattern; coarse-grained stuck around via Mixtral-family finetunes.

## Example architectures

### Mixtral 8x7B (coarse, no shared expert)

- 32 layers, hidden 4096, GQA 8:1
- 8 experts per MoE block, top-2 routing, softmax gate
- No shared expert
- SwiGLU experts, intermediate 14336 each
- Relatively simple to serve — standard MoE dispatch, no group limit, no MLA, no MTP

### DeepSeek-V3 / R1 (fine-grained + MLA + MTP + FP8 block)

The most operationally demanding open architecture. Every MoE + attention optimization lands here first.

- 61 layers (first 3 dense MLP, remaining MoE)
- **MLA attention**: compressed KV latent `c_kv` (dim ~512) + small decoupled-RoPE head dim; decode uses the **query-absorption trick** to compute attention directly on compressed KV — see [`algorithms/attention-variants/`](../algorithms/attention-variants.md)
- MoE block: 256 routed experts + **1 shared expert** (always-on), top-8 with **group-limited routing** (pick top-g groups first)
- **Auxiliary-loss-free load balancing** — bias adjusted at train time; per-expert bias becomes a persistent serving-side routing offset
- **MTP heads** trained jointly — used as native speculative drafters
- **FP8 block quantization**: 1×128 activations, 128×128 weights — needs DeepGEMM or equivalent
- Requires: MLA-aware attention backend, DeepEP dispatch, FP8 block kernels, MTP speculative support

### Qwen3-MoE 30B-A3B / 235B-A22B (fine-grained, no MLA)

- Standard GQA attention + **QK norm** (like Qwen3 dense)
- 128 routed experts, top-8, no shared expert in these variants
- Auxiliary-loss-free load balancing
- Standard SwiGLU per expert
- Simpler than DeepSeek: no MLA, no MTP in base variants (though Qwen3-Next / Qwen3-MoE-Next add MTP)
- Qwen3-VL-MoE and Qwen3-Omni-MoE extend to multimodal

### Llama-4 Scout / Maverick

- Llama-family MoE variants (released 2025)
- Coarse-ish (16 experts typical), GQA attention
- Serving path in most engines builds on the Llama-dense path plus MoE block swap

## Core MoE operations

```
hidden → router_logits → top_k + softmax → (token_idx, expert_idx, weight)
hidden → DISPATCH (permute / all-to-all) → experts (grouped-GEMM) → COMBINE → weighted sum
```

Plus optional:
- shared expert (always-on, runs in parallel)
- residual + MoE output back into the block

See [`algorithms/moe-routing-dispatch/`](../algorithms/moe-routing-dispatch.md) for routing variants, dispatch kernels (padded / permutation / DeepEP / Mori / NIXL-EP), expert-FFN kernel families (grouped-GEMM, Marlin-MoE, DeepGEMM, FlashInfer-MoE), and EPLB.

## Routing variants

| Variant | Description | Models |
|:--------|:------------|:-------|
| Softmax top-k | classic `topk(softmax(logits))` | Mixtral |
| Group-limited top-k | pick top-g expert groups first, then top-k within | DeepSeek V3 |
| Auxiliary-loss-free | bias-adjusted during train; persistent bias at serve time | DeepSeek V3, Qwen3-MoE |
| Sinkhorn / normalized | balanced routing by construction | GLaM (research) |

## Weight-key conventions

Canonical HF layout with expert index in the key:

```
model.layers.<i>.mlp.gate.weight                           # router
model.layers.<i>.mlp.experts.<e>.gate_proj.weight
model.layers.<i>.mlp.experts.<e>.up_proj.weight
model.layers.<i>.mlp.experts.<e>.down_proj.weight
model.layers.<i>.mlp.shared_expert.*                       # DeepSeek / Qwen3-MoE subset
```

Engines almost always fuse per-layer experts into a 3D tensor `(num_experts, intermediate_size, hidden_size)` for grouped-gemm. The loader concatenates individual HF per-expert tensors into the fused shape, preserving expert ordering.

Auxiliary-loss-free checkpoints also carry per-expert bias:

```
model.layers.<i>.mlp.gate.e_score_correction_bias         # DeepSeek V3
```

This bias is loaded as a regular parameter and applied at serve time.

## Parallelism: DP-attention + EP-MoE is the modern default

Fine-grained MoE with hundreds of experts doesn't fit a naive TP=N layout — too much replicated attention, too little expert sharding. The canonical layout:

- **Attention**: Data Parallel (each rank holds full attention weights, different request batches)
- **MoE**: Expert Parallel (experts shard across ranks; all-to-all dispatch/combine)

Matches how [`algorithms/parallelism/`](../algorithms/parallelism.md) describes the pattern. Prefer this over TP unless the model is actually small enough that TP fits cleanly.

## Pitfalls

- **Pointing a non-MLA attention backend at DeepSeek.** Silent OOM (tries to allocate dense KV for 256 heads) or wrong semantics.
- **Expert ordering mismatches.** Checkpoints from different frameworks may have different canonical orderings. Accuracy "looks off" but not crashing → check expert index mapping.
- **Shared expert double-count.** Shared expert must not be counted in the 256 routed experts in DeepSeek; top-k indexes routed only.
- **Routing bias forgotten.** Auxiliary-loss-free models need the per-expert bias at serve time; missing it skews routing.
- **Group-limited routing done wrong.** Must do top-groups first, then top-k within groups. Filtering after a full top-k is silently different math.
- **Top-k config attribute name.** `num_experts_per_tok` vs `topk` vs `k` — standardize.
- **FP8 block alignment.** DeepSeek-style 128×128 block quant requires hidden/intermediate dims divisible by 128. Custom fine-tunes may not be aligned.
- **CUDA graph + all-to-all.** Expert imbalance makes all-to-all sizes dynamic; either bucket or keep MoE layers eager.
- **MTP token-budget accounting.** MTP commits N extra tokens on accept; scheduler's budget and KV allocation must reflect the max acceptance.
- **TP=N replicating every expert.** Ruins the MoE memory advantage. Use EP.

## See also

- [`algorithms/attention-variants/`](../algorithms/attention-variants.md) — MLA in context with other head-sharing schemes
- [`algorithms/moe-routing-dispatch/`](../algorithms/moe-routing-dispatch.md) — routing + dispatch + expert-FFN kernels + EPLB
- [`algorithms/parallelism/`](../algorithms/parallelism.md) — DP-attention + EP-MoE canonical layout
- [`algorithms/speculative-decoding/`](../algorithms/speculative-decoding.md) — MTP as draft
- [`algorithms/quantization-schemes/`](../algorithms/quantization-schemes.md) — FP8 block, Marlin-MoE, FlashInfer-MoE
- [`models/text-dense/`](text-dense.md) — non-MoE counterpart
