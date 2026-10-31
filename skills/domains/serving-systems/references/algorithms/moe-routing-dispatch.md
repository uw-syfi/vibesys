# MoE routing and dispatch

Move each token to its top-k experts, run the expert FFN, combine back. Serving-time cost is dominated by the dispatch/combine communication pattern and the expert-FFN GEMM kernel, not the router itself.

## Pipeline

```
logits_router → softmax / top-k → (token_idx, expert_idx, weight) tuples
    ↓                                ↓
    ↓                            dispatch: send tokens to their experts
    ↓                                ↓
(residual)                       expert FFN (grouped-GEMM)
    ↓                                ↓
    ↓                            combine: return weighted outputs
    ↓________________ + _____________↓
                       ↓
                  out hidden
```

## Routing variants

| Variant | Algorithm | Used by |
|:--------|:----------|:--------|
| Softmax top-k | `topk(softmax(logits))` | classic MoE (Mixtral) |
| Group-limited top-k | pick top-k groups, then top-k experts within those groups | DeepSeek V3 (shared + fine-grained) |
| Auxiliary-loss-free balancing | bias adjustment during training, no serving-time cost | DeepSeek V3, Qwen3-MoE |
| Sinkhorn / normalized | normalization over tokens and experts | GLaM, ST-MoE |

Shared experts (always-on experts added to the routed result): adds a fixed GEMM outside the top-k path. DeepSeek-V3 and Qwen3-MoE both use one shared expert.

## Dispatch / combine patterns

| Pattern | Communication | When |
|:--------|:--------------|:-----|
| **Padded replicate** | single all-gather | small batches, debug |
| **Permutation-based** | index scatter/gather locally | single-GPU or TP-only |
| **All-to-all dispatch** | NCCL all-to-all over EP group | multi-GPU EP, compute-heavy experts |
| **DeepEP** | NVSHMEM-backed all-to-all with overlap | Hopper+ multi-node, DeepSeek-class |
| **Mori / NIXL-EP** | RDMA-based all-to-all | latest vLLM fused-moe backends |

Permutation + all-to-all dominates on Hopper; DeepEP adds intra-/inter-node overlap for the largest deployments.

## Expert FFN kernel

The FFN is a batched GEMM where each "batch" is one expert with variable token count:

| Kernel family | Library | Notes |
|:--------------|:--------|:------|
| GroupedGEMM | CUTLASS-based | `cutlass_moe.py` family |
| Triton GroupedGEMM | Triton | portable fallback |
| Marlin-MoE | INT4 weight-only | `fused_marlin_moe.py` |
| DeepGEMM | SGLang / DeepSeek | FP8 block-quant GEMM |
| FlashInfer MoE / TRT-LLM MoE | FlashInfer / TRT-LLM | Hopper+ FP8 |

## EPLB — expert-level load balancing

Expert popularity is skewed in practice. Static assignment + skewed traffic = GPU stragglers. EPLB periodically reshuffles expert-to-GPU placement based on measured load:

- **Policy** — how to re-balance (replicate hot experts, swap, hybrid)
- **Execution** — when and how to move weights (hot-swap, double buffer)
- **Communication** — async worker that runs the rebalance without stalling

## Compatibility

| Implementation | Engines | Dispatch | Expert FFN | Hardware |
|:---------------|:--------|:---------|:-----------|:---------|
| vLLM fused_moe | vLLM | many backends (see `fused_moe/prepare_finalize/`, `.../runner/`) | Triton / CUTLASS / Marlin / DeepGEMM / FlashInfer | NVIDIA / ROCm / XPU |
| SGLang MoE | SGLang | `token_dispatcher/` (padded / DeepEP / Mori / NIXL) | `moe_runner/`, `cutlass_moe.py`, `fused_moe_triton/` | NVIDIA / ROCm |
| TensorRT-LLM MoE | TRT-LLM | C++ kernels | TRT-LLM MoE (FP8/FP4) | NVIDIA |

## Engine pointers

| Engine | Router | Dispatch / combine | Expert FFN | EPLB |
|:-------|:-------|:-------------------|:-----------|:-----|
| vLLM | `vllm/model_executor/layers/fused_moe/router/`, `.../topk_weight_and_reduce.py` | `vllm/model_executor/layers/fused_moe/prepare_finalize/` (+ `mori_prepare_finalize.py`, `nixl_ep_prepare_finalize.py`) | `vllm/model_executor/layers/fused_moe/{fused_moe,cutlass_moe,triton_cutlass_moe,fused_marlin_moe,triton_deep_gemm_moe,flashinfer_cutlass_moe}.py` | `vllm/distributed/eplb/` |
| SGLang | `python/sglang/srt/layers/moe/{router,topk}.py` | `python/sglang/srt/layers/moe/token_dispatcher/` | `python/sglang/srt/layers/moe/{moe_runner/,cutlass_moe.py,flashinfer_cutedsl_moe.py,flashinfer_trtllm_moe.py,fused_moe_triton/}` | `python/sglang/srt/layers/moe/ep_moe/` |
| TensorRT-LLM | (C++ + _torch) | `cpp/tensorrt_llm/kernels/moe*`, `_torch/` MoE modules | same | |

## Pitfalls

- **Token count per expert is dynamic.** Grouped-GEMM needs prefix-sums and careful indexing; an off-by-one silently drops tokens.
- **Top-k sampling during capture.** `torch.topk` on logits is CUDA-graph-safe, but custom top-k kernels with autotune may not be.
- **Shared expert scheduling.** Running the shared expert in parallel with dispatch saves latency but requires two independent streams or an overlapped-launch kernel.
- **EPLB concurrency.** Rebalance moves weights; attention forward must not see a partially moved expert. Double-buffer or fence.
- **Loss-free bias and serving inference.** The per-expert bias from auxiliary-loss-free training is part of the checkpoint — treat as a persistent routing offset at serve time.

## See also

- [`models/text-moe/`](../models/text-moe.md) — MoE models in practice (Mixtral, DeepSeek-V3, Qwen3-MoE, Llama-4)
- `algorithms/parallelism/` — EP + TP + DP combinations
- `algorithms/quantization-schemes/` — FP8 / FP4 MoE-specific kernels
