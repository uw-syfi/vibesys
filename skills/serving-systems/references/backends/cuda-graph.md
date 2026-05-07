# CUDA Graph for serving

Capture-and-replay a forward pass so every per-kernel CPU launch happens at capture time, not per step. The remainder of this skill covers:

- **The full-graph pattern** used by from-scratch engines: capture the whole decode forward, pad batches, eager-fallback for oversized.
- **The piecewise pattern** used by vLLM v1 in production: split at attention, capture each graph-safe range independently, attention runs eager between partitions.
- **Attention backend compatibility**: SDPA fixed-shape decode, FlashInfer (`use_cuda_graph=True`), and FlashAttention (`flash_attn_with_kvcache` vs varlen).
- **Capturing non-decoder components**: vision encoders, Whisper encoder, VAE decoders, diffusion denoisers, codec decoders.

## Prerequisites

- `torch.cuda.CUDAGraph` support (CUDA 11+)
- For the full-graph pattern: a serving engine with static/bucketed KV cache (SDPA) or paged KV cache (FlashInfer or FlashAttention)
- For the piecewise pattern: `torch.compile` with a custom backend (vLLM's `VLLM_COMPILE` or an equivalent split-at-attention backend)

## What to capture and what not to

- **Capture**: forward pass where shapes are stable — decode loop at fixed batch size, Whisper encoder at fixed duration, diffusion denoiser step, VAE decoder at fixed latent size.
- **Do NOT capture**: prefill with variable prompt lengths (in the full-graph pattern — piecewise handles it), token sampling (`.item()` / `.tolist()` force CPU syncs), `flashinfer.*.plan()` (must run outside the graph to update workspace), autotune-triggering kernels on first call (warmup first).

## Full vs piecewise capture

Two strategies for an LLM decoder. Both compose with the same eager-fallback logic.

| Strategy | What's captured | What stays eager | When to pick |
|:---------|:----------------|:-----------------|:-------------|
| **Full** | embedding → all layers → lm_head, one graph per batch size | sampler, detokenize, scheduler | fixed-shape decode, small-to-medium models, from-scratch engines |
| **Piecewise** | each graph-safe FX subgraph between attention ops, one CUDA graph per subgraph per shape | attention ops themselves, and anything in `splitting_ops` | varlen prefill, mixed prefill-decode batches, models with unavoidable graph breaks |

Full is simpler. Piecewise is more flexible — especially when attention has variable-length queries (varlen prefill) or the model has other uncaptureable ops. **vLLM v1's default is `FULL_AND_PIECEWISE`**: use a full graph for uniform decode batches, fall back to piecewise for prefill / mixed batches, fall back to eager for everything else. See the "Piecewise capture (vLLM pattern)" section below.

## Attention backend compatibility

### PyTorch SDPA

SDPA is graph-safe when the decode attention shapes are fixed. For single-batch
decode, the graph key is not just batch size: it also includes visible KV
length. Three practical patterns:

| Pattern | Graph key | Tradeoff |
|:--------|:----------|:---------|
| Exact visible length | `(bs=1, kv_len)` | no wasted attention FLOPs, many graphs |
| Fixed max length | `(bs=1, max_kv_len)` | one graph, attends over max length every token |
| Fixed buckets | `(bs=1, bucket_len)` | few graphs, bounded wasted FLOPs |

For one fixed-max graph, keep `k_cache` / `v_cache` full-sized and mutate only
the static mask values before replay. Do not slice `:cur_len` inside a graph
you expect to reuse for another length. See [`backends/sdpa/`](sdpa.md)
for the single-batch implementation pattern.

### FlashInfer

Wrappers have explicit graph support via `use_cuda_graph=True` plus pre-allocated static buffers passed at construction time. `plan()` does in-place copies into those buffers instead of allocating fresh tensors, so the captured region only ever sees stable addresses:

```python
decode_wrapper = flashinfer.BatchDecodeWithPagedKVCacheWrapper(
    workspace, "NHD", use_cuda_graph=True,
    paged_kv_indptr_buffer=static_kv_indptr,
    paged_kv_indices_buffer=static_kv_indices,
    paged_kv_last_page_len_buffer=static_kv_last_page_len,
)
```

`plan()` must be called **outside** the graph (it updates workspace); `run()` is what gets captured.

### FlashAttention

No `use_cuda_graph` flag — FA just cares that shapes and addresses are stable. Two sub-cases:

**`flash_attn_with_kvcache` (paged decode)** — can be captured when the replay
uses the same effective shapes / launch path as capture. Do **not** assume one
graph captured at `cache_seqlens=N` will correctly replay at `N+1`; validate
this for the exact FA version and kernel path. Metadata tensors need to be
int32 at a fixed maximum size:

| Tensor | Shape | Notes |
|:-------|:------|:------|
| `block_table` | `(max_bs, max_blocks_per_seq)` int32 | oversized; unused slots point to garbage pages |
| `cache_seqlens` | `(max_bs,)` int32 | one per request |
| `k_cache`, `v_cache` | `(num_blocks, page_block_size, num_kv_heads, head_dim)` | addresses stable by design |

`page_block_size` must be a multiple of 256 — same as the non-graph case.

**`flash_attn_varlen_func` (prefill / chunked prefill)** — has `cu_seqlens` that change shape step-to-step. Usually **left eager** and paired with piecewise capture for the rest of the forward. It *is* possible to capture by preallocating `cu_seqlens` at max length and padding unused slots, but the extra FLOPs on padded heads often negate the win.

### Triton-based backends

Autotune is the landmine. Warmup must cover every shape the engine will replay at — otherwise the first replay triggers autotune inside the captured region, corrupting the graph. See [`backends/triton-kernels/`](triton-kernels.md).

## Full-graph from-scratch pattern


### Static Tensors

CUDA graphs require all tensor addresses to be fixed at capture time. Pre-allocate per-batch-size:

| Tensor | Shape | Notes |
|---|---|---|
| `static_input_ids` | `(bs, 1)` long | Token IDs |
| `static_position_ids` | `(bs, 1)` long | RoPE positions |
| `static_logits` | `(bs, 1, vocab)` | Output (set during capture) |
| `static_kv_indptr` | `(bs+1,)` int32 | Page table boundaries |
| `static_kv_indices` | `(max_num_pages,)` int32 | Oversized; safe for any page layout |
| `static_kv_last_page_len` | `(bs,)` int32 | Last page occupancy |
| `static_batch_indices` | `(bs,)` int32 | For `append_paged_kv_cache` |
| `static_positions` | `(bs,)` int32 | For `append_paged_kv_cache` |

### FlashInfer Integration

Each batch size gets a **dedicated** `BatchDecodeWithPagedKVCacheWrapper` with `use_cuda_graph=True` and static buffer pointers:

```python
decode_wrapper = flashinfer.BatchDecodeWithPagedKVCacheWrapper(
    workspace, "NHD", use_cuda_graph=True,
    paged_kv_indptr_buffer=static_kv_indptr,
    paged_kv_indices_buffer=static_kv_indices,
    paged_kv_last_page_len_buffer=static_kv_last_page_len,
)
```

The workspace (128 MB) can be shared across all per-batch-size wrappers since only one replays at a time. FlashInfer's `plan()` copies data into the static buffers via `.copy_()` internally.

## Capture Lifecycle

1. **Allocate** static tensors and create dedicated decode wrapper
2. **Fill dummy metadata** — each slot points to 1 garbage page
3. **Warmup** — run 3 iterations (plan + forward) to trigger all lazy CUDA allocations
4. **Plan** one final time
5. **Capture** — `torch.cuda.CUDAGraph()` around the forward call
6. **Store** graph, wrapper, and static tensor references

```python
graph = torch.cuda.CUDAGraph()
with torch.cuda.graph(graph):
    static_logits = _forward(batch_size, decode_wrapper, static_input_ids, ...)
```

## Replay Lifecycle

1. **Copy** real data into static buffers for first `actual_bs` slots
2. **Pad** remaining slots: `input_ids=0`, `position_ids=0`, KV metadata pointing to garbage pages
3. **Plan** outside the graph (updates workspace)
4. **Replay** — `graph.replay()`
5. **Slice** — return `static_logits[:actual_bs]`

## Batch Padding

Pad actual batch to the smallest captured size >= actual batch. If no captured size fits, fall back to eager.

### Garbage Pages for Padding

Reserve `max_batch_size` pages from the KV cache pool at init time. These are never freed and serve as dummy KV targets for padded slots:

```python
# For each padded slot i (actual_bs <= i < padded_bs):
kv_indptr[i + 1] = total_real_pages + (i - actual_bs + 1)
kv_indices[total_real_pages + (i - actual_bs)] = garbage_pages[i - actual_bs]
kv_last_page_len[i] = 1
```

## Engine Integration

Refactor `_decode_step()` into a dispatcher:

```python
def _decode_step(self):
    if self.cuda_graph_runner is not None:
        padded_bs = self.cuda_graph_runner.get_padded_batch_size(batch_size)
        if padded_bs is not None:
            self._decode_step_cuda_graph(active, batch_size, padded_bs)
            return
    self._decode_step_eager(active, batch_size)  # original logic
```

In `_decode_step_cuda_graph`, compute `batch_indices` and `positions` directly instead of calling `get_batch_indices_positions` (avoids launching a Triton kernel that would conflict with graph replay):

```python
batch_indices = torch.arange(batch_size, dtype=torch.int32, device=device)
positions = torch.tensor([seq_len_after - 1 for each request], dtype=torch.int32, device=device)
```

## Critical Pitfall: `_forward` Must Take All Tensors as Parameters

The `_forward` method (the captured computation) must receive `kv_indices`, `kv_indptr`, and `kv_last_page_len` as **parameters**, not look them up from a dict keyed by batch_size. During capture, the dict isn't populated yet — those entries are stored *after* capture completes.

## Piecewise capture (vLLM v1 pattern)

Full-graph capture assumes everything in the forward is graph-safe. In production that's often false: attention has variable-length queries during prefill, custom ops break the graph, or MoE dispatch has dynamic shapes. Piecewise capture keeps what can be captured and leaves the rest eager.

### The core idea

1. `torch.compile` traces the model to an FX graph.
2. The graph is **split at attention ops** (and anything else in `splitting_ops`).
3. Each resulting FX subgraph is compiled to a kernel via Inductor.
4. Each compiled subgraph is wrapped in a `CUDAGraphWrapper` that captures-and-replays it independently for each shape it sees.
5. At runtime, attention ops execute **eagerly between** the captured partitions — no graph includes attention.

Result: captured kernel-launch overhead is eliminated for everything except the attention call itself. Variable-shape attention (FA varlen) just works.

### The `CUDAGraphMode` enum

Defined at `vllm/config/compilation.py` (~line 53):

| Value | Meaning |
|:------|:--------|
| `NONE` | no CUDA graphs; full eager |
| `PIECEWISE` | split at attention, capture each FX subgraph, attention eager |
| `FULL` | capture entire forward as one graph |
| `FULL_DECODE_ONLY` | FULL for uniform decode batches, NONE for prefill / mixed |
| `FULL_AND_PIECEWISE` | FULL for uniform decode, PIECEWISE for prefill / mixed — **v1 default** |

### Implementation surface

| File | Role |
|:-----|:-----|
| `vllm/config/compilation.py` | `CUDAGraphMode` enum, `CompilationConfig`, `splitting_ops` list, `is_attention_compiled_piecewise()` |
| `vllm/compilation/partition_rules.py` | `should_split(node, splitting_ops)` — which FX nodes trigger a split |
| `vllm/compilation/piecewise_backend.py` | `PiecewiseBackend` — compiles each FX subgraph per shape range |
| `vllm/compilation/cuda_graph.py` | `CUDAGraphWrapper.__call__()` — per-shape capture / replay |
| `vllm/v1/cudagraph_dispatcher.py` | `CudagraphDispatcher.dispatch()` — per-batch mode decision |
| `vllm/v1/worker/gpu/cudagraph_utils.py` | `ModelCudaGraphManager` (MRV2 side) — handles PIECEWISE and FULL uniformly |
| `vllm/forward_context.py` | `ForwardContext` carries `cudagraph_runtime_mode` and `batch_descriptor` so wrappers know what to do |

### The split itself

`splitting_ops` defaults to the attention family (`vllm::unified_attention_with_output`, `vllm::unified_mla_attention_with_output`, `vllm::mamba_mixer2`, …). A list lives at `CompilationConfig._attention_ops` in `compilation.py` (~line 737).

When `cg_mode == CUDAGraphMode.PIECEWISE`, the forward context sets attention metadata to `None` inside captured partitions — the captured code can't see it, forcing attention to run eagerly outside the capture boundary.

### Per-batch dispatch

`CudagraphDispatcher.dispatch()` picks a mode per batch using a descriptor (`num_tokens`, `uniform_decode`, `has_lora`, etc.):

1. Try FULL keys first — require exact `num_reqs` match.
2. Fall back to PIECEWISE — keys have `num_reqs=None` so any request count matches.
3. Fall back to NONE — eager.

This is why FULL_AND_PIECEWISE is the default: uniform decode batches hit the FULL fast path; prefill / mixed batches cleanly degrade to piecewise instead of all-eager.

### Piecewise with MRV2

MRV2's `ModelCudaGraphManager` (`vllm/v1/worker/gpu/cudagraph_utils.py`, ~line 263) handles PIECEWISE and FULL through the same `.capture()` / `.dispatch()` pair. A notable detail at ~line 335:

```python
with set_forward_context(
    attn_metadata if cg_mode != CUDAGraphMode.PIECEWISE else None,
    self.vllm_config,
    cudagraph_runtime_mode=cg_mode,
):
    model_output = model(**model_inputs)
```

For PIECEWISE, attention metadata is explicitly `None` in the forward context — captured partitions cannot accidentally consume it.

### When to prefer piecewise

- Varlen prefill / chunked prefill (FlashAttention varlen shapes change per step).
- Models with irreducible graph breaks (custom ops, dynamic control flow, non-graphable MoE dispatch).
- Mixed prefill-decode batches.
- Large models where full-graph capture would blow the graph memory budget.

### When FULL still wins

- Uniform decode-only batches with fixed shapes.
- Small models where every op is graph-safe and the full-graph overhead saved is large relative to the forward.

## Capturing non-LLM-decoder components

Everything above focuses on the decoder loop. Other components along a serving pipeline also benefit from capture when they run often enough. The rules of thumb: **fixed input shape → capture per shape**, **bounded variable input → capture one graph per shape bucket**, **iterative computation that runs N times per request → ideal capture candidate**.

### Vision encoders (token-splice VLMs)

| Model class | Input | Strategy |
|:-----------|:------|:---------|
| LLaVA / LLaVA-1.5 | fixed 336² per tile | capture one graph for the fixed tile shape |
| LLaVA-NeXT | dynamic 1×1 / 1×2 / 2×2 / ... tile grids | capture one graph per grid configuration (~8 total) |
| Qwen2-VL / Qwen3-VL (NaViT) | variable resolution within `min_pixels` / `max_pixels` bounds | bucket by token count; one graph per bucket |

For the NaViT case, choose N buckets covering the resolution range (e.g., 256 / 512 / 1024 / 2048 / 4096 tokens) and pad to the nearest bucket before invoking the graph. See [`models/vision-language/`](../models/vision-language.md).

### Whisper encoder (speech-language)

Whisper pads audio to 30 s chunks before the encoder, so the encoder shape is **always fixed**. Ideal capture candidate: one graph, applied once per utterance. Cross-attention K/V are computed from the encoder output inside the decoder path — capture the decoder step as usual.

For streaming ASR variants (`qwen3_asr_realtime`), the encoder runs on rolling chunks — the chunk size is fixed, so capture per chunk.

### Audio encoders for audio-LLM fusion

Qwen2-Audio, Qwen3-ASR, Step-Audio-2: a Whisper-style encoder + adaptor → text LLM. Encoder shape depends on input duration, bounded by a max; capture per duration bucket or per `max_audio_seconds`.

### VAE decoders (image / video gen)

VAE decoders are **fixed-shape transforms** (latent → pixels). One graph per output resolution. Often the peak-memory step of image / video generation — CPU launch overhead is tiny relative to compute, so the graph win is modest, but capture is trivial and free of downsides.

For **tiled VAE decode** (very-high-res images, video), each tile is a fixed shape → capture per tile shape.

### Diffusion denoiser steps (image / video gen)

A diffusion model runs the same denoiser `N` times (step schedulers: 4–50 steps) on a fixed-shape latent. **Ideal capture candidate**: one graph, replayed per step. With classifier-free guidance the effective batch doubles — capture both `bs` and `bs * 2` to handle CFG-on and CFG-off.

Flow-matching models (SD3, Flux) are identical from a capture perspective. Step-caching techniques (TEA-cache) skip some blocks per step, which breaks naive capture — see the step-caching library's docs.

### Neural codec decoders (speech generation)

Mimi, SNAC, DAC, EnCodec: token sequence → waveform. Shape is determined by the codec's fixed frame rate × number of frames decoded per call. Capture per `(codec, decode_chunk_size)` pair.

For VoxServe-class serving (see [`models/speech-generation/`](../models/speech-generation.md)), the `detokenize_interval` defines chunk size; one graph per codec / chunk-size pair.

### Cross-attention (encoder-decoder models)

In Whisper / mllama: K_enc and V_enc are computed from the encoder output once per utterance. Capture **the per-layer decoder forward** (self-attn + cross-attn + MLP) as you would a standard decoder, treating K_enc / V_enc as fixed-addressed static tensors. The capture picks up cross-attention for free.

### Shape bucketing (the common pattern)

For any variable-shape component, the approach is the same:

1. Pick N buckets covering the expected range, exponentially spaced (1, 2, 4, 8, 16 tokens; or 256, 512, 1024, 2048 pixels).
2. Capture one graph per bucket during warmup.
3. At runtime, pad the input up to the next bucket size; replay that graph.
4. For oversize inputs, fall back to eager.

Tradeoff: more buckets = more memory + capture time, but tighter padding. Production deployments tune this empirically.

### When capture doesn't help

- Component runs once per request and is compute-heavy (e.g., long text-encoder pass for SDXL). CPU launch overhead is a rounding error; skip capture.
- Shape varies continuously with no natural buckets (very rare).
- Inputs come from a slow CPU pipeline — the overhead isn't in kernel launch anyway. Profile first.

## Performance Impact

On Llama-3.2-1B-Instruct, CUDA graphs reduce per-token decode latency by ~4-5x (e.g., median TPOT from 18ms to 4ms) by eliminating per-kernel CPU launch overhead. Throughput gains are more modest (~10-17%) because prefill (which stays eager) dominates at higher concurrency.

## See also

- [`algorithms/async-scheduling/`](../algorithms/async-scheduling.md) — hides *scheduler-level* CPU overhead; orthogonal to the kernel-launch overhead CUDA graphs address, and stacks with it. Covers vLLM's MRV2 which manages piecewise capture.
- [`algorithms/batched-sampling/`](../algorithms/batched-sampling.md) — the sampler is one of the kernels typically inside the captured decode pass
- [`backends/flashinfer/`](flashinfer.md) — FlashInfer wrappers are CUDA-graph-safe when configured with static buffers (`use_cuda_graph=True`)
- [`backends/flashattention/`](flashattention.md) — FA `flash_attn_with_kvcache` is graph-safe only for fixed/bucketed effective lengths; `varlen_func` usually left eager or piecewise
- [`backends/triton-kernels/`](triton-kernels.md) — autotune warmup before capture is mandatory
- [`frameworks/pytorch/`](../frameworks/pytorch.md) — `torch.compile(mode="reduce-overhead")` uses CUDA graphs under the hood; the piecewise backend is a `torch.compile` custom backend
- [`models/vision-language/`](../models/vision-language.md), [`models/speech-language/`](../models/speech-language.md), [`models/speech-generation/`](../models/speech-generation.md), [`models/image-generation/`](../models/image-generation.md), [`models/video-generation/`](../models/video-generation.md) — the components discussed in "Capturing non-LLM-decoder components"


---

## Runner

## Constructor

Accepts: model, kv_manager, device, dtype, batch_sizes (default `[1, 2, 4, 8, 16, 32]`).

Key initialization:
1. Reserve **garbage pages** from kv_manager for padding (`max_batch_size` pages)
2. Allocate a **shared 128 MB workspace** (`torch.zeros(128*1024*1024, dtype=torch.uint8)`)
3. Initialize empty dicts for per-batch-size state: `graphs`, `decode_wrappers`, `static_*` tensors

```python
class CUDAGraphRunner:
    def __init__(self, model, kv_manager, device, dtype, batch_sizes=None):
        self.batch_sizes = sorted(batch_sizes or [1, 2, 4, 8, 16, 32])
        max_bs = max(self.batch_sizes)
        self.garbage_pages = kv_manager._allocate_pages(max_bs)
        self.workspace = torch.zeros(128 * 1024 * 1024, dtype=torch.uint8, device=device)
        # dicts keyed by batch_size:
        self.graphs = {}
        self.decode_wrappers = {}
        self.static_input_ids = {}
        # ... (all static tensor dicts)
```

## `_capture(batch_size)` Method

### 1. Allocate Static Tensors

All tensors are allocated once and retain their GPU addresses forever:

```python
static_input_ids = torch.zeros(batch_size, 1, dtype=torch.long, device=device)
static_position_ids = torch.zeros(batch_size, 1, dtype=torch.long, device=device)
static_kv_indptr = torch.zeros(batch_size + 1, dtype=torch.int32, device=device)
static_kv_indices = torch.zeros(max_num_pages, dtype=torch.int32, device=device)  # oversized
static_kv_last_page_len = torch.ones(batch_size, dtype=torch.int32, device=device)
static_batch_indices = torch.arange(batch_size, dtype=torch.int32, device=device)
static_positions = torch.zeros(batch_size, dtype=torch.int32, device=device)
```

### 2. Create Dedicated Decode Wrapper

Pass static buffers so FlashInfer's `plan()` writes into them via `.copy_()`:

```python
decode_wrapper = flashinfer.BatchDecodeWithPagedKVCacheWrapper(
    self.workspace, "NHD", use_cuda_graph=True,
    paged_kv_indptr_buffer=static_kv_indptr,
    paged_kv_indices_buffer=static_kv_indices,
    paged_kv_last_page_len_buffer=static_kv_last_page_len,
)
```

### 3. Fill Dummy Metadata

Each slot points to 1 garbage page so the forward pass has valid KV cache to read:

```python
for i in range(batch_size):
    static_kv_indptr[i + 1] = i + 1
    static_kv_indices[i] = self.garbage_pages[i % len(self.garbage_pages)]
    static_kv_last_page_len[i] = 1
```

### 4. Warmup (3 iterations)

Triggers all lazy CUDA allocations (cuBLAS handle creation, FlashInfer JIT, etc.):

```python
for _ in range(3):
    decode_wrapper.plan(indptr=..., indices=..., last_page_len=..., ...)
    self._forward(batch_size, decode_wrapper, static_input_ids, static_position_ids,
                  static_batch_indices, static_positions,
                  static_kv_indices, static_kv_indptr, static_kv_last_page_len)
```

### 5. Capture

One final `plan()` then capture:

```python
decode_wrapper.plan(...)
graph = torch.cuda.CUDAGraph()
with torch.cuda.graph(graph):
    static_logits = self._forward(batch_size, decode_wrapper, ...)
```

### 6. Store

```python
self.graphs[batch_size] = graph
self.decode_wrappers[batch_size] = decode_wrapper
self.static_logits[batch_size] = static_logits
# ... all other static tensors
```

## `_forward()` Method

Identical to the eager decode forward pass but operates entirely on static tensors. This is the function captured by the CUDA graph.

Signature — all tensors passed as parameters (not looked up from dicts):

```python
def _forward(self, batch_size, decode_wrapper,
             input_ids, position_ids, batch_indices, positions,
             kv_indices, kv_indptr, kv_last_page_len):
```

Body:
```python
hidden_states = model.model.embed_tokens(input_ids)
cos, sin = model.model.rotary_emb(hidden_states, position_ids)
for layer_idx, layer in enumerate(model.model.layers):
    residual = hidden_states
    hidden_states = layer.input_layernorm(hidden_states)
    # QKV proj → RoPE → reshape to NHD
    q, k, v = ...
    # Append to KV cache (uses passed-in kv_indices/kv_indptr/kv_last_page_len)
    flashinfer.page.append_paged_kv_cache(
        k_nhd, v_nhd, batch_indices, positions,
        kv_manager.kv_caches[layer_idx],
        kv_indices, kv_indptr, kv_last_page_len, "NHD")
    # Attention
    attn_output = decode_wrapper.run(q_fi, kv_manager.kv_caches[layer_idx])
    # O proj + residual + MLP
    ...
hidden_states = model.model.norm(hidden_states)
return model.lm_head(hidden_states)
```

## `replay()` Method

### Input Copying

Copy real data into the first `actual_bs` slots of static buffers:

```python
self.static_input_ids[padded_bs][:actual_bs].copy_(input_ids)
self.static_position_ids[padded_bs][:actual_bs].copy_(position_ids)
self.static_batch_indices[padded_bs][:actual_bs].copy_(batch_indices)
self.static_positions[padded_bs][:actual_bs].copy_(positions)
s_kv_indptr[:actual_bs + 1].copy_(kv_indptr)
s_kv_indices[:total_real_pages].copy_(kv_indices)
s_kv_last_page_len[:actual_bs].copy_(kv_last_page_len)
```

### Padding

For each padding slot `i` (from `actual_bs` to `padded_bs - 1`):
- `input_ids[i] = 0`, `position_ids[i] = 0`
- `batch_indices[i] = i` (so `append_paged_kv_cache` writes to the right slot)
- `positions[i] = 0`
- KV metadata: 1 garbage page per slot

```python
for i in range(num_pad):
    pad_idx = actual_bs + i
    s_kv_indptr[pad_idx + 1] = total_real_pages + i + 1
    s_kv_indices[total_real_pages + i] = self.garbage_pages[i]
    s_kv_last_page_len[pad_idx] = 1
```

### Plan + Replay + Slice

```python
self.decode_wrappers[padded_bs].plan(...)  # OUTSIDE graph
self.graphs[padded_bs].replay()
return self.static_logits[padded_bs][:actual_bs]
```

## `get_padded_batch_size()` Method

Returns the smallest captured batch size >= actual batch, or `None` for eager fallback:

```python
def get_padded_batch_size(self, actual_bs):
    for bs in self.batch_sizes:  # sorted ascending
        if bs >= actual_bs:
            return bs
    return None
```

## Engine-Side Changes

### Decode Step Dispatcher

```python
def _decode_step(self):
    active = [r for r in self._active if not r.finished]
    batch_size = len(active)
    if self.cuda_graph_runner is not None:
        padded_bs = self.cuda_graph_runner.get_padded_batch_size(batch_size)
        if padded_bs is not None:
            self._decode_step_cuda_graph(active, batch_size, padded_bs)
            return
    self._decode_step_eager(active, batch_size)
```

### CUDA Graph Decode Step

Same KV management as eager (append_token, build_batch_arrays), but:
- Compute `batch_indices = arange(bs)` and `positions = [seq_len_after - 1]` directly (no Triton kernel)
- Call `cuda_graph_runner.replay()`
- Sample from returned logits (outside graph)

### Lifespan

Call `engine.capture_cuda_graphs()` after engine creation, before `engine.start()`.

### CLI Flags

- `--cuda-graph-batch-sizes` (default `"1,2,4,8,16,32"`)
- `--no-cuda-graph` to disable
