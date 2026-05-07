# Text-only dense decoders

**This is the foundational architecture of most modern LLMs.** Understand it first — everything else in `models/` extends or swaps components of this template:

- `text-moe/` replaces the MLP with a mixture of experts (attention, norm, RoPE unchanged)
- `vision-language/` adds a vision encoder + projector upstream (decoder unchanged)
- `ssm-hybrid/` replaces some attention layers with SSM blocks (MLP, norm often kept)
- `speech-generation/` (AR family) runs this architecture over neural-codec tokens instead of text

The template itself: pre-RMSNorm + attention + SwiGLU MLP + rotary positions + tied or untied `lm_head`, repeated `L` times. Nearly every text LLM released since 2023 outside the MoE and SSM families follows it with small variations.

## Architecture at a glance

| Component | Typical choice | Variants |
|:----------|:---------------|:---------|
| Norm | **RMSNorm**, pre-norm placement | (LayerNorm in older models) |
| Attention | **GQA** (grouped-query) | MHA (Llama-2), MQA (rare); see [`algorithms/attention-variants/`](../algorithms/attention-variants.md) |
| Positional | **Rotary** (RoPE) | many scaling variants — see below |
| MLP | **SwiGLU**: `down(silu(gate(x)) * up(x))` | GeLU in older / Phi-3 |
| Head | untied `lm_head` (Llama / Qwen3) | tied in Gemma, some Phi |
| QK norm | absent in Llama | present in Qwen3, Gemma-2 |

## Example architectures

### Llama-3.1 8B (canonical)

- 32 layers, hidden 4096, 32 Q heads / 8 KV heads (GQA 4:1), head_dim 128
- RoPE `rope_theta=500000`, **Llama-3 scaled**: `rope_type="llama3"`, `factor=8`, `low_freq_factor=1`, `high_freq_factor=4`, `original_max_position_embeddings=8192` — enables 128k context
- SwiGLU, intermediate 14336
- RMSNorm, untied lm_head
- Tokenizer: tiktoken-style BPE, 128k vocab, `<|begin_of_text|>` BOS

### Mistral 7B v0.1 (MHA + sliding window)

- 32 layers, hidden 4096, 32 MHA heads
- Sliding-window attention (window=4096)
- RoPE with default scaling (no Llama-3 scaling)
- Serving engines must honor the window mask — attention-backend selection matters

### Qwen3 8B dense

- 32 layers, hidden 4096, 28 Q heads / 4 KV heads (GQA 7:1), head_dim 128
- **QK norm**: extra RMSNorm on Q and K post-projection (absent in Llama)
- RoPE default scaling
- Untied lm_head, SwiGLU
- Chat template: ChatML-based

### Gemma-2 9B (atypical)

- GQA + QK norm
- Alternating global and sliding-window attention layers
- RMSNorm **before and after** each sublayer (not just pre-norm)
- Tied lm_head, SwiGLU (GELU gate)

### Phi-3 / Phi-4 small

- Smaller family (3–14B parameters), often GQA
- Some Phi variants use **GELU** instead of SwiGLU
- Different chat-template conventions

## RoPE scaling variants

| Variant | `rope_type` | Key parameters | Models |
|:--------|:-----------|:--------------|:-------|
| Default | `"default"` or absent | `rope_theta` | Llama-2, Mistral |
| **Llama-3 scaled** | `"llama3"` | `factor`, `low_freq_factor`, `high_freq_factor`, `original_max_position_embeddings` | Llama-3.1+, Llama-4 |
| Linear | `"linear"` | `factor` | older long-context fine-tunes |
| Dynamic NTK | `"dynamic"` | `factor` | various |
| YaRN | `"yarn"` | multiple | Qwen long-context |

Llama-3 scaling is a piecewise-linear interpolation over the frequency spectrum — low-frequency dims scaled more. **Do not reuse Llama-2 RoPE on a Llama-3.1+ model** — it silently breaks past ~8k tokens.

## Weight-key conventions (HuggingFace → engine)

Canonical HF layout:

```
model.embed_tokens.weight
model.layers.<i>.self_attn.{q_proj,k_proj,v_proj,o_proj}.weight
model.layers.<i>.self_attn.{q_norm,k_norm}.weight          # QK-norm variants only
model.layers.<i>.mlp.{gate_proj,up_proj,down_proj}.weight
model.layers.<i>.input_layernorm.weight
model.layers.<i>.post_attention_layernorm.weight
model.norm.weight
lm_head.weight                                              # absent if tied
```

Engines usually fuse Q/K/V and gate/up into single linear layers; the loader concatenates three HF tensors into one engine tensor. Check `config.tie_word_embeddings` for lm_head sharing.

## Pitfalls

- **Assuming tied lm_head.** Llama-2/3 have untied, Gemma has tied — check `config.tie_word_embeddings`.
- **Llama-3.1+ without scaled RoPE.** Works up to ~8k; produces garbage past. Silent.
- **Missing QK norm on Qwen3 / Gemma-2.** Silent precision degradation. The weight-key map must handle both presence and absence.
- **Sliding window on Mistral derivatives.** Llama doesn't use sliding window; Mistral-7B-v0.1 does (4096). Engines must honor `config.sliding_window`.
- **Gemma-2 alternating attention patterns.** Global + sliding-window layers alternate — a single attention-backend call type isn't enough.
- **Llama-4 dense vs MoE.** Llama-4 has both dense and MoE variants (Scout / Maverick MoE); don't point a dense serving path at a MoE config.
## See also

- [`algorithms/attention-variants/`](../algorithms/attention-variants.md) — MHA / MQA / GQA / SWA / QK-norm catalog + backend compatibility
- [`tooling/io-handling/`](../tooling/io-handling.md) — tokenization, chat templates (per-family BOS / turn-marker conventions), BOS double-add pitfalls
- [`engines/vllm/`](../engines/vllm.md), [`engines/sglang/`](../engines/sglang.md), [`engines/trtllm/`](../engines/trtllm.md) — where each engine implements these models
- [`algorithms/*`](../../algorithms/) — every standard serving algorithm applies to this family (text-dense is the default calibration target)
- [`models/text-moe/`](text-moe.md) — the MoE variant
- [`backends/flashinfer/`](../backends/flashinfer.md), [`backends/flashattention/`](../backends/flashattention.md) — attention backends
