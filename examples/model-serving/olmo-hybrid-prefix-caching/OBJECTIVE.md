# Objective — Olmo-Hybrid-7B prefix-caching workload

Maximize **aggregate output token throughput (tok/s)** on a single **NVIDIA L4** GPU for a long-shared-prefix batch workload, while keeping accuracy within the accuracy checker's tolerance. Build an OpenAI-compatible `/v1/completions` server.

Target model: `allenai/Olmo-Hybrid-7B` — a hybrid attention model that interleaves linear-attention layers with full-attention layers (3 linear : 1 full, 32 layers total). KV state is therefore heterogeneous: full-attention layers hold a paged KV cache; linear-attention layers hold a recurrent state.

## Workload (this is the only workload that matters)

The benchmark sends **20 concurrent requests** that share an **identical 32 768-token prefix**. Each request adds:
- a unique 128-token tail (so the divergent suffix is short),
- 128 generated tokens at temperature 0 with `ignore_eos`.

The shared prefix is **always 32 k tokens, always identical across all 20 requests, always identical across benchmark invocations** (deterministic shared seed). Optimize for *this* shape — not for arbitrary serving.

## What to optimize

1. **Prefix cache the shared 32 k prefix across all 20 requests.** The first
   request pays the full prefill; the other 19 should reuse cached KV / state
   for the shared portion and only prefill their 128-token unique tail.
   - For the **full-attention layers** this is standard radix / block-level
     prefix caching of the paged KV cache (see
     `references/algorithms/radix-prefix-caching.md`).
   - For the **linear-attention layers** the "KV cache" is a recurrent state.
     Prefix sharing means snapshotting that state at the end of the shared
     prefix and forking it per-request. See
     `references/algorithms/heterogeneous-kv-cache.md` for how production
     engines (SGLang, vLLM) handle hybrid prefix caching across attention
     and SSM/linear layers.
   - **Sharing must be GPU-side zero-copy, not host-side Python aliasing.**
     Populate the prefix into one paged KV pool *once* at prefill, then
     express sharing via FlashInfer `plan()`'s `kv_indices` — every
     request's indices reference the same physical prefix page IDs plus its
     own tail pages. Do not refill a scratch with `kv_cache[page_idx].copy_(...)`
     each call; that's the anti-pattern that costs ~half of decode CUDA
     time. (`BatchDecodeWithSharedPrefixPagedKVCacheWrapper` is an
     equivalent alternative that takes shared and unique K/V separately.)
2. **Continuous batching of the 20 concurrent decode streams.** After
   prefill, you have 20 streams generating 128 tokens each at temperature 0;
   they should decode together in one continuous batch, not serially.
3. **Fused attention kernel + CUDA graphs** for the decode loop. The decode
   step on a 32 k-context cache is bandwidth-bound; per-kernel launch overhead
   matters at batch=20.
4. **Bench-honest measurement.** The benchmark warmup populates the prefix
   cache first; the measured 20 requests should each hit the cache for the
   shared prefix. If your aggregate throughput does not improve substantially
   versus a no-cache baseline, the cache is not actually being hit — verify
   before claiming a speedup.

## Headline metric

`aggregate_throughput_tok_per_sec` printed by `benchmark/benchmark.py` as the `Primary metric:` line. This is `total_output_tokens / wall_clock` across the 20 requests after warmup. This is the only number `perf_metric` should record.

## Notes

- Text-generation, hybrid (linear-attn + full-attn) causal LM. **Single L4
  (Ada, sm_89, 24 GB) target.** 
- Implement model layers explicitly (own attention / linear-attention / MLP
  / norm / RoPE); use `transformers` only as a utility for config / tokenizer
  / weight loading. The reference implementation in `reference/reference.py`
  is the file copied from `transformers/models/olmo_hybrid/` — read it for
  correctness, then design serving on top.
- The prompt the server receives is a list of token IDs (vLLM-style
  `prompt: list[int]`), not a string — the benchmark synthesises the 32 k
  shared prefix from random IDs and sends them directly. The server must
  accept `prompt` as either `str` or `list[int]`.
