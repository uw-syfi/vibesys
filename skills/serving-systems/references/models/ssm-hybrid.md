# SSM and hybrid SSM+attention

Pure-SSM and hybrid-SSM architectures break several assumptions built into attention-only serving engines. If your engine was designed for decoder-only transformer attention, adding these requires rethinking the KV memory model.

## Why they're different

| Assumption attention-only engines make | How SSM breaks it |
|:---------------------------------------|:------------------|
| Each new token grows KV cache by O(per-token) | SSM keeps a **fixed-size state**; cache doesn't grow |
| Paged attention handles KV | SSM has no K, no V — just a small state tensor |
| Batching works by stacking attention queries | SSM is sequential in the recurrence dim; selective-scan kernels batch differently |
| Prefix caching is block-granular | SSM state at position N depends on the whole prefix 0..N — cacheable but only at *state* granularity, not block |
| Attention kernels are the hot path | SSM kernels (selective scan, chunked scan) are the hot path |

Hybrid models (Jamba, Zamba2, Nemotron-H) interleave SSM layers with standard attention layers — the cache is **per-layer**: attention layers use paged KV, SSM layers use a state tensor.

## Core structures

### SSM state (Mamba-2 style)

Per-layer per-request:

```
ssm_state:  (state_size, num_heads, head_dim)
conv_state: (d_conv - 1, num_heads * head_dim)     # small conv-1d cache
```

State dim is typically 16–128, much smaller than a KV cache entry. Total state memory is O(num_layers × state_size × hidden) per request — **independent of sequence length**.

Update mechanism: `selective_scan` (for Mamba) or `chunked_scan` (Mamba-2, throughput-oriented) advances the state by one chunk of tokens.

### Hybrid cache

Per request, per layer, store whichever applies:

- Attention layer → page list + last_page_len (standard paged KV)
- SSM layer → `ssm_state` + `conv_state` tensors

Total memory grows only in attention layers as the sequence grows.

## Example architectures

### Mamba-2 (pure SSM)

- Selective state-space model, no attention
- Chunked scan kernels for throughput
- `d_state=128` typical, `d_conv=4`, head_dim varies
- Falcon-Mamba-7B is a production-scale pure-SSM LLM
- Serving benefits: constant-memory decode, regardless of context length
- Serving challenges: no prefix-cache reuse at block granularity; weaker quality on long-retrieval benchmarks than attention baselines

### Jamba (hybrid SSM + attention + MoE)

- AI21's hybrid: alternates Mamba and attention blocks
- Some blocks add MoE experts inside the MLP
- Three axes of serving complexity in one model — SSM cache + paged KV + expert parallelism
- Serving support is rarer than pure text-moe or pure text-dense

### Zamba 2 (hybrid with shared attention)

- Zamba2-7B / 2B: Mamba blocks + periodic attention blocks
- Attention weights **shared across attention blocks** (one set of QKV/O weights used at multiple positions in the stack)
- Shared weights complicate weight loading and some parallelism schemes

### Nemotron-H (NVIDIA hybrid)

- Hybrid attention + Mamba-2, with attention blocks concentrated in the deeper layers
- Nemotron-H / Nemotron-Nano / Nemotron-NAS variants — NAS explores the hybrid ratio
- Comes with MTP variants (`nemotron_h_mtp`) for speculative decoding
- Relatively well-supported across vLLM / SGLang / TRT-LLM

### Jet-Nemotron

- Follow-up hybrid from NVIDIA
- Similar architectural ideas as Nemotron-H with updated training

## Kernels — what vLLM and SGLang actually use

Two distinct Triton kernel families, both adapted from open-source upstreams, plus an optional CUDA conv-1d.

### Family A — SSD (State Space Duality, Mamba / Mamba-2)

Used for Mamba-1, Mamba-2, Jamba, Zamba2, Nemotron-H, Falcon-Mamba. Decomposes the Mamba-2 recurrence into four kernel stages:

| Stage | File (both engines) | Role |
|:------|:--------------------|:-----|
| Chunk state | `ssd_chunk_state.py` | compute per-chunk final state from inputs |
| Intra-chunk BMM | `ssd_bmm.py` | batched matmul within a chunk |
| Chunk scan | `ssd_chunk_scan.py` | output contribution from chunk inputs + initial state |
| State passing | `ssd_state_passing.py` | propagate the final state across chunks |
| Combined | `ssd_combined.py` | orchestrates the four stages end-to-end |
| Mamba-1 kernel | `mamba_ssm.py` | older selective-scan for Mamba-1 |
| Dispatcher | `ssu_dispatch.py` | picks the right kernel per phase (prefill / decode / chunked) |

- **vLLM location**: `vllm/model_executor/layers/mamba/ops/`. Callsites in `vllm/model_executor/layers/mamba/{mamba_mixer,mamba_mixer2}.py`.
- **SGLang location**: `python/sglang/srt/layers/attention/mamba/ops/`. Callsites in `python/sglang/srt/layers/attention/mamba/mamba.py`.
- **Upstream**: both adapted from Tri Dao / Albert Gu's `state-spaces/mamba` v2.2.x; file headers in both engines cite `https://github.com/state-spaces/mamba/.../ssd_*.py` explicitly.

### Family B — FLA (Flash Linear Attention, DeltaNet / GDN)

