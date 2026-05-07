# Paged attention

Each sequence's KV is stored in fixed-size blocks (pages) drawn from a pool, addressed via a per-request page table. Eliminates external fragmentation, enables dynamic growth, and opens the door to prefix sharing.

## Concept

- **Block (page)**: a fixed-size chunk of contiguous GPU memory holding KV for `block_size` consecutive tokens of a single request's single layer (typically `block_size ∈ {16, 32, 64, 128}`).
- **Block pool**: pre-allocated at startup to fill the KV memory budget.
- **Page table (block table)**: per-request list of block IDs giving the logical → physical mapping for that sequence's KV.
- **Last-page length**: how much of the final block is filled; everything before it is full.

A paged-attention kernel takes `(Q, K_pool, V_pool, page_table, last_page_len)` and walks the logical sequence by indirecting through the page table. Append writes a KV at the current last-page position, growing into a new block when it fills.

## Tensor layout choices

| Decision | Options | Notes |
|:---------|:--------|:------|
| Axis order | NHD `(num_heads, head_dim)` vs HND | FlashInfer wants NHD; FA2/FA3 flexible |
| One tensor per layer vs packed | both used | per-layer is simpler; packed saves an indirection |
| K and V in one tensor vs two | `(max_pages, 2, block_size, num_kv_heads, head_dim)` common for per-layer | |
| Block size | 16 / 32 / 64 / 128 | smaller = less waste at sequence ends, more kernel overhead |

## The batch-arrays interface

Paged-attention kernels (FlashInfer, vLLM) typically consume three int32 tensors describing all requests in a batch:

| Array | Shape | Meaning |
|:------|:------|:--------|
| `kv_indptr` | `(batch_size + 1,)` | prefix-sum of per-request page counts; request `i` owns pages in `[kv_indptr[i], kv_indptr[i+1])` |
| `kv_indices` | `(total_pages,)` | flat list of physical page IDs |
| `kv_last_page_len` | `(batch_size,)` | how full each request's last page is |

Helper `flashinfer.page.get_batch_indices_positions` converts these into per-token `(batch_idx, position)` pairs for the append kernel.

## Block-size tradeoffs

| Smaller block | Larger block |
|:--------------|:-------------|
| Less tail waste (last page is usually partial) | Fewer page-table entries, less indirection |
| More page-table updates per step | Kernel prefers contiguity |
| Finer prefix-sharing granularity (radix cache) | Coarser sharing |

16 is common default. Hopper-era kernels often prefer 64+ for TMA-friendliness.

## Compatibility

| Implementation | Engines | Backends | Hardware | Notes |
|:---------------|:--------|:---------|:---------|:------|
| FlashInfer paged | SGLang, vLLM | `backends/flashinfer/` | NVIDIA sm_80+ | NHD layout, plan/run pattern |
| FlashAttention 2/3 paged | vLLM v1 | `backends/flashattention/` | NVIDIA (FA3: sm_90+) | `flash_attn_with_kvcache(..., block_table=...)`; validate CUDA graphs with growing `cache_seqlens` or capture by bucket |
| Triton paged | vLLM, SGLang fallback | `backends/triton-kernels/` | NVIDIA + AMD | reference impl |
| vLLM custom PagedAttention | vLLM (optional path) | `$SERVE_REPOS/vllm/csrc/attention/` | NVIDIA | written for v0, still used in places |

## Engine pointers

| Engine | Block / page management | Attention backend files |
|:-------|:------------------------|:------------------------|
| vLLM v1 | `vllm/v1/core/{kv_cache_coordinator,kv_cache_manager,block_pool}.py` | `vllm/v1/attention/backends/{flash_attn,flashinfer,triton_attn}.py` |
| SGLang | `python/sglang/srt/mem_cache/{memory_pool,allocator,common}.py` | `python/sglang/srt/layers/attention/{flashinfer_backend,flashattention_backend,triton_backend}.py` |
| TensorRT-LLM | C++ side: `cpp/tensorrt_llm/batch_manager/` (KV cache manager) | `tensorrt_llm/_torch/attention_backend/` |

(Use `$SERVE_REPOS/<engine>/` prefix; see the engine SKILL.md files.)

## External KV stores — the connector pattern

