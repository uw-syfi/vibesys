# Cross-attention KV cache (encoder-decoder decode)

The problem this solves: in an encoder-decoder model (Whisper, encoder-decoder ASR/translation, mllama-style interleaved cross-attention), each decoder layer cross-attends to the **encoder context**. Those context keys/values depend only on the encoder output — they are the same for every decode step of a request. Reprojecting `cross_k = W_k · enc`, `cross_v = W_v · enc` on every step re-encodes the whole context per token: a silent perf disaster that dominates decode cost (`attention-variants.md` and `speech-language.md` both flag it). This note is the **implementation recipe** those two only gesture at: how to build the cross-attention cache so it is computed once and read every step, and how to make it live in the same paged / FlashInfer machinery as self-attention so continuous batching and CUDA graphs still apply.

## What makes cross-attention KV different from self-attention KV

| | self-attention KV | cross-attention (context) KV |
|---|---|---|
| source | decoder tokens (grows every step) | encoder output (fixed after prefill) |
| write cadence | append 1 slot/step | write once, at request start |
| masking | causal | **non-causal** (every query sees the whole context) |
| head config | decoder's `num_kv_heads` / `head_dim` | may differ (a second encoder can have its own) |
| RoPE | as the decoder uses | **none** — context positions are baked in at encode time |

The two are different enough that a cache manager written only for the growing causal self-attention case will not handle cross-attention without explicit support (a common failure when bolting an encoder-decoder model onto a decoder-only engine). But they are close enough that **the same paged-KV pool + FlashInfer prefill wrapper serve both** — the only real difference is "written once, never rewritten" and `causal=False`.

## Design: a separate context pool per source

Keep the decoder's self-attention KV cache exactly as-is (paged, causal, grows per step — see [`paged-attention.md`](paged-attention.md)). Add, alongside it, one **cross-attention context pool** per encoder source:

- Its own page geometry (`num_kv_heads`, `head_dim`, `page_size`, `max_context_len`, page budget) because the context head config can differ from the decoder's.
- Multiple sources (e.g. text encoder + audio encoder feeding different cross-attn blocks) → one pool each; **pools whose head config is identical can share physical memory**, and the shared pool's page budget is the *sum* of the sharers' budgets (two same-geometry sources at 256 pages each → one 512-page pool). Dedup by the config-minus-page-count, not by source name.
- Route its plan/run state through the *same* per-label machinery as self-attention under a derived label like `{base_label}::CROSS_ATTN::{source}`, so nothing about scheduling, batching, or graph capture needs a new code path.

## The three operations

Give the cache manager three methods (names illustrative). They mirror `plan_attention` / `run_attention` but split the write out because the context is written once:

1. **`add_cross_attn_kv(request_ids, k, v, layer_idx, source)`** — called once per request per layer at **prefill/encode time**. Projects and writes the encoder context K/V into the source's pool; allocate pages on the first layer's write and reuse for the rest. This is the *only* place context KV is written.

2. **`plan_cross_attention(q_seq_lens, source)`** — called each step, in `preprocess` (outside any captured region). Builds the FlashInfer prefill plan from the decoder query lengths (`qo_indptr`) and the **fixed** context pages (`paged_kv_indptr` / `paged_kv_indices`), with **`causal=False`**. Because the context pages never change after encode, this plan is memoizable: fingerprint `(q_seq_lens, context_pages, dtype)` and skip the re-plan when it is unchanged across steps (effective wherever plan state persists — e.g. the CUDA-graph persistent-wrapper path).

3. **`run_cross_attn(q, layer_idx, source)`** — called in the layer forward. Runs the pre-planned wrapper against the pool. Unlike `run_attention` it **must not write KV** (`set_kv_cache` is skipped — the context is already there). Returns `(num_query_tokens, num_qo_heads, head_dim)`.

RoPE: cross labels **never plan or apply RoPE**. If the plan/apply are label-keyed (they should be), this is automatic — just don't call them for the cross label. Context positional information, if any, is already in the encoder output.

## Why non-causal prefill is the right primitive

