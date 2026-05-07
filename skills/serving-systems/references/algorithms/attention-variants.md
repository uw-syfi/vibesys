# Attention variants

The attention operator has splintered into a zoo of variants. Most choices fall on three mostly-independent axes. Knowing where a given model sits lets you pick the right backend, predict KV memory, and spot the common pitfalls.

## The three axes

```
head sharing  ×  masking pattern  ×  complexity class
  MHA                causal               O(L²)
  MQA                bidirectional        O(L) (SSM / linear / RetNet / RWKV)
  GQA                SWA                  hybrid (mixed per-layer)
  MLA                local+global alt
                     cross-attention
                     3D / spatio-temporal
                     tree (spec. decode)
```

Plus the cross-cutting **self vs. cross** distinction (where K/V come from).

## Axis 1 — head sharing (KV cache size)

All of these share the same attention kernel family; they only differ in the ratio of query heads to key/value heads.

| Variant | Ratio | KV cache size vs MHA | Quality | Used by |
|:--------|:------|:---------------------|:--------|:--------|
| **MHA** (multi-head) | `H_q = H_kv` | 1× (baseline) | best | Llama-2, Mixtral older, Whisper |
| **MQA** (multi-query) | `H_kv = 1` | `1/H_q` | small drop | older PaLM; rare today |
| **GQA** (grouped-query) | `H_q = g · H_kv`, g ∈ {4, 7, 8} | `1/g` | near-MHA | **Llama-3+, Mistral, Qwen, Gemma, Mixtral, most modern** |
| **MLA** (multi-head latent) | compressed KV latent (~512 dim) + small decoupled-RoPE head dim | **5–15× smaller than MHA** | best or near-best | DeepSeek V2 / V3 / R1 / V3.1 |

MLA is qualitatively different from MHA/MQA/GQA — instead of sharing KV across heads, it projects KV into a low-rank latent space and decompresses on the fly at each layer. Decode typically uses the **query-absorption trick** to compute attention directly on the compressed KV, avoiding the decompression on the decode hot path.

### Kernel constraint

For MHA/MQA/GQA: `H_q % H_kv == 0` is enforced by FlashAttention and FlashInfer. Llama-3 8B has 32/8 (g=4); Qwen3 8B has 28/4 (g=7). MLA does not use this relation — pick an MLA-specific backend.

## Axis 2 — masking pattern (who attends to whom)

### Self-attention masking

| Pattern | Description | Where |
|:--------|:------------|:------|
| **Causal** | token `t` attends to `[0..t]` | every decoder-only LLM |
| **Bidirectional** | every position attends to every other | encoder sides; MMDiT transformer blocks (SD3) |
| **Sliding window (SWA)** | causal within a window `W` | Mistral-7B (W=4096), Gemma-2 alternating |
| **Local + global alternating** | every N-th layer is full causal; others are SWA | Gemma-2, some long-context models |
| **Block-sparse / dilated** | sparse pattern (Longformer, BigBird) | rare in production serving |
| **3D spatio-temporal** | full or window attention across `(T, H, W)` tokens | video diffusion transformers (CogVideoX, HunyuanVideo) |
| **Tree** | each candidate attends only to its ancestors in a tree | speculative decoding's verify step |

### Cross-attention

Q from sequence A, K/V from sequence B (fixed once B is produced):

- **Whisper decoder**: Q from partial transcript; K/V from encoder hidden states (one forward, then frozen).
- **mllama (Llama-3.2 Vision)**: Q from text; K/V from image features. Cross-attention layers are interleaved with self-attention in the decoder stack.
- **Flamingo-style**: same pattern.

Cross-attention **should cache K/V once per encoder pass** — computing them every decode step is a common silent perf disaster.

### Sparse attention (token / block sparsity on long contexts)

