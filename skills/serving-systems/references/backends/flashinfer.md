# FlashInfer for serving

FlashInfer provides paged-attention wrappers, fused non-attention ops, and batched sampling, all CUDA-graph-compatible. This skill covers calling conventions from a serving engine — not kernel internals.

## Prerequisites

- `flashinfer-python` installed (`uv pip install flashinfer-python`)
- `ninja` on PATH (FlashInfer JIT-compiles kernels on first call per shape)
- A serving engine with KV cache management

## Paged attention wrappers

Two wrappers handle the two generation phases:

| Wrapper | Query shape | Use |
|:--------|:------------|:----|
| `BatchPrefillWithPagedKVCacheWrapper` | variable-length per request | prefill + chunked prefill; causal masking |
| `BatchDecodeWithPagedKVCacheWrapper` | single token per request | steady-state decode |

Both follow **plan-then-run**:

1. `plan()` once per batch step with page-table metadata.
2. `run()` once per layer with Q and the per-layer KV cache tensor.

### Paged KV layout (NHD)

One tensor per layer, per request:

```python
kv_cache: (max_pages, 2, page_size, num_kv_heads, head_dim)
#                     ^ index 0 = K, index 1 = V
```

Three int32 tensors describe page ownership across a batch:

| Tensor | Shape | Meaning |
|:-------|:------|:--------|
| `kv_indptr` | `(batch_size + 1,)` | prefix-sum of per-request page counts |
| `kv_indices` | `(total_pages,)` | flat list of physical page IDs |
| `kv_last_page_len` | `(batch_size,)` | how full each request's last page is |

### append_paged_kv_cache

Writes freshly computed K/V into the paged pool. Pair with `flashinfer.page.get_batch_indices_positions` to compute per-token `(batch_idx, position)`:

```python
batch_indices, positions = flashinfer.page.get_batch_indices_positions(
    append_indptr,
    seq_lens,           # total length INCLUDING tokens being appended
    total_append_tokens,
)
flashinfer.page.append_paged_kv_cache(
    k, v, batch_indices, positions, kv_cache, kv_indices, kv_indptr, kv_last_page_len,
    layout="NHD",
)
```

**Gotcha:** `seq_lens` must include the tokens being appended. Passing the pre-append length yields negative positions and illegal memory access.

### Workspace

Both wrappers need a pre-allocated workspace buffer (128 MB typical, shared across wrappers since only one replays at a time):

```python
workspace = torch.zeros(128 * 1024 * 1024, dtype=torch.uint8, device=device)
prefill_wrapper = flashinfer.BatchPrefillWithPagedKVCacheWrapper(workspace, "NHD")
decode_wrapper  = flashinfer.BatchDecodeWithPagedKVCacheWrapper(workspace, "NHD")
```

### MLA variants (DeepSeek)

MLA operates on compressed KV (`c_kv` latent) plus a decoupled-RoPE head dim (`k_pe`). Wrappers are in separate submodules:

- `flashinfer.mla.BatchMLAPagedAttentionWrapper` — prefill + decode
- `flashinfer.decode.BatchDecodeMlaWithPagedKVCacheWrapper` — decode-only, query-absorption trick for the hot path
- `flashinfer.trtllm_batch_decode_with_kv_cache_mla` — trtllm-gen MLA decode, often fastest on Blackwell

Plan args for MLA are richer than the standard paged case: `ckv_indptr`, `kpe_indptr`, `kv_lora_rank`, `qk_nope_head_dim`, `qk_rope_head_dim`, `sm_scale`. Writes into the MLA paged pool go through `flashinfer.page.append_paged_mla_kv_cache` — **not** the standard `append_paged_kv_cache`, which assumes dense K/V. See [`models/text-moe/`](../models/text-moe.md).

### Cascade attention (shared-prefix workloads)

When many concurrent requests share a common prefix (system prompt, few-shot header, RAG context), `MultiLevelCascadeAttentionWrapper` splits attention into shared and unique segments and merges the partial results. Same `plan()` / `run()` shape as the base wrappers but with a level list:

- `flashinfer.MultiLevelCascadeAttentionWrapper` — general cascade
- `flashinfer.BatchPrefillWithSharedPrefixPagedKVCacheWrapper`, `flashinfer.BatchDecodeWithSharedPrefixPagedKVCacheWrapper` — shared-prefix specializations
- `flashinfer.cascade.merge_state`, `merge_state_in_place`, `merge_states` — combine partials manually

Bigger TTFT / memory wins than padded batched prefill on RAG-shaped workloads.

