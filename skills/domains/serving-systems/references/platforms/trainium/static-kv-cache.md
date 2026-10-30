# Resident, in-place KV cache on Trainium

The decisive decode optimization: the KV cache lives in preallocated `neuron`
buffers and each compiled decode step writes the new keys and values **in
place**. Nothing crosses the host boundary except the step's inputs (token ids,
positions) and its output (sampled token ids).

Prerequisites: [`neuron-pytorch.md`](neuron-pytorch.md) (compile rules, buckets).

## Why it works on TorchNeuron

`torch.compile(backend="neuron")` supports regions that mutate their inputs.
A compiled step that does `k_cache.copy_(...)` updates the caller's device
buffer, and the next step reads it without a round trip. No NxD
`KVCacheManager`, no `ModelBuilder` aliasing: ordinary PyTorch.

Verified on trn2 (SDK 2.33): a compiled decode step with an in-place cache write
produced bit-identical tokens and cache contents to eager mode, compiled to
**one** graph, and ran 0.22 ms/step (0.47 ms including a per-step `.cpu()` of
the sampled tokens).

## Pattern

```python
B, KVH, MAXLEN, D = slots, num_kv_heads, max_len, head_dim
k_cache = torch.zeros(L, B, KVH, MAXLEN, D, dtype=torch.bfloat16, device="neuron")
v_cache = torch.zeros_like(k_cache)
arange = torch.arange(MAXLEN, device="neuron")          # built once, passed in

def write_kv(cache, layer, new, pos):                   # new: [B, KVH, 1, D]; pos: [B] int32
    hit = (arange[None, :] == pos[:, None])[:, None, :, None]   # [B, 1, MAXLEN, 1]
    cache[layer].copy_(torch.where(hit, new, cache[layer]))

def decode_step(ids, pos, k_cache, v_cache, arange, weights):
    h = embed(ids)
    for layer in range(L):
        q, k, v = qkv(h, layer, pos)                    # RoPE applied at pos
        write_kv(k_cache, layer, k, pos)
        write_kv(v_cache, layer, v, pos)
        valid = arange[None, :] <= pos[:, None]          # [B, MAXLEN] per-slot extent
        h = attn_out(q, k_cache[layer], v_cache[layer], valid, layer) + mlp(h, layer)
    return torch.argmax(lm_head(h)[:, -1].float(), dim=-1)   # sample on device

step = torch.compile(decode_step, backend="neuron", fullgraph=True)
```

Rules that keep it fast:

- **Allocate once at startup** at `(layers, slots, kv_heads, max_len, head_dim)`
  and never reallocate. Admission and eviction are slot bookkeeping.
- **Write by mask or index, never by slicing with a Python int that varies.**
  `cache[:, :, pos]` with a Python `pos` is a new graph per position;
  a `pos` **tensor** with `torch.where` or `index_copy_` is one graph. Both
  `torch.where`+`copy_` and `cache.index_copy_(seq_dim, pos, new)` mutate the
  resident buffer from a compiled region (verified); measure which is faster
  for your shapes.
- **Store GQA heads, expand transiently.** The cache holds `kv_heads`; expand
  to query heads (`repeat_interleave`) only inside attention, or use an
  attention kernel that takes GQA directly.
- **Bucket the attended extent**, not the buffer: for long `max_len`, compile a
  few decode graphs that attend over `cache[..., :bucket, :]` (static slice
  bounds) and pick the smallest bucket covering the longest active slot.
- **Prefill writes a block.** A prefill graph per prompt bucket writes
  positions `[0, bucket)` for the admitted slot(s); pass the slot index as a
  tensor and use the same masked or indexed write.

## Hybrid and recurrent layers

Linear-attention (Gated DeltaNet, Mamba) layers keep a fixed-size per-slot state
(`[slots, heads, d_k, d_v]` plus a short conv window) instead of a growing cache.
Treat it the same way: preallocate per slot, update in place inside the compiled
step, zero a slot's state on admission. NKI kernels for these updates exist in
the NKI library; see [`nki-kernels.md`](nki-kernels.md).

## Prefix caching

A fixed-slot cache has no block table to share. To reuse a prefix, copy the
cached prefix rows from the donor slot into the new slot on device (one compiled
copy graph per prefix bucket) before prefilling the suffix, or move to a block
layout with an explicit block table tensor. Only do this when the workload has
shared prefixes; it adds graphs and bookkeeping.

## Pitfalls

- **Returning the updated cache instead of mutating it.** Works, but doubles
  cache memory traffic and makes the caller rebind buffers every step.
- **Cache on CPU.** Every step copies it to the device and back: the classic
  host-bound decode. Check `k_cache.device.type == "neuron"`.
- **Host-side sampling.** A full-logits `.cpu()` per step is a large D2H copy.
  Sample on device; read back only `[B]` token ids.
- **Python ints in the region.** Positions, lengths and slot ids must be
  tensors, or each new value is a recompile.

## See also

- [`continuous-batching.md`](continuous-batching.md): slots and buckets around
  this cache.
- [`attention.md`](attention.md): attention over the cached extent.
- [`speculative-decoding.md`](speculative-decoding.md): masked commits of
  accepted tokens.