Orthogonal to the masking list above: prune attention to a small set of "important" KV blocks per query to get sub-quadratic effective cost on long contexts. Split into **framework-level** (runtime picks indices → kernel reads them) and **kernel-level** (kernel decides on the fly).

| Algorithm | Type | Prefill | Decode | Shrinks KV? | Model-native? |
|:----------|:-----|:-------:|:------:|:-----------:|:-------------:|
| **RocketKV** (TRT-LLM `rocket`) | framework | — | yes | yes (permanent eviction + dynamic top-k) | no — drop-in |
| **DSA** (DeepSeek V3.2 sparse attn, TRT-LLM `dsa`) | framework | yes | yes | no (low-rank indexer, no eviction) | yes |
| **Skip Softmax / BLASST** (TRT-LLM `skip_softmax`) | kernel | yes | yes | no | no — drop-in |
| **Native Sparse Attention (NSA)**, **MoBA** | framework | varies | yes | varies | some model-native |

TRT-LLM exposes these via `sparse_attention_config` in the `LLM` API (`RocketSparseAttentionConfig`, `DeepSeekSparseAttentionConfig`, `SkipSoftmaxAttentionConfig`). Framework-level algorithms plug into `AttentionBackend` via `sparse_kv_predict` (which KV tokens to keep) and `sparse_attn_predict` (which KV blocks the current query should attend to) — see `$SERVE_REPOS/TensorRT-LLM/tensorrt_llm/_torch/attention_backend/sparse/{rocket,dsa,kernel,utils}.py`.

Pitfalls:

- **Sparse KV cache breaks block reuse.** If the algorithm evicts tokens (RocketKV), two requests that looked like prefix-shares are no longer safe to share — TRT-LLM requires `enable_block_reuse: false` for RocketKV. DSA (no eviction, low-rank only) keeps reuse working.
- **Prediction cost matters at low latency.** Decode-only algorithms that add a per-step top-k selection can eat a large fraction of attention time; the selection itself usually wants a custom Triton kernel.
- **Auxiliary memory pool.** Algorithms with their own side-state (RocketKV's KT cache, DSA's indexer K-cache) need a second pool. TRT-LLM offers a Python `KVCacheManager` subclass (easier) or integration into the C++ `KVCacheManager` (enables KV reuse / disagg transmission of the aux state).
- **Where sparsity applies is asymmetric.** TRT-LLM today supports sparse *KV cache* in context + sparse *computation* in generation for MQA/GQA; MLA gets token-level sparse compute in both phases; sparse compute in context and sparse KV in decode are still unsupported. Don't assume a config works everywhere.

SGLang has its own backends — `nsa_backend.py`, `tbo_backend.py` — for NSA and related algorithms.

## Axis 3 — complexity class (quadratic vs sub-quadratic)

Attention is canonically `O(L²)` in sequence length. Several families trade that for `O(L)`:

| Family | Mechanism | Serving impact |
|:-------|:----------|:---------------|
| **SSM** (Mamba / Mamba-2) | selective state-space recurrence | fixed-size state per request; no KV growth |
| **Linear attention** | kernelized `softmax(QK)V` → `Q(K^T V)` | fixed-size state; quality has historically lagged |
| **RetNet** (retention) | parallel / recurrent / chunkwise dual view | fixed-size state |
| **RWKV** | RNN with attention-like gating | fixed-size state |
| **DeltaNet / GLA** | newer linear attention families | fixed-size state |
| **Hybrid** (Jamba, Zamba2, Nemotron-H) | interleave quadratic and sub-quadratic layers | per-layer decision |

Serving these requires cache machinery beyond paged KV — see [`models/ssm-hybrid/`](../models/ssm-hybrid.md) and SGLang's `layers/attention/fla/` and `wave_ops/` for linear-attention paths.

## KV cache size — a concrete feel

For a 70B-ish model, per token, per layer:

```
KV per token per layer = 2 · H_kv · head_dim · bytes_per_elt
```

