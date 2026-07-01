# NxD KV cache (device-resident KV on Trainium)

How to get a **device-resident, in-place KV cache** on a NeuronCore using
**NxD Inference** (`neuronx_distributed_inference`). This is the single biggest
serving optimization on Trainium, and it's the one thing a from-scratch
`torch_neuronx.trace` decode **cannot** do.

> Read [`neuron-pytorch.md`](neuron-pytorch.md) first (why everything compiles to
> static graphs) and [`nxd-inference.md`](nxd-inference.md) (what NxD is). For the
> serving algorithm, [`../algorithms/paged-attention.md`](../algorithms/paged-attention.md).

## The problem this solves

`torch_neuronx.trace` graphs are **stateless pure functions**: a decode graph
takes the KV cache as an input and returns the updated cache as an output, and
the runtime copies those tensors **host↔device every token**. For an 8B model
that's the dominant cost — the NeuronCore ends up ~99% idle while Python
marshals the whole cache (and `cat`/splits it across a batch) each step. Raw
aliasing on `torch_neuronx.trace` does **not** cleanly fix this (it either fails
at runtime or doesn't persist across calls — verified empirically).

NxD's `KVCacheManager` fixes it by holding the cache as **resident model state**
and letting **`ModelBuilder`** alias it in place, so the cache **never leaves the
device** between steps.

## `KVCacheManager` — storage

```python
from neuronx_distributed_inference.modules.kvcache.kv_cache_manager import KVCacheManager
kv = KVCacheManager(config, num_kv_head=NUM_KV_HEADS)
```

It allocates the cache as an `nn.ParameterList` of **resident buffers**, two per
layer (K and V):

```
kv.past_key_values[2*layer + 0]  # K, shape (kv_cache_batch_size, num_kv_head, max_len, head_dim)
kv.past_key_values[2*layer + 1]  # V, same shape
```

These are `nn.Parameter(requires_grad=False)` — i.e. **model state**. That's the
whole trick: because they live inside the traced module, `ModelBuilder` can alias
them input→output and keep them resident.

Config it reads (build a `NeuronConfig` + `InferenceConfig`):

| Field | On | Meaning |
|:------|:---|:--------|
| `kv_cache_batch_size`, `kv_cache_padding_size` | `NeuronConfig` | batch rows of the cache |
| `max_length`, `max_context_length` | `NeuronConfig` | `max_context_length <= max_length` (asserted) |
| `torch_dtype`, `padding_side` | `NeuronConfig` | BF16, "right" |
| `num_hidden_layers`, `num_attention_heads`, `num_key_value_heads`, `hidden_size`, `head_dim`, `num_cores_per_group` | `InferenceConfig` | model shape |

## API — read / write

```python
# READ (in attention): the cached K/V up to seq_len
k, v = kv.get_cache(seq_len, seq_ids=seq_ids)            # returns list of (K, V) per layer

# WRITE: scatter the freshly-computed K/V into the resident cache, in place
kv.update_cache(
    is_for_context_encoding=True_for_prefill_else_False,
    seq_ids=seq_ids,            # which sequences -> which cache rows
    position_ids=position_ids,  # (batch, bucket): where in max_len to write
    new_key_values=new_kv,      # list of (K, V) from this forward pass
    seq_len=max_len,
    scatter_index=None,         # optional explicit slot index
)

# continuous-batching slot allocation: active seq ids -> cache batch rows
idx = kv.get_cache_update_index_for_seq_ids(seq_ids)
# per-layer: get_kv_by_layer_id(...) / update_kv_by_layer_id(...)
```

- **`is_for_context_encoding=True`** = prefill (write the whole prompt);
  `False` = decode (write one token). Same resident cache for both.
- **`seq_ids`** is how continuous batching addresses per-sequence cache rows — so
  different requests occupy different *rows* of the one resident buffer. **No
  host-side `torch.cat`/split per token** (the classic from-scratch slowdown).
- The scatter at `position_ids`/`scatter_index` is a **static-shape in-place
  write** — NxD compiles it; you don't fight Neuron's no-dynamic-indexing rule.

## CRITICAL: residency comes from `ModelBuilder`, not from `update_cache`

**`update_cache` is *functional*** — it returns the updated K/V; calling it in
plain eager mode does **not** write back into `past_key_values` (verified: the
buffer stays zeros). The in-place persistence is wired when you **trace through
`ModelBuilder`** with an alias map:

```python
from neuronx_distributed.trace.model_builder import ModelBuilder, BaseModelInstance

# Your traced module returns (logits, *updated_kv). input_output_aliases maps each
# updated-KV OUTPUT index back onto its past_key_values INPUT, so the runtime keeps
# the buffer resident and updates it in place.
instance = BaseModelInstance(module_cls=build_decode_module, input_output_aliases=aliases)
builder = ModelBuilder(...)
builder.add(key="decode", model_instance=instance, example_inputs=example)
neuron_model = builder.trace(...)
```

So the pattern is: **model state = the KV `nn.Parameter`s; forward returns the
updated KV; `ModelBuilder` aliases output→state.** A from-scratch decode that
just calls `update_cache` and re-passes the cache will *not* be resident — you
must use the `ModelBuilder` + `input_output_aliases` path (or reimplement that
aliasing yourself, which is the NKI route — see the `neuron-nki-*` skills).

## Usage sketch (bespoke model, NxD cache)

```python
class Attention(nn.Module):          # YOUR explicit attention (own QKV/RoPE/GQA)
    def forward(self, x, kv: KVCacheManager, seq_ids, position_ids, is_prefill):
        q, k, v = self.qkv(x)        # your projections + RoPE
        kv.update_cache(is_prefill, seq_ids, position_ids, [(k, v)], self.max_len)
        K, V = kv.get_cache(self.max_len, seq_ids=seq_ids)
        return self.attn(q, K, V)    # your attention math over the cached K/V
```
Build the model with `ModelBuilder` so the cache aliases in place; compile static
prompt/decode buckets (see [`neuron-pytorch.md`](neuron-pytorch.md)).

## Variants

| Class | Use |
|:------|:----|
| `KVCacheManager` | base contiguous cache |
| `BlockKVCacheManager` | **paged / block** KV cache (non-contiguous blocks, vLLM-style) |
| `DataParallelKVCacheManager` | data-parallel across cores |
| `MultimodalKVCacheManager`, `GptOssKVCacheManager` | multimodal / model-specific |

Plus built-in **KV quantization** (`kv_quant_config`), **cache tiling**
(`tile_cache`/`untile_cache`, a partition-layout optimization), and
**sliding-window** support.

## Pitfalls

- **Eager ≠ resident.** `update_cache` alone won't persist — you must trace
  through `ModelBuilder` with `input_output_aliases`. (This is the #1 surprise.)
- **Needs a Neuron device.** `update_cache` lowers XLA ops; off-device it errors
  with `num_devices > 0`.
- **`max_context_length <= max_length`** is asserted in `NeuronConfig`.
- Keep prompt/decode **shapes static and bucketed** — the cache `max_len` is
  fixed at construction.

## Related

- [`nxd-inference.md`](nxd-inference.md) — NxD overview.
- [`neuron-pytorch.md`](neuron-pytorch.md) — why static graphs / compile cache.
- The bundled **`neuron-nki-*`** skills — for writing the resident-cache /
  attention as a custom NKI kernel if you go fully from-scratch.
