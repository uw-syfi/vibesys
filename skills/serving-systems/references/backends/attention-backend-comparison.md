# Attention backend picker

A serving system has three credible options. The right pick depends on workload shape, hardware generation, and whether you need to capture the forward in a CUDA graph.

For deep usage of a backend, open the per-backend reference:
- [`backends/flashinfer.md`](flashinfer.md) — paged batched wrappers (the default)
- [`backends/flashattention.md`](flashattention.md) — FA2 / FA3 direct kernels
- [`backends/sdpa.md`](sdpa.md) — PyTorch `F.scaled_dot_product_attention` baseline

## TL;DR — default is FlashInfer batched

| Workload | Backend | Why |
|:---|:---|:---|
| **Default — any production NVIDIA decode/prefill, batched or single-request** | **FlashInfer batched wrappers** (`BatchPrefillWithPagedKVCacheWrapper`, `BatchDecodeWithPagedKVCacheWrapper`) with `use_cuda_graph=True` | Native paged KV, graph-safe with a stable workspace, supports MLA/FP8/sliding-window/tree masks. Single-request use is a batch of size 1 — the plan cost amortizes the same way once the wrapper is reused across decode steps. |
| **Bring-up / debugging / correctness baseline** | **SDPA** (`F.scaled_dot_product_attention`, `enable_gqa=True`) | Zero extra deps; works on any PyTorch install. Use it to verify your attention math is right before introducing a kernel-library dependency. |
| **Eager long-context decode where graph capture is undesired** | **FlashAttention** (`flash_attn_with_kvcache`) | Direct kernel call, no plan step, simplest API. Pick this only when you've decided not to capture the forward in a CUDA graph at all. |
| **Non-NVIDIA** (Apple, AMD) | **SDPA** (Apple → MLX backend; AMD → ROCm or upstreamed FA AMD port) | FlashInfer is NVIDIA-only. |
| **MLA-using models** (DeepSeek V2/V3, Qwen3-MoE) | **FlashInfer** (MLA wrappers + `append_paged_mla_kv_cache`) | FA3 has MLA in newer versions but FlashInfer's path is more mature. |
| **Need fused RMSNorm / RoPE / SiLU** alongside attention | **FlashInfer** (regardless of attention pick) | Even if you use FA for attention, FlashInfer's `rmsnorm`, `apply_rope_pos_ids`, `silu_and_mul` remove launch + HBM-write overhead. The two libraries compose. |

## CUDA-graph compatibility — the section that matters most

All three have graph-safe modes, but they differ sharply in what's safe inside a captured forward.

| Backend | Inside a captured graph? | Notes |
|:---|:---|:---|
| **FlashInfer batched wrappers** | **YES** when constructed with `use_cuda_graph=True` AND a stable preallocated workspace buffer reused across replays. | This is the default. `plan()` stays *outside* capture and copies metadata into static buffers; the captured forward only does `.run()`. |
| **FlashAttention** `flash_attn_with_kvcache` | YES with **fixed effective length / bucket**. | Capturing at `cache_seqlens=N` and replaying with a different `cache_seqlens` does **not** generally work — the captured kernel can return the N-length result against a longer cache. Capture per length bucket. |
| **FlashAttention** `flash_attn_varlen_func` | **NO** in the standard sense | Varlen is genuinely dynamic. Use eager or piecewise mode (vLLM v1 pattern). |
| **SDPA** | YES with **fixed shapes**. | Mask inactive positions to keep tensor shapes stable across replays; capture per bucket if a single fixed shape is too wasteful. `enable_gqa=True` is graph-safe. |

**Rules for any captured attention forward**:

1. All input/output tensors live at **fixed device addresses** for the lifetime of the graph. Preallocate during startup; in-place `copy_` to refresh per call. Do **not** allocate inside the captured region.
2. Workspace / scratchpad buffers are also fixed-address. FlashInfer needs the workspace passed to its constructor; SDPA-on-graph won't allocate scratchpads but watch out for any helper that does.
3. No `.item()` calls, no Python branches on tensor values, no dynamic-shape slices using a Python int *inside* the captured forward. Anything dynamic must be expressed as tensor indexing against a stable-address tensor.
4. Causal masks / position-id tensors are preallocated and indexed inside the forward; do not build them per call.
5. **Validate by differential**: run the same inputs eagerly and via the captured graph at startup; compare logits per query position. Internal scratchpad aliasing typically surfaces only at the *last* query position, not the first.