With `H_kv = 8`, `head_dim = 128`, BF16:

- MHA (H_kv = H_q = 64): ~32 KB / token / layer
- GQA 4:1 (H_kv = 8): ~4 KB / token / layer
- MLA (compressed c_kv = 512): ~1 KB / token / layer (the decompress matrices are per-layer but not per-token)
- SSM: 0 (state is fixed size independent of L)

At 80 layers and 128k context, the MHA row is ~320 GB of KV — impossible. GQA makes it ~40 GB. MLA makes it ~10 GB. SSM: 0.

This is why **head-sharing and MLA matter operationally**, not just for training efficiency.

## Per-family summary

Rough lookup for "what attention does this model use":

| Model family | Head sharing | Masking | Complexity |
|:-------------|:-------------|:--------|:-----------|
| Llama-2 | MHA | causal | O(L²) |
| Llama-3 / 3.1 / 3.2 / 3.3 / 4-dense | GQA | causal | O(L²) |
| Mistral 7B | MHA | causal + SWA (4096) | O(L²) |
| Mixtral 8×7B | GQA | causal | O(L²) |
| Gemma-2 | GQA + QK norm | **causal alternating with SWA** | O(L²) |
| Qwen2 / 2.5 / 3 dense | GQA + QK norm (Qwen3) | causal | O(L²) |
| Qwen-VL / Qwen2-VL / Qwen3-VL | GQA | causal + **M-RoPE 3D position** | O(L²) |
| Qwen3-MoE | GQA + QK norm | causal | O(L²) |
| **DeepSeek V2 / V3 / R1** | **MLA** + decoupled RoPE | causal | O(L²) |
| Phi-3 / Phi-4 | GQA (mostly) | causal (some SWA) | O(L²) |
| Whisper | MHA **self + cross** | causal self, bidirectional cross | O(L²) |
| mllama | GQA self + cross (to image) | causal self, cross | O(L²) |
| Mamba-2, Falcon-Mamba | — (pure SSM) | — | O(L) |
| Jamba, Zamba2, Nemotron-H | GQA + Mamba | causal | hybrid |
| Jet-Nemotron | hybrid | causal | hybrid |
| SD3 (MMDiT) | MHA | bidirectional (text+image joint) | O(L²) |
| CogVideoX, HunyuanVideo | DiT attention | **3D or windowed 3D** | O(L²) in tokens |

## Backend / engine support

Per-backend support (non-exhaustive; check current versions before depending):

| Variant | FlashInfer | FlashAttention | Triton attn | MLA-specific | Engine files (SGLang) |
|:--------|:-----------|:---------------|:------------|:-------------|:----------------------|
| MHA / GQA / MQA | ✓ paged | ✓ (FA2 + FA3) | ✓ | — | `flashinfer_backend.py`, `flashattention_backend.py`, `triton_backend.py`, `aiter_backend.py` |
| MLA | ✓ (`mla.*` wrappers) | — | — | **required**: FlashInfer-MLA, CUTLASS-MLA, FlashMLA | `flashinfer_mla_backend.py`, `cutlass_mla_backend.py`, `flashmla_backend.py` |
| SWA (window_size arg) | ✓ | ✓ | ✓ | — | same as MHA/GQA |
| Local+global alternating (Gemma-2) | ✓ with per-layer dispatch | ✓ | ✓ | — | engine picks per layer |
| Cross-attention | ad hoc; usually per-model | ad hoc | ad hoc | — | per-model (Whisper) |
| SSM (selective scan, chunked scan) | N/A | N/A | ✓ | N/A (Mamba kernels in `jit_kernel`) | `fla/`, Mamba-specific paths |
| Linear attention (generic) | — | — | ✓ | — | SGLang `fla/` directory |
| 3D / spatio-temporal | via custom DiT attention | via custom | ✓ | — | lives in video-gen pipelines, not LLM engines |
| NSA, TBO, wave attention | — | — | ✓ | — | `nsa_backend.py`, `tbo_backend.py`, `wave_backend.py` |
| Tree attention (speculative) | via custom mask | via custom mask | ✓ | — | see spec-decode implementations |

