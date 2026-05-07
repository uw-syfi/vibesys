# Parallelism for serving

One skill covers all axes because the interesting decisions are about combinations. Each variant gets a section; the "common combinations" table is the load-bearing part.

## Axes

### Tensor Parallel (TP)

Shard each layer's weights across ranks. Column-parallel in QKV / gate-up; row-parallel in O / down; an all-reduce at the row-parallel output. Within one layer, cost = 2 collectives on the hidden dim.

- Collective: **all-reduce** (per row-parallel op)
- Scales: sub-linearly; limited by NVLink bandwidth
- Typical upper bound: TP=8 within one NVLink domain

### Sequence Parallel (SP)

Shard the *sequence dim* of activations across TP ranks between collectives, saving activation memory. Column-parallel output is `reduce-scatter`'d across seq, then `all-gather`'d before the next row-parallel input. Net collective volume is equal to TP's all-reduce but the activations are smaller.

- Collective: **reduce-scatter + all-gather** (replaces all-reduce)
- Pairs with: TP (always together)

### Pipeline Parallel (PP)

Split the layer stack across stages. Each stage processes a microbatch, passes activations to the next. Serving uses 1F1B or interleaved 1F1B to keep bubbles small.

- Communication: point-to-point (per microbatch)
- Scales: across nodes when NVLink-limited
- Cost: bubble + hidden-state P2P (small vs TP/SP)

### Data / Replica Parallel (DP)

Independent replicas sharing nothing but the external scheduler. Scales linearly for throughput, doesn't reduce single-request latency.

### Expert Parallel (EP)

Experts shard across ranks. Dispatch all-to-all sends each token to the rank holding its expert; combine all-to-all returns weighted outputs.

- Collective: **all-to-all** (per MoE layer, both dispatch and combine)
- Pairs with: DP-attention (below) or TP for attention layers

### Context Parallel / Ring Attention (CP)

For very long contexts: shard the sequence dimension across ranks and run ring-attention (chunked + point-to-point KV exchange). Used in training and some long-context serving. Less common for typical inference; relevant for 1M+ token workloads.

#### Helix (temporal CP, decode-only)

Plain ring-attention is prefill-style: each rank holds a contiguous sequence slice and rings KV around. Decode on very long contexts doesn't want that layout — the KV is already there from prefill, and spatially disaggregating attention and FFN onto separate GPUs (AFD) wastes the decode GPUs during the FFN phase.

**Helix** (TRT-LLM `cp_type: HELIX`, paper: arXiv 2507.07120) keeps the same GPUs through the whole step and *temporally* reconfigures them:

1. KV cache is partitioned across CP ranks at block granularity (so `cp_config.tokens_per_block` must match `kv_cache_config.tokens_per_block`).
2. Each rank computes partial attention over its local KV shard.
3. Partial (max, sum, output) triples are combined across ranks via an all-reduce (similar to the online-softmax reduction in flash-attention).
4. For the FFN / MoE layers in the same step, the CP ranks switch role and act as TP ranks, so no GPU sits idle.

Sweet spot: **disaggregated serving, decode server, input ≥ 64K tokens, low batch (latency-sensitive)**. Currently gated to **MLA models on Blackwell** (DeepSeek-V3 / V3-Lite) in TRT-LLM — the partial-attention reduction relies on MLA's low-rank structure and trtllm-gen kernels.

Location: `$SERVE_REPOS/TensorRT-LLM/cpp/tensorrt_llm/kernels/helixKernels.cu`, `tensorrt_llm/_torch/modules/attention.py` (MLA path), `docs/source/features/helix.md`.

## Common combinations

| Scheme | Attention layers | FFN / MoE layers | Typical use |
|:-------|:-----------------|:-----------------|:------------|
| **TP** | TP=N | TP=N | single-node dense serving |
| **TP + SP** | TP+SP | TP+SP | TP with activation-memory relief |
| **TP + PP** | TP within node, PP across nodes | same | dense multi-node |
| **TP + EP** | TP | EP | MoE, one stage |
| **DP-attention + EP-MoE** | DP=N (each rank has full attention) | EP=N | MoE serving, single layer replicated for attention and sharded for experts — the default for DeepSeek-V3, Qwen3-MoE |
| **DP** | DP | DP | throughput scaling |

**DP-attention + EP-MoE** is the modern default for fine-grained MoE because attention is relatively cheap while MoE FFN is the expensive part — sharding only the experts minimizes collective volume.

## Collective primitives

| Primitive | Pattern | Where it shows up |
|:----------|:--------|:------------------|
| `all-reduce` | N → N (sum + broadcast) | TP row-parallel |
| `reduce-scatter` | N → N (sum-then-shard) | TP + SP |
| `all-gather` | N → N (concat-shard) | TP + SP, EP prep |
| `all-to-all` | N → N (exchange) | EP dispatch / combine, ring-attention |
| `p2p send/recv` | 1 → 1 | PP microbatch passing, ring-attention |

Bandwidth requirements: all-to-all and all-reduce roughly scale linearly with hidden dim × batch; NVLink domain size and NIC link count become the bottleneck at scale.

## Compatibility

| Scheme | vLLM | SGLang | TRT-LLM |
|:-------|:-----|:-------|:--------|
| TP | ✓ | ✓ | ✓ |
| TP + SP | ✓ (via compile) | ✓ | ✓ |
| PP | ✓ | ✓ | ✓ |
| EP | ✓ | ✓ | ✓ |
| DP-attention + EP-MoE | ✓ | ✓ | ✓ |
| CP / ring attention | partial | partial | partial |

## Engine pointers

| Engine | Distributed core | EP / MoE-specific |
|:-------|:-----------------|:------------------|
| vLLM | `vllm/distributed/{parallel_state,communication_op}.py`, `vllm/distributed/device_communicators/` | `vllm/distributed/eplb/`, `vllm/distributed/elastic_ep/`, `vllm/distributed/kv_transfer/` |
| SGLang | `python/sglang/srt/distributed/{parallel_state,communication_op}.py`, `.../device_communicators/` | `python/sglang/srt/layers/moe/ep_moe/` |
| TensorRT-LLM | `tensorrt_llm/mapping.py` (rank mapping), `cpp/tensorrt_llm/runtime/` | `tensorrt_llm/_torch/` MoE + EP modules |

## Pitfalls

- **NVLink-domain boundaries matter most.** TP=16 across two NVL8 islands is almost always worse than TP=8 + something-else across the boundary.
- **DP-attention needs separate KV caches per rank.** Radix cache / APC must be per-rank-scoped, not shared across DP replicas.
- **SP without TP is nonsense.** SP is a TP activation-memory optimization, not an independent axis.
- **EP + CUDA graph.** All-to-all collectives with dynamic-sized buckets break graph replay; either bucket into fixed sizes or keep MoE layers eager.
- **PP bubbles dominate at low concurrency.** PP helps throughput, not single-request latency. Don't confuse the two when tuning.
- **TP + LoRA.** LoRA adapters shard the same way as base weights; if the adapter wasn't saved with matching shards, naive load will OOM or mismatch.

## See also

- `algorithms/moe-routing-dispatch/` — EP is half of the MoE story
- `algorithms/disaggregated-serving/` — orthogonal to parallelism; can combine
- `hardware/nvidia/` — NVLink domain sizes (HGX-8, NVL72)
- `engines/*` — concrete implementations