The single biggest practical mistake: **trying to capture a forward that uses an attention API not on the YES list above** (e.g., `flash_attn_varlen_func` or any path that allocates internal scratchpads on the fly). Capture appears to succeed; replay diverges from eager by several logit units at the last query position; debugging burns rounds. Pick the right API up front.

## The plan/run cost — usually a non-issue

FlashInfer's batched wrappers follow a `.plan()` → `.run()` pattern:
1. Page-table processing
2. Workspace allocation
3. Schedule split-K / split-batch decisions

**Cost: ~hundreds of microseconds**, paid **once per batch composition change** (not per decode step). For steady-state decode the same plan is reused across every step until the batch composition changes, so the per-step cost is just `.run()` — which is what gets captured into the CUDA graph.

If you find yourself calling `.plan()` every step in a steady-state decode loop, you've defeated the optimization. Plan once when the batch composition changes; run repeatedly.

## Per-feature matrix

| Feature | SDPA | FlashAttention | FlashInfer (batched) |
|:---|:---|:---|:---|
| Plan step | none | none | required, kept outside graph capture |
| Paged KV (block table) | no native paged path | yes — caller owns block table | yes — wrapper consumes planned page-table metadata |
| Variable-length prefill | varlen requires manual mask | `flash_attn_varlen_func` (eager) | `BatchPrefillWithPagedKVCacheWrapper.run()` |
| GQA / MQA | `enable_gqa=True` | native | native |
| MLA | n/a | FA3 v2.7+ | yes |
| Sliding-window | manual `attn_mask` | `window_size=(left, right)` | `window_left=N` in plan |
| Tree attention (spec decoding) | manual mask | FA3 supports custom masks | `causal=False` + custom mask in plan |
| FP8 (E4M3) | partial | FA3 supports FP8 KV | yes (block-scaled) |
| Fused RMSNorm/RoPE/SiLU | no | no | **yes** |
| CUDA-graph compatible | yes (fixed shape) | yes (fixed bucket) | yes (`use_cuda_graph=True`) |

## Hardware support

| Backend | Ampere (sm_80) | Hopper (sm_90) | Blackwell (sm_100) | AMD MI300 | Apple |
|:---|:---|:---|:---|:---|:---|
| SDPA | yes | yes | yes | via ROCm PyTorch | via MLX backend |
| FlashAttention 2 | yes | yes | partial | partial via FA AMD | no |
| FlashAttention 3 | no | yes | yes | no | no |
| FlashInfer | yes | yes | yes | no | no |

## Migration shorthand

- **SDPA → FlashInfer batched**: introduce a paged KV cache; wrap allocation in a `BlockManager`-style helper; switch decode to `BatchDecodeWithPagedKVCacheWrapper.plan() → .run()`. Watch RoPE — FlashInfer expects keys already rotated.
- **FA → FlashInfer batched**: K/V layout differs (FA: `(batch, seq, kv_heads, head_dim)`; FlashInfer paged: `(num_pages, page_size, kv_heads, head_dim)`). Build a small adapter; don't share the cache tensor directly.
- **FA2 → FA3**: most call sites are source-compatible; FA3 adds FP8 KV and faster MLA. Watch `softmax_scale` defaults — they changed.

## Common pitfalls

- **Capturing the wrong API into a graph.** See the CUDA-graph section above. The cost of picking the wrong API is rounds of debugging divergent logits at the last query position.
- **`plan()` per step.** With FlashInfer batched, `plan()` is meant to be called once per batch-composition change, not per decode step. Re-planning each step wastes the entire optimization.
- **Mixing libraries on one model.** Use one backend for attention; using FA for prefill and FlashInfer for decode is possible but doubles the K/V layout maintenance and rarely pays off.
- **SDPA `is_causal=True` with explicit `attn_mask`.** Either, not both — passing both silently disables the causal short-circuit.
- **FlashInfer workspace per-step allocation** breaks CUDA-graph capture. Allocate once at construction; reuse forever.

## When to pick none of these

The serving stack should rarely need a custom attention kernel — these three plus optional Triton handle >95% of cases. The exception is novel attention variants (custom mask, new linear-attention scheme, fused decode + sampler) where none has the right primitive. For that path, see `agent-gpu-skills` — kernel-writing is out of scope here.