A single decode query token attending to the full fixed context is exactly a **1-query, N-key non-causal attention** — i.e. a prefill with `causal=False` where the "prefill" is one query row and the KV is the paged context. So the existing `FlashInferPrefillWrapper` (see [`../backends/flashinfer.md`](../backends/flashinfer.md)) handles it directly: independent `qo_indptr` (decoder queries) and `paged_kv_indptr`/`indices` (encoder pages), `causal=False`. You reuse the persistent-wrapper / static-buffer / pre-plan machinery for free — **CUDA-graph compatibility comes along with it** (see [`../backends/cuda-graph.md`](../backends/cuda-graph.md)), because capture only sees the run, and the run is a plain planned wrapper call.

## Prefill-vs-decode

- **Prefill** (the forced decoder prompt, e.g. Whisper's `<|startoftranscript|><|en|><|transcribe|><|notimestamps|>`): write the context once via `add_cross_attn_kv` for every layer, then `plan_cross_attention` with the prompt length as `q_seq_lens`.
- **Decode** (1 token/step): no new context write; `plan_cross_attention` with `q_seq_lens = [1, 1, …]`. The plan memo above makes this a near-no-op when the batch is unchanged.

## Continuous batching

Cross-attention batches the same way self-attention does: `qo_indptr` concatenates the per-request decoder queries, `paged_kv_indptr` concatenates each request's context pages. Requests with different context lengths coexist in one wrapper call — the per-request `paged_kv_last_page_len` handles the ragged tail. So a continuous-batching scheduler ([`continuous-batching.md`](continuous-batching.md)) needs no cross-attention-specific logic beyond calling the three methods per active batch.

## Pitfalls

- **Reprojecting context KV every step.** The headline mistake. Symptom: decode throughput scales with context length as if re-encoding. Fix: `add_cross_attn_kv` once, `run_cross_attn` (no write) thereafter.
- **Writing KV inside `run_cross_attn`.** Copy-pasting `run_attention` drags in `set_kv_cache`, which overwrites the context with the decoder's query projection. The cross path must *only read*.
- **Applying decoder RoPE to cross Q/K.** Rotates the query against un-rotated context keys → garbage. Cross labels skip RoPE entirely.
- **One pool forced to the decoder's head config.** If the encoder's `num_kv_heads`/`head_dim` differ (or a second encoder differs), a single shared-geometry pool silently mis-shapes the K/V. Give each distinct config its own pool.
- **Re-planning cross-attention every decode step.** The context is immutable; without the plan memo you pay the FlashInfer plan (~hundreds of µs of CPU) per step per source, which caps multi-request decode throughput. Fingerprint and skip.

## Compatibility

| Implementation | Engine | Backend / library | Hardware |
|:--|:--|:--|:--|
| Context pool + non-causal paged prefill (`causal=False`), plan-once reuse | encoder-decoder-capable paged engines | flashinfer prefill wrapper | NVIDIA (sm_80+) |
| Dense cross-attn K/V (small context, no paging) | ad hoc per-model | SDPA / FlashAttention varlen | any |
| Interleaved cross-attn layers (text Q, image K/V) | mllama-style | per-model cross-attn support | NVIDIA |

The paged-context-pool row is the reusable one for large or many-source contexts; the dense row is the pragmatic choice when a single context is small enough that paging overhead outweighs it (mllama's image cross-attn, short-context translation).

## Cross-refs

- [`../models/speech-language.md`](../models/speech-language.md) — the encoder-decoder ASR family (Whisper) this is the decode-time engine for; two KV caches per layer.
- [`../algorithms/attention-variants.md`](attention-variants.md) — where cross-attention sits among the masking-pattern variants.
- [`../algorithms/paged-attention.md`](paged-attention.md) — the self-attention paged cache the context pool sits beside.
- [`../algorithms/heterogeneous-kv-cache.md`](heterogeneous-kv-cache.md) — when multiple cache types (here: self + one-or-more context pools) share an allocator.
- [`../backends/flashinfer.md`](../backends/flashinfer.md) — the prefill wrapper (`causal=False`) that runs the context attention.

A concrete offline testbed for this recipe: the `whisper-large-v3` model-serving target (encoder-decoder ASR, self-attn KV + a single `default` cross-attention context pool).