Once the block pool is paged, the physical backing of a block no longer has to be GPU HBM. The **connector pattern** (TRT-LLM `KvCacheConnector`, vLLM `vllm/v1/kv_offload/`) makes "load / save a block from somewhere else" a first-class operation the scheduler can invoke.

Split into two halves so it composes cleanly with the existing scheduler loop:

- **Scheduler-side (leader only).** Decides *which* blocks need loading on request admission and *which* need saving at finish time. Key hooks:
  - `get_num_new_matched_tokens(request, num_computed_tokens) → (n_tokens, is_async)` — on admission, check the external store for a prefix hit.
  - `build_connector_meta(scheduler_output)` — emit a pickled metadata blob describing load / save tasks, broadcast to all workers per step.
  - `request_finished(request, block_ids) → bool` — optionally hold the blocks while an async save drains.
- **Worker-side (all ranks).** Executes the transfers against the registered KV tensors:
  - `register_kv_caches(kv_cache_tensor)` — one-time wiring of the GPU pool.
  - `start_load_kv(stream)` and `wait_for_layer_load(layer_idx, stream)` — layer-granular load synchronization, so compute on layer *k* can start as soon as layer *k*'s blocks are resident.
  - `save_kv_layer(layer_idx, stream)` / `wait_for_save(stream)` — mirror path for eviction.
  - `get_finished(...)` — polled each step to reap completed async transfers.

Concrete use cases this enables, all built on the same two interfaces:

| Use case | What the connector does |
|:---------|:------------------------|
| **CPU / NVMe offload** | `save_kv_layer` copies to host or mmap'd file when a block is evicted; `start_load_kv` restores on prefix hit |
| **Custom disagg KV transfer** | Scheduler identifies blocks needed on decode instance; worker pushes via NIXL / UCX / RDMA instead of the built-in path |
| **Cross-process KV sharing** | Blocks persisted to a shared store keyed by prefix hash; second LLM process picks up where the first left off |
| **P2P cache mirroring** | `save_kv_layer` fans out to peer GPUs / peer nodes for redundancy |

Pitfalls specific to connectors:

- **Block-granular is the wrong unit for slow backends.** One `.pt` file per block (the TRT-LLM reference example) collapses under filesystem metadata overhead. Real impls batch per-request or per-layer.
- **Blocking I/O in `save_kv_layer` stalls the GPU.** Offload the actual read/write to a background thread; the connector hook only enqueues the work and `wait_for_save` drains.
- **Partial-block hits.** The default reference impl only matches whole blocks; partial reuse requires either copying the matched tokens into a fresh block (see `copy_on_partial_reuse` in `algorithms/radix-prefix-caching/`) or a connector that understands intra-block offsets.

TRT-LLM location: `$SERVE_REPOS/TensorRT-LLM/tensorrt_llm/_torch/pyexecutor/connectors/` (scheduler + worker base classes), `examples/llm-api/llm_kv_cache_connector.py` (file-backed reference impl). vLLM's equivalent lives in `$SERVE_REPOS/vllm/vllm/v1/kv_offload/`.

## Pitfalls

- **`seq_lens` semantics differ across kernels.** FlashInfer's `get_batch_indices_positions` wants the total sequence length *including* the tokens being appended — passing the pre-append length yields negative positions and illegal memory access.
- **Garbage pages for padded batches.** When padding to a captured CUDA-graph batch size, padded slots still need a valid page ID in `kv_indices`. Reserve a pool of throwaway pages at engine init.
- **Append vs allocate order.** Allocate new page first, then append; otherwise the append kernel writes past the current last page.
- **Block size must agree between allocator, append kernel, and attention kernel.** A mismatch is silent and corrupts KV.
- **Engine-drives-layers when using wrapper-style backends.** With FlashInfer wrappers, the engine must call `append_paged_kv_cache` between each layer's QKV and attention — the model class becomes a weights holder only.

## See also

- `algorithms/attention-variants/` — orthogonal axis: which *flavor* of attention (MHA / GQA / MLA / SWA / cross) is stored in the paged pool
- `algorithms/radix-prefix-caching/` — prefix sharing built on the block pool
- `backends/flashinfer/` — paged attention wrappers
- `backends/flashattention/` — `flash_attn_with_kvcache` paged mode
- `algorithms/continuous-batching/` — paging enables this