### BatchAttention (newer unified wrapper)

`flashinfer.BatchAttention` and `flashinfer.BatchAttentionWithAttentionSinkWrapper` add support that the classic Prefill/Decode wrappers don't:

- **`logits_soft_cap`** — required for Gemma-2, Grok
- **Attention sinks** — GPT-OSS / OLMoE family
- **`window_left`** — sliding-window attention (Mistral / Gemma-2 alternating)
- **Asymmetric `head_dim_qk` / `head_dim_vo`** — some newer models
- **`pos_encoding_mode="ALIBI"`** as an alternative to pre-applied RoPE

Default workspace for `BatchAttention` is larger (384 MB float + 8 MB int) — allocate accordingly. On new integrations, prefer `BatchAttention` over the classic wrappers unless you have a reason.

### POD attention (mixed prefill+decode batches)

`flashinfer.PODWithPagedKVCacheWrapper` and `BatchPODWithPagedKVCacheWrapper` fuse prefill and decode into a single kernel — an alternative to chunked-prefill scheduling if your engine would otherwise dispatch to two wrappers per mixed batch.

### A note on `single_*_with_kv_cache`

FlashInfer also ships `single_decode_with_kv_cache` / `single_prefill_with_kv_cache` (and `..._return_lse` variants). **Do not use these in serving paths**, especially not inside CUDA graph capture: they allocate internal scratchpads on each call, which CUDA graph capture pins to ephemeral addresses and the captured graph then reads stale memory at replay. Symptom: replay diverges from eager by several logit units at the *last* query position. Use the batched wrappers (`BatchDecodeWithPagedKVCacheWrapper`, `BatchPrefillWithPagedKVCacheWrapper`) with `use_cuda_graph=True` and stable preallocated buffers — they work correctly for batch size 1.

## Engine-drives-layers architecture

With wrapper-style backends, the **engine drives the per-layer loop** because it must call `append_paged_kv_cache` and `wrapper.run()` between QKV and O. The model class becomes a weights holder; its `forward()` isn't called in steady state.

```python
# Instead of logits, past = model(input_ids, past)
hidden = model.embed_tokens(input_ids)
for layer_idx, layer in enumerate(model.layers):
    # layernorm → QKV proj → RoPE → append KV → flashinfer.run → O proj → MLP
    ...
logits = model.lm_head(model.norm(hidden))
```

