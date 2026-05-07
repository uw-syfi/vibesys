# FlashAttention for LLM serving

Use FlashAttention as the **attention kernel backend** inside a serving engine — not as a full serving runtime. FlashAttention gives you fast attention kernels (including an inference API that can update KV cache in place and read from a paged cache). Your engine still owns request scheduling, page allocation, block tables, cache growth, and request cleanup.

This is the main difference from FlashInfer, which exposes serving-oriented wrappers (plan/run, workspace, batch planning). FlashAttention exposes lower-level kernels and leaves the serving layer to you.

## Prerequisites

- A working LLM inference server with KV cache support
- `flash-attn` installed (`pip install flash-attn --no-build-isolation`)
- `ninja` on PATH
- Supported hardware / dtype for the FA version you target

## Which FA? (selection rubric)

| Version | Package / import | Hardware | Dtypes | Status |
|:--------|:-----------------|:---------|:-------|:-------|
| **FA2** | `pip install flash-attn`; `from flash_attn import ...` | Ampere / Ada / Hopper (sm_80+) | fp16, bf16 | stable |
| **FA3** | `cd hopper && python setup.py install`; `import flash_attn_interface; flash_attn_interface.flash_attn_func(...)` | Hopper (H100/H200, sm_90) | fp16, bf16 (fwd+bwd); **fp8** (fwd only) | **beta** |
| **FA4** | `pip install flash-attn-4` (or `"flash-attn-4[cu13]"`); `from flash_attn.cute import flash_attn_func` | Hopper + **Blackwell** | bf16, fp8, fp4 paths | new |

Build requirements: FA2 needs CUDA 12.0+ and PyTorch 2.2+ plus `ninja`, `packaging`, `psutil`. FA3 needs CUDA 12.3+ (12.8 recommended). FA3 import path `flash_attn_interface` is **a distinct top-level name** from `flash_attn`; both can coexist via namespace packaging. The `hopper/` subdirectory in the dao-ailab repo is the FA3 source.

For most serving integrations today target FA2 (`flash_attn`). Promote to FA3 on Hopper when FP8 wins justify the beta status; use FA4 on Blackwell.

## Architectural model

The engine still drives batching and cache management. With FlashAttention in serving, the serving layer must explicitly handle:

- request admission and removal
- per-request sequence length
- per-request KV placement
- `block_table` construction for paged KV
- `cache_seqlens` construction per active decode batch
- prefill vs decode dispatch

## Core APIs (FA2)

| API | Purpose |
|:----|:--------|
| `flash_attn_func(q, k, v, ...)` | dense attention kernel for equal-length batched tensors |
| `flash_attn_varlen_func(q, k, v, cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k, ...)` | variable-length attention for packed tokens; **also supports paged KV via `block_table` + `seqused_k`** |
| `flash_attn_with_kvcache(q, k_cache, v_cache, k=None, v=None, cache_seqlens=..., cache_batch_idx=..., block_table=..., num_splits=..., ...)` | inference-only path; updates cache in place; optional paged KV; supports split-KV for long-context decode |
| `flash_attn_qkvpacked_func`, `flash_attn_varlen_qkvpacked_func` | fused-QKV variants when the engine keeps Q/K/V in one packed tensor |
| `flash_attn_kvpacked_func`, `flash_attn_varlen_kvpacked_func` | fused-KV variants when K and V share storage |
| `flash_attn_unpadded_func` | FA1 name, aliased for back-compat |

`flash_attn_with_kvcache` is the inference path for iterative decoding. It updates `k_cache` / `v_cache` in place in the same call that computes attention, and does **not** support backward pass.

### Common kwargs across entry points