## Serving implications by axis

| Axis | Primary serving impact |
|:-----|:-----------------------|
| Head sharing | KV cache size (memory-bound decode speed) |
| Masking | attention compute cost, backend picked, mask construction |
| Complexity class | cache growth behavior, which kernel library applies |

Non-obvious interactions:

- **GQA + paged attention**: works trivially. Block layout uses `num_kv_heads`, not `num_q_heads`.
- **MLA + paged attention**: needs MLA-aware paged layout. Standard paged kernels don't fit.
- **SWA + long context**: attention is `O(L · W)` not `O(L²)`; memory for KV still grows with `L` unless explicitly dropped past window.
- **Cross-attention + paged attention**: self-attn uses paged; cross-attn K/V is small and usually dense.
- **3D attention + memory**: video DiT activations scale with `T · H · W`; even window 3D can be tens of GB per forward step.
- **Hybrid models + paged KV**: manage two cache types per request, only attention layers use paged.

## Pitfalls

- **Wrong H_q / H_kv ratio.** FA / FlashInfer enforce `H_q % H_kv == 0`. Silent crash or kernel-not-applicable error.
- **MLA on a non-MLA backend.** Silent OOM (allocates dense KV for 256-ish heads) or wrong semantics.
- **Forgetting SWA.** Mistral-7B-v0.1 requires `window_size`; engines that default to full causal silently produce worse outputs past the window.
- **Gemma-2 alternating pattern.** A single attention-backend call per layer isn't enough — per-layer choice between global and local matters.
- **Cross-attn KV recomputed each step.** Must be computed once per encoder pass (per-layer K_enc / V_enc projections of the shared encoder output) and cached across all decode steps.
- **Mixing variants in a "standard" engine.** An engine written for GQA-causal won't handle mllama's cross-attention layers without explicit support.
- **Linear-attention quality assumptions.** Pure linear attention historically trails MHA on retrieval tasks; hybrids (Jamba, Nemotron-H) mitigate but don't close the gap entirely.
- **3D attention memory.** The quadratic cost in `T·H·W` is real — for video DiTs, expect memory to dominate all other serving concerns.
- **Tree attention mask errors.** In speculative decoding, candidates must attend to ancestors only; siblings attending to each other is a silent correctness bug.
- **QK-norm absence.** Qwen3 / Gemma-2 apply RMSNorm to Q and K post-projection; omitting it silently degrades precision.
- **Cache sharing across variants.** Two requests to the same model with different quantized-KV schemes must not share cached KV.

## Out of scope — kernel implementation

How these attention variants are implemented at the kernel level (FA3 WGMMA pipeline, FlashInfer MLA kernel design, Mamba selective-scan chunked kernel) lives in `agent-gpu-skills` (`cuda-skill`, `triton-skill`, `cutlass-skill`).

## See also

- [`algorithms/paged-attention/`](paged-attention.md) — orthogonal: how KV is stored (block-based, non-contiguous)
- [`backends/flashattention/`](../backends/flashattention.md), [`backends/flashinfer/`](../backends/flashinfer.md) — kernels that implement these variants
- [`models/text-dense/`](../models/text-dense.md), [`models/text-moe/`](../models/text-moe.md), [`models/ssm-hybrid/`](../models/ssm-hybrid.md), [`models/vision-language/`](../models/vision-language.md), [`models/speech-language/`](../models/speech-language.md), [`models/video-generation/`](../models/video-generation.md) — per-family variants in practice
- [`algorithms/speculative-decoding/`](speculative-decoding.md) — tree attention for verification
- [`algorithms/quantization-schemes/`](quantization-schemes.md) — KV quantization interacts with head-sharing