Covered in detail in [`#paged-kv-cache`](#paged-kv-cache) and [`#wrapper-usage`](#wrapper-usage).

## Fused non-attention ops

### RMSNorm

| Kernel | Semantics | When to use |
|:-------|:----------|:------------|
| `flashinfer.rmsnorm(x_2d, w, eps)` | returns `rmsnorm(x) * w` as a new tensor | input layernorm, final norm |
| `flashinfer.fused_add_rmsnorm(hs_2d, res_2d, w, eps)` | **in-place**: `res ← res + hs`, `hs ← rmsnorm(res) * w` | post-attention layernorm fusing the residual add |

Both require 2D `(tokens, hidden)`. For 3D (prefill) tensors create a `.view(-1, H)` — in-place mutations propagate through shared storage.

### RoPE

`flashinfer` computes cos/sin on the fly inside the kernel — no pre-computation:

- `flashinfer.apply_rope_pos_ids(q, k, pos_ids, rope_theta)` — default RoPE
- `flashinfer.apply_llama31_rope_pos_ids(q, k, pos_ids, rope_theta, rope_scale, low_freq_factor, high_freq_factor, old_context_len)` — Llama-3 scaled

Inputs must be NHD: `(nnz, num_heads, head_dim)`. `pos_ids` is `(nnz,)` int32. Select variant from `config.rope_parameters["rope_type"]`.

### SiLU + multiply (SwiGLU MLP)

```python
# gate_proj(x) and up_proj(x) concatenated along the feature dim
gate_up = torch.cat([gate_proj(x), up_proj(x)], dim=-1)  # (..., 2 * I)
return down_proj(flashinfer.silu_and_mul(gate_up))       # (..., I)
```

Single kernel replaces `silu(gate) * up` (two kernels + an intermediate tensor).

### Batched sampling — full menu

The `flashinfer.sampling` submodule covers the whole typical pipeline:

| Kernel | Purpose |
|:-------|:--------|
| `sampling_from_probs`, `sampling_from_logits` | unrestricted multinomial / greedy |
| `top_p_sampling_from_probs` | top-p (rejection-sampling under the hood) |
| `top_k_sampling_from_probs` | top-k |
| `min_p_sampling_from_probs` | min-p |
| `top_k_top_p_sampling_from_probs`, `top_k_top_p_sampling_from_logits` | joint top-k + top-p |
| `top_p_renorm_probs`, `top_k_renorm_probs`, `top_k_mask_logits` | filter-without-sampling helpers |
| `chain_speculative_sampling` | rejection-resample for speculative verify |

### `flashinfer.logits_processor` — fused sampler pipelines

For anything beyond a single filter, use the pipeline builder instead of hand-chaining kernels:

```python
from flashinfer.logits_processor import LogitsPipe, Temperature, Softmax, TopP, Sample

pipe = LogitsPipe([Temperature, Softmax, TopP, Sample])
token = pipe(logits, temperature=temperatures, top_p=top_ps)
```

Components available: `Temperature`, `Softmax`, `TopK`, `TopP`, `MinP`, `Sample`. The pipeline compiles to fused kernels automatically — this is the upstream-recommended shape for serving samplers. Replaces the hand-rolled `softmax + top_p_sampling_from_probs` pattern in older code.

Handle `temperature == 0` (greedy) with an argmax + mask path upstream of the pipe; the kernels themselves want strictly positive temperatures.

## CUDA-graph compatibility

The **batched** wrappers are graph-safe. The single-request wrappers (`single_*_with_kv_cache`) are **not** — see the warning above.

For the batched wrappers:

1. **Dedicated wrapper per captured batch size.** `BatchDecodeWithPagedKVCacheWrapper(workspace, "NHD", use_cuda_graph=True, paged_kv_indptr_buffer=..., paged_kv_indices_buffer=..., paged_kv_last_page_len_buffer=...)` — pass persistent static buffers.
2. **`plan()` outside the graph.** The wrapper's `plan()` does `.copy_()` into the static buffers; that's the mechanism. Replay is just `run()`.
3. **Workspace sharing.** One shared workspace is fine across per-bs wrappers — only one replays at a time.
4. **Use the same wrapper for batch=1.** Single-request decode goes through the batched wrapper with `batch_size=1`. The plan cost is paid once per batch-composition change (not per step) and the per-step `.run()` is what gets captured into the graph.
5. **Differential validate.** Run the same input eagerly and via the captured graph at startup; compare logits per query position. Internal-scratchpad aliasing surfaces only at the last query position.

See [`backends/cuda-graph/`](cuda-graph.md) for the full lifecycle.

## Tensor shapes by phase

| Phase | Q | K/V (for append) |
|:------|:--|:-----------------|
| Prefill | `(total_tokens, num_qo_heads, head_dim)` | `(total_tokens, num_kv_heads, head_dim)` |
| Decode | `(batch_size, num_qo_heads, head_dim)` | `(batch_size, num_kv_heads, head_dim)` |

Common reshape from standard `(batch, heads, seq, head_dim)`:

```python
q_prefill = q.permute(0, 2, 1, 3).reshape(prompt_len, num_qo_heads, head_dim)
q_decode  = q.squeeze(2)
```

## JIT, AOT, and avoiding first-call stalls

FlashInfer JIT-compiles kernels on first call per shape. That's a multi-second stall at cold start — the #1 user-facing FlashInfer serving issue. Mitigations, in order of effort:

| Approach | How |
|:---------|:----|
| **Warmup sweep** | call every (batch, seq, head) combo you'll see before opening the listen socket |
| **Pre-built cubin wheel** | `pip install flashinfer-cubin` — ships with prebuilt binaries |
| **JIT cache wheel** | `pip install flashinfer-jit-cache --index-url https://flashinfer.ai/whl/cu129` — drop-in cache; `cu128` / `cu130` variants available |
| **AOT generation** | `flashinfer.aot` + the `flashinfer` CLI (`show-config`, `list-modules`, `module-status`, `download-cubin`, `clear-cache`, `export-compile-commands`) for bespoke builds |

Tuning env vars:

- `FLASHINFER_CUDA_ARCH_LIST="9.0;10.0"` — restrict to the arches you deploy on (trim compile time)
- `FLASHINFER_LOGLEVEL`, `FLASHINFER_LOGDEST` — debug
- `FLASHINFER_AUTOTUNER_LOAD_FROM_FILE` — load persisted autotune configs

### Autotune context manager

Some kernels (GEMM, MoE, FP4) benefit from autotune but you don't want autotune to run inside CUDA graph capture. Gate it:

```python
with flashinfer.autotune():
    warmup_pass(dummy_inputs)   # autotune runs
    # persist with flashinfer.autotune().save() or equivalent
# Autotune disabled for subsequent captured regions
```

## GEMM, MoE, and FP4 (quantized-linear and MoE paths)

When running a quantized-linear or MoE serving path, FlashInfer's `gemm` and `fused_moe` modules often beat cuBLAS / hand-rolled Triton:

| Module | Highlights |
|:-------|:-----------|
| `flashinfer.gemm` | `mm_bf16`, `bmm_bf16`, `mm_fp8`, `bmm_fp8`, `mm_fp4`, `mm_mxfp8`, `bmm_mxfp8`, `gemm_fp8_nt_groupwise`, `group_gemm_fp8_nt_groupwise`, `group_deepgemm_fp8_nt_groupwise`, `batch_deepgemm_fp8_nt_groupwise`, `group_gemm_mxfp4_nt_groupwise`, `tgv_gemm_sm100`, `SegmentGEMMWrapper` (LoRA) |
| `flashinfer.fused_moe` | `cutlass_fused_moe`, `trtllm_fp4_block_scale_moe`, `trtllm_fp8_block_scale_moe`, `trtllm_fp8_per_tensor_scale_moe`, `trtllm_bf16_moe`, `CuteDslMoEWrapper` |
| `flashinfer.fp4_quantization` | `fp4_quantize`, `nvfp4_quantize`, `mxfp4_quantize`, `nvfp4_kv_quantize` / `nvfp4_kv_dequantize` (FP4 KV cache — Blackwell native) |
| `flashinfer.fp8_quantization` | `mxfp8_quantize`, `mxfp8_dequantize_host` |
| `flashinfer.topk` | `top_k`, `top_k_page_table_transform`, `top_k_ragged_transform` for MoE routing / spec draft |

FP4 GEMM, NVFP4 MoE, and FP4 KV cache are **Blackwell-only** (SM 10.0+). `nvfp4_kv_dequantize` runs on Ampere+ for inference-only flows.

## Communication (TP / EP)

`flashinfer.comm` provides NCCL-adjacent primitives tuned for serving:

- `trtllm_allreduce_fusion`, `trtllm_custom_all_reduce` — TRT-LLM-style fused all-reduce
- `MnnvlMemory`, `trtllm_mnnvl_all_reduce`, `trtllm_mnnvl_fused_allreduce_rmsnorm` — NVLink-5 / NVL72 multi-node memory
- `moe_a2a_dispatch`, `moe_a2a_combine` — all-to-all dispatch/combine for EP

Drop-in replacements for NCCL calls in TP forward + fused norm + allreduce, with measurable latency wins on Hopper / Blackwell.

## Additional norm / activation variants

Beyond the ops listed earlier, you'll want these on certain models:

- `gemma_rmsnorm`, `gemma_fused_add_rmsnorm` (Gemma-2 / Gemma-3)
- `layernorm` (pre-Llama families)
- `rmsnorm_quant`, `fused_add_rmsnorm_quant`, `rmsnorm_fp4quant`, `add_rmsnorm_fp4quant` (fused norm + quant)
- `fused_rmsnorm_silu` (SwiGLU fusion)
- `gelu_and_mul`, `gelu_tanh_and_mul` (GELU MLPs in Phi-3 / older models)
- `silu_and_mul_scaled_nvfp4_experts_quantize` (NVFP4 MoE epilogue)
- `apply_rope_with_cos_sin_cache` / `_inplace` (precomputed cos/sin table path)

## Pitfalls

- **`seq_lens` semantics** (see above): include the appending tokens.
- **Workspace-buffer reuse across wrappers**: OK for exclusive use; unsafe under concurrent streams.
- **RoPE on MLA**: use `pos_encoding_mode="NONE"` and apply RoPE manually before attention on the auxiliary head dim.
- **Mixing in-place and out-of-place RMSNorm forms**: `rmsnorm` returns a new tensor; `fused_add_rmsnorm` mutates. Using the wrong one breaks the residual chain.
- **Version drift**: flashinfer APIs evolve. Pin the version and re-check signatures at upgrade — see [`#kernel-api`](#kernel-api).

## Out of scope — kernel implementation

Writing FlashInfer-style kernels from scratch: see `agent-gpu-skills` (`cuda-skill`, `cutlass-skill`, `triton-skill`).

## Additional references




## See also

- [`algorithms/attention-variants/`](../algorithms/attention-variants.md) — MHA / GQA / MLA / SWA wrappers in context
- [`algorithms/paged-attention/`](../algorithms/paged-attention.md)
- [`algorithms/batched-sampling/`](../algorithms/batched-sampling.md)
- [`backends/cuda-graph/`](cuda-graph.md)
- [`models/text-moe/`](../models/text-moe.md) — MLA variants (DeepSeek family)


---

## Kernel Api

## flashinfer.rmsnorm

```python
flashinfer.rmsnorm(
    input: torch.Tensor,       # (batch_size, hidden_size) — must be 2D
    weight: torch.Tensor,      # (hidden_size,)
    eps: float = 1e-06,
    out: Optional[torch.Tensor] = None,
) -> torch.Tensor              # same shape as input
```

Returns a **new** tensor. Not in-place.

## flashinfer.fused_add_rmsnorm

```python
flashinfer.fused_add_rmsnorm(
    input: torch.Tensor,       # (batch_size, hidden_size) — must be 2D
    residual: torch.Tensor,    # (batch_size, hidden_size) — must be 2D
    weight: torch.Tensor,      # (hidden_size,)
    eps: float = 1e-06,
) -> None                      # MODIFIES BOTH IN-PLACE
```

Two-step in-place operation:
1. `residual[i] += input[i]`
2. `input[i] = (residual[i] / RMS(residual)) * weight[i]`

After call: `input` = normalized result, `residual` = updated sum.

## flashinfer.silu_and_mul

```python
flashinfer.silu_and_mul(
    input: torch.Tensor,       # (..., 2 * hidden_size)
    out: Optional[torch.Tensor] = None,
) -> torch.Tensor              # (..., hidden_size)
```

Computes `silu(input[..., :H]) * input[..., H:]`. Works with any leading dimensions.

## flashinfer.apply_rope_pos_ids

```python
flashinfer.apply_rope_pos_ids(
    q: torch.Tensor,           # (nnz, num_q_heads, head_dim)
    k: torch.Tensor,           # (nnz, num_k_heads, head_dim)
    pos_ids: torch.Tensor,     # (nnz,) integer
    rotary_dim: Optional[int] = None,
    interleave: bool = False,
    rope_scale: float = 1,
    rope_theta: float = 10000.0,
) -> Tuple[torch.Tensor, torch.Tensor]  # (q_rope, k_rope)
```

Standard RoPE. Computes cos/sin on-the-fly from `pos_ids` and `rope_theta`.

## flashinfer.apply_llama31_rope_pos_ids

```python
flashinfer.apply_llama31_rope_pos_ids(
    q: torch.Tensor,           # (nnz, num_q_heads, head_dim)
    k: torch.Tensor,           # (nnz, num_k_heads, head_dim)
    pos_ids: torch.Tensor,     # (nnz,) integer
    rotary_dim: Optional[int] = None,
    interleave: bool = False,
    rope_scale: float = 8,
    rope_theta: float = 500000.0,
    low_freq_factor: float = 1,
    high_freq_factor: float = 4,
    old_context_len: int = 8192,
) -> Tuple[torch.Tensor, torch.Tensor]  # (q_rope, k_rope)
```

Llama 3.1 RoPE with frequency-dependent scaling. `rope_scale` should be set to `config.rope_parameters["factor"]`.

Also available: `apply_llama31_rope_pos_ids_inplace` (same signature, modifies q/k in-place, returns None).

## flashinfer.softmax

```python
flashinfer.softmax(
    logits: torch.Tensor,      # (batch_size, vocab_size) — float32
    temperature: Union[torch.Tensor, float, None] = None,  # scalar or (batch_size,)
) -> torch.Tensor              # (batch_size, vocab_size) — probabilities
```

Online safe softmax with per-request temperature scaling. Pass `logits.float()` to ensure float32.

## flashinfer.top_p_sampling_from_probs

```python
flashinfer.top_p_sampling_from_probs(
    probs: torch.Tensor,       # (batch_size, num_classes) — float32 probabilities
    top_p: Union[torch.Tensor, float],  # scalar or (batch_size,)
    deterministic: bool = True,
    check_nan: bool = False,
) -> torch.Tensor              # (batch_size,) int32 — sampled indices
```

Takes **probabilities** (not logits). May return a tuple `(samples, success)` in some versions — handle with:
```python
result = flashinfer.top_p_sampling_from_probs(probs, top_ps)
sampled = result[0] if isinstance(result, tuple) else result
```

## Dtype Requirements

| Kernel | Input dtype | Output dtype |
|---|---|---|
| rmsnorm | float16/bfloat16 | same as input |
| fused_add_rmsnorm | float16/bfloat16 | same as input (in-place) |
| silu_and_mul | float16/bfloat16 | same as input |
| apply_rope_pos_ids | float16/bfloat16 (q/k), int32/int64 (pos_ids) | same as q/k |
| softmax | float32 (logits) | float32 |
| top_p_sampling_from_probs | float32 (probs) | int32 |


---

## Paged Kv Cache

## Overview

A paged KV cache replaces per-request dense KV tensors with a shared pool of fixed-size pages. This eliminates the memory waste from padding variable-length sequences and enables dynamic memory allocation.

## Cache Tensor Layout

One tensor per transformer layer, using NHD layout:

```python
# Per-layer shape: (max_pages, 2, page_size, num_kv_heads, head_dim)
#                   ^          ^  ^          ^              ^
#                   pages      K/V tokens    heads          features
kv_caches = [
    torch.zeros(max_pages, 2, page_size, num_kv_heads, head_dim,
                dtype=dtype, device=device)
    for _ in range(num_layers)
]
```

- Dimension 1 (`2`): index 0 = keys, index 1 = values
- `page_size`: typical values are 1, 16, or 64. Smaller pages = less wasted space in the last page; larger pages = less page management overhead
- All layers share the same page indices — if request A owns pages `[3, 7]`, those page slots are used in every layer's tensor

## Page Table State

Per-request tracking with three dictionaries:

```python
request_pages: dict[str, list[int]]       # request_id → ordered list of page indices
request_last_page_len: dict[str, int]     # request_id → how many tokens in the last page
request_seq_len: dict[str, int]           # request_id → total sequence length
```

A free pool (list of available page indices) is maintained separately.

## Operations

### `init_request(request_id, seq_len)`

Called during prefill. Allocates enough pages for the initial sequence:

```python
num_pages = ceil(seq_len / page_size)
last_page_len = seq_len - page_size * (num_pages - 1)
pages = allocate_pages(num_pages)
```

Example: `seq_len=20`, `page_size=16` → 2 pages, `last_page_len=4`

### `append_token(request_id)`

Called during decode (once per request per step). Reserves space for one more token:

```python
seq_len += 1
if last_page_len == page_size:
    # Last page is full — allocate a new page
    new_page = allocate_pages(1)
    pages.append(new_page)
    last_page_len = 1
else:
    last_page_len += 1
```

### `free_request(request_id)`

Returns all pages to the free pool when a request finishes.

### `build_batch_arrays(request_ids, device)`

Constructs the three tensors FlashInfer needs to navigate the page table:

```python
kv_indptr = [0]       # cumulative page count boundaries
kv_indices = []       # flat list of page indices for all requests
kv_last_page_len = [] # fill level of each request's last page

for rid in request_ids:
    pages = request_pages[rid]
    kv_indptr.append(kv_indptr[-1] + len(pages))
    kv_indices.extend(pages)
    kv_last_page_len.append(request_last_page_len[rid])
```

All three tensors must be `dtype=torch.int32`.

## Example

Three active requests with `page_size=16`:

| Request | seq_len | Pages | last_page_len |
|---------|---------|-------|---------------|
| A       | 20      | [0, 1] | 4            |
| B       | 10      | [2]    | 10           |
| C       | 35      | [3, 4, 5] | 3         |

Batch arrays:
```
kv_indptr        = [0, 2, 3, 6]
kv_indices       = [0, 1, 2, 3, 4, 5]
kv_last_page_len = [4, 10, 3]
```

## Memory Formula

Total KV cache memory:

```
max_pages × num_layers × 2(K+V) × page_size × num_kv_heads × head_dim × dtype_bytes
```

Example: 2048 pages × 16 layers × 2 × 16 tokens × 8 heads × 64 dim × 2 bytes (fp16) = ~1 GB

## Ordering Constraint

The sequence of operations per decode step must be:

1. Read `seq_lens_before` (= RoPE position of new token)
2. Call `append_token()` for each request (updates page table)
3. Call `build_batch_arrays()` (reads updated page table)
4. Call `get_batch_indices_positions()` with `seq_lens_after`
5. Call wrapper `plan()` and then layer loop with `append_paged_kv_cache` + `run()`


---

## Wrapper Usage

## Setup

```python
import flashinfer
import flashinfer.page

# Allocate workspace buffer (shared between prefill and decode wrappers)
workspace = torch.zeros(128 * 1024 * 1024, dtype=torch.uint8, device=device)

# Create wrappers — "NHD" = (tokens, heads, head_dim) layout
prefill_wrapper = flashinfer.BatchPrefillWithPagedKVCacheWrapper(workspace, "NHD")
decode_wrapper = flashinfer.BatchDecodeWithPagedKVCacheWrapper(workspace, "NHD")
```

## `get_batch_indices_positions`

Computes per-token batch indices and within-sequence positions for `append_paged_kv_cache`. This tells the append kernel which request each token belongs to and where in the page table to write it.

```python
batch_indices, positions = flashinfer.page.get_batch_indices_positions(
    append_indptr,   # int32: cumulative token counts [0, n_tokens_req0, n_tokens_req0+req1, ...]
    seq_lens,        # int32: total seq len per request INCLUDING tokens being appended
    nnz,             # int: total number of tokens across all requests
)
```

**Critical**: `seq_lens` must include the tokens being appended. If a request has 0 tokens in cache and is appending 5 tokens, pass `seq_lens=[5]`, not `[0]`. Passing pre-append lengths produces negative positions and CUDA illegal memory access.

### Prefill example (single request, prompt_len=20)

```python
qo_indptr = torch.tensor([0, 20], dtype=torch.int32, device=device)
seq_lens = torch.tensor([20], dtype=torch.int32, device=device)
batch_indices, positions = flashinfer.page.get_batch_indices_positions(qo_indptr, seq_lens, 20)
# batch_indices = [0, 0, 0, ..., 0]  (20 zeros)
# positions = [0, 1, 2, ..., 19]
```

### Decode example (3 requests, seq_lens 21, 11, 36 after append)

```python
append_indptr = torch.tensor([0, 1, 2, 3], dtype=torch.int32, device=device)  # 1 token each
seq_lens = torch.tensor([21, 11, 36], dtype=torch.int32, device=device)
batch_indices, positions = flashinfer.page.get_batch_indices_positions(append_indptr, seq_lens, 3)
# batch_indices = [0, 1, 2]
# positions = [20, 10, 35]   (= seq_len - 1 for each)
```

## `append_paged_kv_cache`

Writes K and V tensors into the correct page and offset in the paged cache:

```python
flashinfer.page.append_paged_kv_cache(
    key,                # (nnz, num_kv_heads, head_dim)
    value,              # (nnz, num_kv_heads, head_dim)
    batch_indices,      # from get_batch_indices_positions
    positions,          # from get_batch_indices_positions
    paged_kv_cache,     # per-layer tensor: (max_pages, 2, page_size, num_kv_heads, head_dim)
    kv_indices,         # from build_batch_arrays
    kv_indptr,          # from build_batch_arrays
    kv_last_page_len,   # from build_batch_arrays
    "NHD",
)
```

This is called **once per layer** in the layer loop, writing that layer's K/V into the layer's cache tensor.

## Prefill Wrapper

### `plan()` — called once per batch step (before the layer loop)

```python
prefill_wrapper.plan(
    qo_indptr=qo_indptr,                     # int32: [0, prompt_len] for single request
    paged_kv_indptr=kv_indptr,                # from build_batch_arrays
    paged_kv_indices=kv_indices,              # from build_batch_arrays
    paged_kv_last_page_len=kv_last_page_len,  # from build_batch_arrays
    num_qo_heads=num_qo_heads,                # e.g. 32
    num_kv_heads=num_kv_heads,                # e.g. 8 (GQA)
    head_dim_qk=head_dim,                     # e.g. 64
    page_size=page_size,                      # e.g. 16
    causal=True,                              # standard causal attention
    q_data_type=dtype,                        # e.g. torch.float16
)
```

### `run()` — called once per layer

```python
# q shape: (total_tokens, num_qo_heads, head_dim)
attn_output = prefill_wrapper.run(q, kv_cache_for_this_layer)
# attn_output shape: (total_tokens, num_qo_heads, head_dim)
```

The plan metadata (page table, causal flag, etc.) persists across `run()` calls — no need to re-plan between layers.

## Decode Wrapper

### `plan()` — called once per batch step

```python
decode_wrapper.plan(
    indptr=kv_indptr,              # from build_batch_arrays
    indices=kv_indices,            # from build_batch_arrays
    last_page_len=kv_last_page_len,# from build_batch_arrays
    num_qo_heads=num_qo_heads,
    num_kv_heads=num_kv_heads,
    head_dim=head_dim,
    page_size=page_size,
    q_data_type=dtype,
)
```

Note: parameter names differ slightly from prefill (`indptr` vs `paged_kv_indptr`, `head_dim` vs `head_dim_qk`).

### `run()` — called once per layer

```python
# q shape: (batch_size, num_qo_heads, head_dim)
attn_output = decode_wrapper.run(q, kv_cache_for_this_layer)
# attn_output shape: (batch_size, num_qo_heads, head_dim)
```

## Full Per-Layer Pattern

### Prefill (inside layer loop)

```python
for layer_idx, layer in enumerate(model.model.layers):
    residual = hidden_states
    hidden_states = layer.input_layernorm(hidden_states)

    attn = layer.self_attn
    q = attn.q_proj(hidden_states).view(1, prompt_len, num_qo_heads, head_dim).transpose(1, 2)
    k = attn.k_proj(hidden_states).view(1, prompt_len, num_kv_heads, head_dim).transpose(1, 2)
    v = attn.v_proj(hidden_states).view(1, prompt_len, num_kv_heads, head_dim)

    q, k = apply_rotary_pos_emb(q, k, cos, sin)

    # Reshape to NHD: (prompt_len, heads, head_dim)
    q_nhd = q.permute(0, 2, 1, 3).reshape(prompt_len, num_qo_heads, head_dim)
    k_nhd = k.permute(0, 2, 1, 3).reshape(prompt_len, num_kv_heads, head_dim)
    v_nhd = v.reshape(prompt_len, num_kv_heads, head_dim)

    flashinfer.page.append_paged_kv_cache(
        k_nhd, v_nhd, batch_indices, positions,
        kv_caches[layer_idx], kv_indices, kv_indptr, kv_last_page_len, "NHD",
    )

    attn_output = prefill_wrapper.run(q_nhd, kv_caches[layer_idx])
    attn_output = attn_output.view(1, prompt_len, hidden_size)
    hidden_states = attn.o_proj(attn_output)

    hidden_states = residual + hidden_states
    residual = hidden_states
    hidden_states = layer.post_attention_layernorm(hidden_states)
    hidden_states = layer.mlp(hidden_states)
    hidden_states = residual + hidden_states
```

### Decode (inside layer loop)

```python
for layer_idx, layer in enumerate(model.model.layers):
    residual = hidden_states
    hidden_states = layer.input_layernorm(hidden_states)

    attn = layer.self_attn
    q = attn.q_proj(hidden_states).view(batch_size, 1, num_qo_heads, head_dim).transpose(1, 2)
    k = attn.k_proj(hidden_states).view(batch_size, 1, num_kv_heads, head_dim).transpose(1, 2)
    v = attn.v_proj(hidden_states).view(batch_size, 1, num_kv_heads, head_dim)

    q, k = apply_rotary_pos_emb(q, k, cos, sin)

    # Squeeze out seq_len=1 dim
    q_fi = q.squeeze(2)       # (batch, num_qo_heads, head_dim)
    k_nhd = k.squeeze(2)      # (batch, num_kv_heads, head_dim)
    v_nhd = v.squeeze(1)      # (batch, num_kv_heads, head_dim)

    flashinfer.page.append_paged_kv_cache(
        k_nhd, v_nhd, batch_indices, positions,
        kv_caches[layer_idx], kv_indices, kv_indptr, kv_last_page_len, "NHD",
    )

    attn_output = decode_wrapper.run(q_fi, kv_caches[layer_idx])
    attn_output = attn_output.view(batch_size, 1, hidden_size)
    hidden_states = attn.o_proj(attn_output)

    hidden_states = residual + hidden_states
    residual = hidden_states
    hidden_states = layer.post_attention_layernorm(hidden_states)
    hidden_states = layer.mlp(hidden_states)
    hidden_states = residual + hidden_states
```

## Common Pitfalls

1. **Negative positions from `get_batch_indices_positions`**: Pass total seq_len (after append), not seq_len before append
2. **`ninja` not found**: FlashInfer JIT-compiles CUDA kernels on first use. Ensure `ninja` binary is on PATH (install via `pip install ninja` in the venv)
3. **Wrong int dtype**: All index tensors (`kv_indptr`, `kv_indices`, `kv_last_page_len`, `qo_indptr`, `append_indptr`, `seq_lens`) must be `torch.int32`
4. **Prefill vs decode parameter names**: Prefill uses `paged_kv_indptr`, `head_dim_qk`; decode uses `indptr`, `head_dim`
5. **RoPE**: Use `pos_encoding_mode="NONE"` (default) and apply RoPE manually for llama3-style frequency-scaled RoPE
