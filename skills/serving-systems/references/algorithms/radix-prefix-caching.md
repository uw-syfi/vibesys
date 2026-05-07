# Radix / prefix caching

Keep the KV cache of committed prefixes alive and reuse it for new requests that share a prefix. Matching is a radix-tree lookup keyed on token IDs. For workloads with high prefix overlap (RAG, few-shot, agent trees) the win on TTFT is dramatic.

## Concept

- **Radix tree node** = chunk of tokens with a pointer to the KV cache pages for those tokens.
- **Edge** = next-token ID (or token chunk).
- On a new request: walk the tree matching the prompt token-by-token; reuse matched pages; allocate and link new pages for the divergent suffix.
- **Reference counting**: nodes in use by a live request can't be evicted.
- **Eviction**: LRU over unreferenced nodes; freed pages return to the block pool.

Match granularity matters:

| Granularity | Pros | Cons | Used in |
|:-----------|:-----|:-----|:--------|
| Token-level | Maximum hit rate | More bookkeeping | SGLang RadixAttention |
| Block-level (`block_size`) | Simpler, aligns with paged layout | Misses partial-block overlap | vLLM APC, TRT-LLM KV reuse |

## Hierarchical tiers (HiCache / offload)

When GPU-resident cache isn't enough, tier outward:

```
GPU HBM (fastest, scarce)
     ↓ (evict / restore)
CPU RAM (larger, slow)
     ↓
NVMe / disk (huge, very slow)
     ↓
object store (unbounded, remote)
```

On a prefix match whose pages have been evicted to a lower tier, the scheduler either (a) fetches them back before computing attention, or (b) recomputes. Fetching is usually a win for long prefixes; recomputing is better for short ones. Implementations maintain a cost model.

## Compatibility

| Implementation | Engine | Granularity | Tiers | Notes |
|:---------------|:-------|:------------|:------|:------|
| RadixAttention | SGLang | token | GPU | default, always on |
| HiCache (radix + tiered) | SGLang | token | GPU → CPU → disk → object store | `hiradix_cache.py`, `hicache_storage.py` |
| Automatic Prefix Caching (APC) | vLLM | block | GPU | enabled by flag |
| KV cache reuse | TensorRT-LLM | block | GPU (CPU offload in progress) | |

## Engine pointers

| Engine | Radix / prefix cache | Hierarchical / offload |
|:-------|:---------------------|:----------------------|
| SGLang | `python/sglang/srt/mem_cache/radix_cache.py`, `radix_cache_cpp.py`, `cpp_radix_tree/` | `hiradix_cache.py`, `hicache_storage.py`, `memory_pool_host.py` |
| vLLM | prefix-caching logic in `vllm/v1/core/kv_cache_manager.py` + `block_pool.py` | KV offload in `vllm/v1/kv_offload/` |
| TensorRT-LLM | C++ side: `cpp/tensorrt_llm/batch_manager/` (KV cache block manager) | |

## Retention policy (priority-aware eviction)

Plain LRU treats all unreferenced blocks equally. That's wrong when some prefixes are system prompts, few-shot demonstrations, or expensive-to-recompute tool outputs that you *know* you'll re-hit. TRT-LLM's `KvCacheRetentionConfig` lets a request tag token ranges with priority 0–100 and an optional TTL, changing the eviction order:

- Blocks carry a priority in `[0, 100]` (default **35**). Eviction always drains the lowest non-empty priority tier first, LRU within the tier. Higher-priority blocks are never evicted until every lower-priority block is gone.
- `TokenRangeRetentionConfig(token_range_start, token_range_end, priority, duration_ms)` applies to **input tokens only** — e.g. "tokens 0–1024 are priority 90 for 60 s, then revert to 35".
- `decode_retention_policy` and `decode_duration_ms` cover the generated portion.
- With host offload enabled (`host_cache_size`), `secondary_offload_min_priority` (default 35) gates whether an evicted block is copied to CPU or dropped outright. Low-priority blocks skip offload and save CPU↔GPU bandwidth.

When it matters: long system prompts in a chat serving tier, RAG with hot retrieval shards, agent workflows that re-enter the same tool prompt. Give those blocks ≥ 50 priority; everything else keeps the default of 35.

## Multimodal cache keying

Image / audio tokens look like placeholder IDs but carry modality-specific KV state. Match two policies:

- **Content hash (default).** Hash the bytes of the image/audio and use that as the cache key for the corresponding token span. Stable across sessions; requires re-hashing on every request.
- **Caller-supplied UUID.** Pass `multi_modal_uuids` alongside `multi_modal_data`. TRT-LLM then keys on `BLAKE3(UUID ‖ content)` — combining both prevents cache collisions when two callers happen to pick the same UUID string, while still letting the caller reuse a stable ID across sessions. The UUID (not the content hash) is what appears in KV cache events, so external cache directories stay human-readable.

Without UUIDs, there is no cross-session hit because each new request re-hashes the content with a different salt; with UUIDs, there is — at the cost of trusting the caller not to reuse a UUID on *different* content (which the `‖ content` defends against).

## Interaction with other features

- **Speculative decoding**: verify rewinds can invalidate a suffix of the committed prefix; the cache must handle "un-commit" correctly, or be conservative about what's shared.
- **Tool calling / branching**: each branch shares the prompt prefix; radix sharing is exactly the right model.
- **Multi-turn conversations**: each turn shares the history; prompt caching matters more as conversations grow.
- **Quantized KV**: cache must track the quant scheme as part of the key, else a reuse across schemes silently produces wrong outputs.
- **Sampling**: output tokens are per-request; only the prefix KV is shared.

## Pitfalls

- **Cache invalidation boundaries.** A generated token is not shareable until committed. Don't cache output KV prematurely.
- **Tokenization drift.** Two identical-looking prompts with different BOS / chat-template can tokenize differently — match by token ID, not by string.
- **Reference leaks.** A crashed worker that held references will pin pages forever unless the scheduler has a reap path on connection loss.
- **Multi-modal tokens.** Image / audio placeholder tokens often hash to the same token ID but carry different cross-attention state; either exclude them from the key or include a content hash.
- **Prefix-cache contamination in benchmarks.** Running the same prompts back-to-back reads from cache, over-reporting TTFT by an order of magnitude. See `tooling/serving-benchmark/`.

## See also

- `algorithms/paged-attention/` — the block pool the radix tree points into
- `algorithms/heterogeneous-kv-cache/` — extending radix-style matching across mixed layer types (full attn + SWA + SSM)
- `engines/sglang/` — source of RadixAttention / HiCache
- `tooling/serving-benchmark/` — how to measure with prefix caching honestly