| Kwarg | Purpose | Default |
|:------|:--------|:--------|
| `softmax_scale` | override `1/sqrt(head_dim)` | auto |
| `causal` | causal mask | `False` |
| `window_size=(left, right)` | **sliding-window attention** (Mistral, Gemma-2 SWA layers); `(-1,-1)` disables | `(-1,-1)` |
| `softcap` | tanh softcap `tanh(x/c)*c` (**Gemma-2, Grok** — missing this = silent accuracy loss) | `0.0` |
| `alibi_slopes` | `(nheads,)` or `(batch, nheads)` fp32 for ALiBi bias | `None` |
| `deterministic` | reproducible output at perf cost | `False` |
| `return_attn_probs` / `return_softmax_lse` | return `(out, softmax_lse)` — useful for speculative decoding and cross-layer aggregation | `False` |

### FA2 `flash_attn_with_kvcache` kvcache-specific kwargs

| Kwarg | Purpose |
|:------|:--------|
| `cache_seqlens` | `(batch,)` int32 current logical lengths |
| `cache_batch_idx` | `(batch,)` int32 indirection when active-batch order ≠ cache row order |
| `cache_leftpad` | `(batch,)` int32 index where real KV starts; enables left-padded dense caches |
| `block_table` | `(batch, max_blocks_per_seq)` int32 — enables **paged KV** |
| `rotary_cos` / `rotary_sin` / `rotary_interleaved` | built-in RoPE applied to `k` and advancing-q before attention |
| `num_splits` | **split-KV for long-context decode**: `0` = heuristic auto, `>1` = forced split count |

### FA3 kwargs — what's different

The FA3 function signature (in `flash_attn_interface.py` under `hopper/`) diverges from FA2:

- **FP8 scales** — `q_descale`, `k_descale`, `v_descale` (tensors of per-tensor or per-block scales)
- **Cache-append split** — FA3 exposes `k_new`, `v_new`, `cu_seqlens_k_new` as a distinct cache-append path (vs. FA2's `k`/`v` kwargs on `flash_attn_with_kvcache`)
- `pack_gqa` — pack GQA heads for better kernel utilization
- `attention_chunk` — chunked attention mode
- `scheduler_metadata` — pre-computed scheduling hints
- `seqused_q` / `seqused_k` — actual used seqlens (decouples from `cu_seqlens` diffs)
- `sm_margin` — SM occupancy tuning

### FA4 entry

```python
from flash_attn.cute import flash_attn_func
```

CuTeDSL-based, Hopper + Blackwell. Different file tree from FA2/FA3.

## Prefill patterns

### Uniform prompt lengths

```python
from flash_attn import flash_attn_func

out = flash_attn_func(q, k, v, dropout_p=0.0, causal=True)
```

### Variable prompt lengths — pack and use `cu_seqlens`

```python
from flash_attn import flash_attn_varlen_func

out = flash_attn_varlen_func(
    q_packed, k_packed, v_packed,
    cu_seqlens_q, cu_seqlens_k,
    max_seqlen_q, max_seqlen_k,
    dropout_p=0.0, causal=True,
)
```

`cu_seqlens` is an int32 prefix-sum of shape `(batch+1,)`. `max_seqlen_*` is for launch tuning, not correctness — overestimating costs a little perf.

### Paged varlen prefill — `block_table` on `flash_attn_varlen_func`

Paged KV isn't only for decode. `flash_attn_varlen_func` accepts `block_table` for prefill / chunked prefill against an already-paged cache. This is the primary vLLM v1 prefill path:

```python
out = flash_attn_varlen_func(
    q_packed,                       # (nnz_q, num_q_heads, head_dim)
    k_cache, v_cache,               # (num_blocks, page_block_size, num_kv_heads, head_dim)
    cu_seqlens_q,                   # (batch+1,) int32
    max_seqlen_q, max_seqlen_k,
    block_table=block_table,        # (batch, max_blocks_per_seq) int32
    seqused_k=seqused_k,            # (batch,) int32 — actual K used; decouples from cu_seqlens_k diff
    leftpad_k=leftpad_k,            # (batch,) int32 — left padding to skip in paged K
    causal=True,
)
```

Relevant kwargs: `block_table`, `seqused_k`, `leftpad_k`, `max_seqlen_k_hint`, `min_seqlen_k`.

### Chunked prefill into cache

If the runtime wants to write prompt K/V directly into the cache while computing attention, use `flash_attn_with_kvcache` with multi-token `k` and `v`. The contract is the same as decode: the engine owns cache capacity and append positions.

## Decode call pattern

Per decode step:

1. Compute Q, K, V for the new token(s).
2. Ensure the target request has enough cache capacity (allocate pages if needed).
3. Build `cache_seqlens` for the active batch.
4. Build `block_table` if using paged KV.
5. Call `flash_attn_with_kvcache`.
6. Increment each request's logical sequence length after the append.

```python
from flash_attn import flash_attn_with_kvcache

out = flash_attn_with_kvcache(
    q=q_step,                          # (batch, seqlen_q, nheads_q, head_dim)
    k_cache=k_cache,
    v_cache=v_cache,
    k=k_step,                          # current-step K to append
    v=v_step,                          # current-step V to append
    cache_seqlens=cache_seqlens,       # int32
    cache_batch_idx=cache_batch_idx,   # optional int32
    block_table=block_table,           # optional int32; enables paged KV
    causal=True,
    softmax_scale=None,
)
```

When `k` and `v` are provided, the cache is updated in place starting at the positions given by `cache_seqlens`, and attention is computed with the updated cache in one call.

## Paged KV cache contract

FlashAttention supports paged KV, but only as a **kernel contract** — the serving layer owns the page pool and bookkeeping.

### Layouts

Without paged KV:
- `k_cache`, `v_cache`: `(batch_size_cache, seqlen_cache, nheads_k, headdim)`

With paged KV via `block_table`:
- `k_cache`, `v_cache`: `(num_blocks, page_block_size, nheads_k, headdim)`
- `block_table`: `(batch_size, max_num_blocks_per_seq)`, `torch.int32`
- `cache_seqlens`: scalar int or `(batch_size,)`, `torch.int32`

**`page_block_size` must be a multiple of 256.**

### What the engine must provide

- A global pool of physical KV pages.
- A free-page allocator.
- Per-request logical-to-physical page mapping.
- A dense `block_table` for the active batch.
- Per-request sequence lengths.

### What FlashAttention does **not** provide

- A request / page manager.
- Wrapper objects for batched decode or prefill.
- Workspace planning APIs.
- Automatic continuous-batching metadata generation.

### Mental model for logical-to-physical translation

For each request, maintain `seqlen` (logical tokens in cache) and `blocks` (ordered list of physical page IDs). The kernel interprets token position `t` in request `i` as:

```
logical_page  = t // page_block_size
offset        = t %  page_block_size
physical_page = block_table[i, logical_page]
```


## Cache allocation strategy

### Non-paged cache

- Preallocate `k_cache` / `v_cache` to max sequence length per cache row.
- Keep a cache row per active or reusable slot.
- Use `cache_batch_idx` when active batch order differs from cache row order.

Simpler, wasteful under heterogeneous request lengths. Fine for early correctness work.

### Paged cache

- Preallocate a global page pool on device.
- Allocate pages as requests grow.
- Free pages when requests finish.
- Rebuild `block_table` for each active decode batch.

Production-oriented. Matches the paged-attention design used by vLLM and SGLang (see [`algorithms/paged-attention/`](../algorithms/paged-attention.md)).

## RoPE handling

`flash_attn_with_kvcache` can apply rotary embedding inline if `rotary_cos` and `rotary_sin` are passed:

- `k` is rotated at positions `cache_seqlens, cache_seqlens + 1, ...`
- if `causal=True` or local attention is on, `q` is rotated at those same advancing positions
- otherwise `q` is rotated at `cache_seqlens` only
- `rotary_dim` must be divisible by 16
- `rotary_interleaved` switches between interleaved and GPT-NeoX-style pairing

For models with non-standard RoPE (Llama-3 scaled, M-RoPE, MLA decoupled RoPE), apply RoPE yourself before the kernel and do not use the built-in rotation path.

## MQA / GQA constraints

Q can have `nheads_q`, K/V can have `nheads_kv`, with the requirement:

```
nheads_q % nheads_kv == 0
```

The CUDA implementation enforces this.

## Shape and dtype requirements

Hot checklist for the CUDA path:

- `head_dim` must be `<= 256`
- `head_dim` must be a multiple of 8
- Last dim must be contiguous
- `cache_seqlens`, `cache_batch_idx`, `block_table` must be `torch.int32`
- `page_block_size` must be divisible by 256 (paged KV only)

## Continuous-batching responsibilities

The engine rebuilds per-step metadata. At each decode step:

- compact the active request list
- map active requests to cache slots or page tables
- build `cache_batch_idx` if active batch order differs from storage order
- rebuild `cache_seqlens`
- rebuild `block_table` if using paged KV
- allocate more pages before any request crosses a page boundary

A clean serving design typically has: one request state per live sequence, one KV manager for allocation / free, and one decode-batch builder that emits the per-step tensors.

## Cleanup

When a request finishes:

- remove it from the scheduler
- free its physical pages (paged) or mark its cache row reusable (dense)
- drop any page-ownership metadata tied to dead requests

FlashAttention does not reclaim cache for you.

## FA2 vs FA3 vs FA4

| Aspect | FA2 | FA3 | FA4 |
|:-------|:----|:----|:----|
| Install | `pip install flash-attn` | `cd hopper && python setup.py install` | `pip install flash-attn-4` |
| Import | `from flash_attn import ...` | `import flash_attn_interface` | `from flash_attn.cute import ...` |
| Hardware | sm_80+ (Ampere / Ada / Hopper) | sm_90+ (Hopper only) | Hopper + Blackwell |
| FP8 | — | **yes** (E4M3 / E5M2, fwd only) | yes |
| FP4 | — | — | paths available |
| TMA / WGMMA | no | yes | yes (+ tcgen05 on Blackwell) |
| Status | stable | **beta** | new / CuTeDSL-based |

On H100-class serving, FA3 is the perf leader but still beta — benchmark before committing. On Blackwell, FA4 is the right target.

## Long-context decode — split-KV (`num_splits`)

Decode at small batch with very long contexts is memory-bandwidth-bound. `flash_attn_with_kvcache(..., num_splits=0)` asks FA to heuristically split the KV dimension and run multiple kernels in parallel, combining partial results via log-sum-exp. Override with `num_splits >= 2` if the heuristic underprovides.

The combine step is available as `flash_attn_combine(out_partial, lse_partial)` in FA3 for custom scheduling. Relevant source: `hopper/benchmark_split_kv.py`, `flash_fwd_combine_kernel.h`.

Rule of thumb: at batch=1 and context ≥ 32k, split-KV is typically a 2–5× decode latency win. At larger batch, the GPU is already saturated and splitting offers less.

## Head-dim and dtype support

| FA2 | FA3 | FA4 |
|:----|:----|:----|
| head_dim ∈ {32, 64, 96, 128, 192, 224, 256} | {64, 96, 128, 192, 256} | arch-dependent |
| fp16, bf16 | fp16, bf16 (fwd+bwd); fp8 fwd | bf16, fp8, fp4 |
| bf16 requires sm_80+ | — | — |

Head dim must be a multiple of 8. Last tensor dim must be contiguous. Accumulation is always FP32.

## ROCm (AMD MI200 / MI300 / RDNA3/4)

FA has a full ROCm backend in two flavors:

- **Composable Kernel (CK) backend** — the CDNA-native path; best perf on MI300X / MI325X
- **Triton-AMD backend** — enable via `FLASH_ATTENTION_TRITON_AMD_ENABLE=TRUE`; broader op coverage, follows FA3's interface for FP8

Installed via ROCm-specific wheels. See upstream README for the up-to-date matrix.

## torch.compile integration

FA2 ≥ 2.7 registers `_torch_custom_op_wrapper` / `register_fake` for PyTorch ≥ 2.4, so the entry points appear as opaque-but-shape-inferred ops to Dynamo — works inside `torch.compile` regions, usable with `mode="reduce-overhead"` (CUDA graphs). Below PyTorch 2.4 the wrappers no-op and graph breaks will appear at each FA call.

## HuggingFace `kernels` integration

```python
from kernels import get_kernel
fa = get_kernel("kernels-community/flash-attn2")      # or "flash-attn3"
```

Convenient for notebooks and rapid iteration; production engines usually import directly.

## CUDA-graph compatibility

Both varlen and kvcache APIs can be captured only when replay keeps the same
effective shapes and launch path:

- **Per-launch shapes are fixed**: `max_seqlen_*` constants, `block_table` sized at `(max_batch, max_blocks)`, `cache_seqlens` sized at `max_batch`.
- **No Python branches inside the captured region**: branch on batch size before capture, not during replay.
- **Paged mode**: `cache_block_size` is a constant; `k_cache` / `v_cache` addresses must be stable across replays.

Do **not** assume one graph captured for `cache_seqlens=N` can replay correctly
for `cache_seqlens=N+1`. Validate this for the exact FlashAttention version and
kernel path. In local FA2 `flash_attn_with_kvcache` probes, replaying a graph
captured at a shorter `cache_seqlens` continued to produce the shorter-length
result after `cache_seqlens` was increased, for both contiguous and paged KV.
For growing decode, either capture by length/bucket, use fixed-length padding,
or leave FA eager and capture the surrounding model piecewise.

See [`backends/cuda-graph/`](cuda-graph.md) for the capture lifecycle.

## Per-engine integration

| Engine | FA integration |
|:-------|:---------------|
| vLLM | `vllm/v1/attention/backends/flash_attn.py` — wraps `flash_attn_varlen_func` and `flash_attn_with_kvcache`; handles block-table conversion |
| SGLang | `python/sglang/srt/layers/attention/flashattention_backend.py` |
| TRT-LLM | `tensorrt_llm/_torch/attention_backend/` — FlashAttention backend alongside TRT-LLM-native |

## Pitfalls

1. **Treating FlashAttention like FlashInfer.** There is no public wrapper owning planning state — no `BatchDecodeWithPagedKVCacheWrapper` equivalent. The serving layer is yours to write.
2. **Forgetting that paged KV is only a layout contract.** `block_table` is the logical-to-physical translation the kernel needs. It is not optional bookkeeping.
3. **Wrong dtype for metadata.** `cache_seqlens`, `cache_batch_idx`, `block_table` all `torch.int32`. int64 silently gives wrong results or crashes.
4. **Wrong page size.** `page_block_size` not a multiple of 256 → invalid paged KV path.
5. **Duplicate `cache_batch_idx` while appending.** If indices aren't distinct and `k` / `v` are provided, the cache update for duplicates is ambiguous. Avoid duplicate storage indices in a step that writes cache.
6. **Assuming backward is available.** `flash_attn_with_kvcache` is inference-only.
7. **Forgetting bottom-right causal alignment.** `flash_attn_with_kvcache` aligns causal masking to the bottom-right corner when query and key lengths differ. Matters when validating chunked prefill or mixed-length decode.
8. **Mixing FP8 on FA3 without scales.** FA3 FP8 needs Q/K/V scales; passing dequantized tensors loses the win.
9. **Custom / non-standard RoPE.** Don't rely on the built-in rotary for Llama-3 scaled, M-RoPE, or MLA decoupled RoPE — apply RoPE yourself first.

## Validation checklist

Before calling the integration correct:

- [ ] Dense-cache decode matches a reference implementation.
- [ ] Paged-cache decode matches dense-cache decode.
- [ ] Batch reorder with `cache_batch_idx` is correct.
- [ ] Page-boundary appends are correct.
- [ ] MQA / GQA cases match reference outputs.
- [ ] Long-context decode is numerically stable.
- [ ] Finished requests actually free their pages.

## Integration recipe

1. Keep your model as the owner of projections and MLP blocks.
2. Use FlashAttention only for the attention kernel call.
3. Implement a KV manager in the serving layer (see [`#paged-kv-manager`](#paged-kv-manager)).
4. Start with dense cache rows for simplest correctness.
5. Move to paged KV once correctness is stable.
6. Use `flash_attn_varlen_func` for packed prompt prefill.
7. Use `flash_attn_with_kvcache` for decode; also for chunked prefill if you want unified append semantics.

## Out of scope — kernel implementation

Writing FlashAttention-style kernels from scratch: see `agent-gpu-skills`'s `triton-skill` and `cutlass-skill`.

## Additional references
- Upstream: <https://github.com/Dao-AILab/flash-attention> (README + `csrc/flash_attn/flash_api.cpp` document the dtype / shape checks and paged-KV contract)
- vLLM paged-attention design: <https://docs.vllm.ai/en/latest/design/paged_attention/>

## See also

- [`algorithms/attention-variants/`](../algorithms/attention-variants.md) — MHA / MQA / GQA / SWA / cross-attention catalog this backend implements
- [`backends/flashinfer/`](flashinfer.md) — higher-level wrapper alternative (plan/run, native paged KV)
- [`algorithms/paged-attention/`](../algorithms/paged-attention.md) — paged-KV design patterns across engines
- [`backends/cuda-graph/`](cuda-graph.md) — capture lifecycle for graph-safe decode
- [`hardware/nvidia/`](../hardware/nvidia.md) — where FA3 wins (Hopper section)


---

## Paged Kv Manager

The serving-layer code FlashAttention does *not* provide. Plug this under
a real scheduler and it becomes the "block manager" for a paged-KV
FlashAttention serving engine.

## Request state

```python
class RequestState:
    def __init__(self):
        self.seqlen = 0     # logical token count stored in cache
        self.blocks = []    # ordered list of physical page IDs
```

## Page-count helper

```python
def pages_needed(seqlen, page_block_size):
    return (seqlen + page_block_size - 1) // page_block_size
```

## Growing a request's page list before append

Call this before any step that writes `append_tokens` new K/V into the
cache for `req`. `free_blocks` is a container of available physical
page IDs (list, deque, or a free-list).

```python
def ensure_capacity(req, append_tokens, free_blocks, page_block_size):
    old_pages = pages_needed(req.seqlen, page_block_size)
    new_pages = pages_needed(req.seqlen + append_tokens, page_block_size)
    for _ in range(new_pages - old_pages):
        req.blocks.append(free_blocks.pop())
```

## Building per-step tensors

The engine rebuilds `block_table` and `cache_seqlens` every decode step
because the active batch (and thus the logical-to-physical mapping
exposed to the kernel) changes.

```python
def build_block_table(reqs, device):
    max_blocks = max(len(r.blocks) for r in reqs)
    block_table = torch.zeros(
        (len(reqs), max_blocks), dtype=torch.int32, device=device,
    )
    cache_seqlens = torch.tensor(
        [r.seqlen for r in reqs], dtype=torch.int32, device=device,
    )
    for i, r in enumerate(reqs):
        if r.blocks:
            block_table[i, :len(r.blocks)] = torch.tensor(
                r.blocks, dtype=torch.int32, device=device,
            )
    return block_table, cache_seqlens
```

Both tensors must be `torch.int32` — FlashAttention enforces this.

## Freeing pages on request completion

```python
def free_request(req, free_blocks):
    for page_id in req.blocks:
        free_blocks.append(page_id)
    req.blocks.clear()
    req.seqlen = 0
```

Do this in the scheduler's finish-handler — FlashAttention will not
reclaim pages for you.

## CUDA-graph-friendly variant

For CUDA-graph capture, avoid the per-step Python loop and per-step
tensor construction. Instead:

- Pre-allocate static `block_table` (`max_batch, max_blocks`) and
  `cache_seqlens` (`max_batch,`) tensors on device.
- Before replay, fill the first `actual_bs` rows with real data and pad
  the remaining rows with safe values (garbage page IDs reserved at
  init, `seqlen=0` won't decode anything real but the shapes stay
  fixed).
- `graph.replay()` reads from the static tensors.

See `backends/cuda-graph/SKILL.md` for the broader capture lifecycle.