Used for Qwen3-Next GDN blocks, DeltaNet, generic linear attention, Kimi Delta Attention (KDA) variants. Different mathematical structure from SSD — this is the linear-attention family with `Q(K^T V)` reassociation + delta update.

| File (both engines) | Role |
|:--------------------|:-----|
| `chunk.py`, `chunk_o.py`, `chunk_delta_h.py`, `chunk_scaled_dot_kkt.py` | chunkwise forward: output, delta-hidden, `K^T K` scaled |
| `fused_recurrent.py` | recurrent (single-token) path for decode |
| `fused_sigmoid_gating{,_recurrent}.py` | fused sigmoid gate |
| `cumsum.py`, `l2norm.py`, `layernorm_gated.py` / `layernorm_guard.py` | helpers |
| `solve_tril.py`, `wy_fast.py` | low-triangular solve / Woodbury helpers |
| `kda.py` | Kimi Delta Attention variant |
| **vLLM** adds `fused_gdn_prefill_post_conv.py` | GDN-specific fused kernel replacing `split → rearrange → contiguous×3 → l2norm×2 → gating` with one Triton launch |
| **SGLang** adds `chunk_fwd.py`, `chunk_intra.py`, `chunk_intra_token_parallel.py`, `fused_gdn_gating.py`, `fused_norm_gate.py` | additional parallelization / fusion variants |

- **vLLM location**: `vllm/model_executor/layers/fla/ops/`. Callsites in `vllm/model_executor/layers/fla/{gdn_linear_attn,linear_attn,short_conv}.py`.
- **SGLang location**: `python/sglang/srt/layers/attention/fla/`. Callsites via the attention-backend registry.
- **Upstream**: both adapted from [`fla-org/flash-linear-attention`](https://github.com/fla-org/flash-linear-attention); each engine adds its own fused-kernel variants on top.

### Conv-1D (pre-SSM / pre-linear-attention projection)

A causal 1D convolution sits in front of every Mamba block and some GDN blocks. Cache: the last `d_conv - 1` tokens per request.

- **CUDA path** (preferred): [`Dao-AILab/causal-conv1d`](https://github.com/Dao-AILab/causal-conv1d) — hand-written CUDA kernel. SGLang wraps it as `python/sglang/srt/layers/attention/mamba/causal_conv1d.py`; vLLM's `vllm/model_executor/layers/mamba/ops/causal_conv1d.py` is the same wrapper.
- **Triton fallback**: SGLang ships `causal_conv1d_triton.py` for environments without the CUDA build.

### Summary — which model uses which

| Model | Kernel family | File path hint |
|:------|:--------------|:---------------|
| Mamba-1 | SSD / `mamba_ssm.py` (legacy selective scan) | `mamba/ops/mamba_ssm.py` |
| Mamba-2, Jamba, Zamba2, Nemotron-H, Falcon-Mamba | SSD / `ssd_*.py` chunked decomposition | `mamba/ops/ssd_*.py` |
| Qwen3-Next (GDN blocks) | FLA + vLLM's `fused_gdn_prefill_post_conv.py` | `fla/ops/*` |
| DeltaNet / RWKV-linear / KDA | FLA | `fla/ops/*` |

## Pitfalls

- **Assuming KV-only cache.** An engine built on paged KV breaks on the first SSM layer — the memory manager must handle SSM state too. Plan upfront.
- **Scheduler token budget and chunk size.** SSM chunked-scan has a fixed chunk size; mixing with chunked prefill requires aligning the two budgets.
- **Conv-1d initialization.** Mamba-2 has a conv-1d before the SSM; its "cache" is the last `d_conv - 1` tokens. Easy to forget — accuracy regresses silently.
- **Speculative rollback.** On a rejected draft, SSM state must roll back to pre-draft. Use a snapshot-then-maybe-commit pattern.
- **Radix sharing across SSM states.** Two prompts sharing a prefix converge to the same state only if the kernel is deterministic. Floating-point order in chunked scan can break this — compare at hashed-state granularity, not byte-level.
- **TP on SSM layers.** TP for SSM requires the state tensor to shard correctly; most implementations instead replicate state and shard the projection matrices.
- **Zamba-style shared weights.** Engines loading the same weights into multiple positions must handle aliasing — memory is O(layers × shared_weights) not O(total_positions × weights).
- **Continuous batching with dropped requests.** SSM state slot reclamation must be explicit; the slot is not freed by a request simply finishing — the scheduler must release it.

## See also

- [`algorithms/attention-variants/`](../algorithms/attention-variants.md) — SSM in context with quadratic attention and linear-attention cousins (RetNet, RWKV, DeltaNet)
- [`algorithms/continuous-batching/`](../algorithms/continuous-batching.md) — SSM state batching differs from KV
- [`algorithms/radix-prefix-caching/`](../algorithms/radix-prefix-caching.md) — SGLang's `mamba_radix_cache` extends radix to SSM states
- [`algorithms/heterogeneous-kv-cache/`](../algorithms/heterogeneous-kv-cache.md) — allocator and prefix-cache design for mixed layer types (Jenga)
- [`algorithms/disaggregated-serving/`](../algorithms/disaggregated-serving.md) — state transfer vs KV transfer
- [`algorithms/speculative-decoding/`](../algorithms/speculative-decoding.md) — rollback considerations
- [`models/text-dense/`](text-dense.md), [`models/text-moe/`](text-moe.md) — attention-only counterparts
