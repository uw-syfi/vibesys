# Attention on Trainium

Which attention implementation to use on TorchNeuron, from least to most work.
Prerequisites: [`neuron-pytorch.md`](neuron-pytorch.md),
[`static-kv-cache.md`](static-kv-cache.md).

## 1. `F.scaled_dot_product_attention` inside a compiled region

Start here. SDPA lowers under `torch.compile(backend="neuron")`, and TorchNeuron
**routes it to a built-in NKI flash-attention kernel when the query sequence
length is a multiple of 2048** (`TORCH_NEURONX_ENABLE_NKI_SDPA=1`, the default).
Other shapes use the compiler's own lowering.

```python
o = F.scaled_dot_product_attention(q, k, v, is_causal=True)            # prefill
o = F.scaled_dot_product_attention(q, k_ctx, v_ctx, attn_mask=valid)   # decode
```

- **Prefill:** bucket prompts to multiples of 2048 when the flash path matters
  (long prompts); shorter buckets still compile and run, without the NKI kernel.
- **Decode** (query length 1): attention over a bucketed context extent with a
  boolean `attn_mask` built from per-slot positions. Expand GQA KV heads with
  `repeat_interleave` inside the region.
- Verified on trn2 (SDK 2.33): compiled causal SDPA at seq 2048 in BF16 matched
  the FP32 CPU reference to 1.4e-2 max abs error.

## 2. NKI library attention kernels

The container ships `nkilib`, AWS's NKI kernel library. Its attention kernels
take serving layouts directly (GQA, KV cache, token generation):

| Kernel | Module | Use |
|:--|:--|:--|
| Context encoding (prefill) | `nkilib.core.attention.attention_cte` | Long prefill |
| Token generation (decode) | `nkilib.core.attention.attention_tkg` | Decode over the cache |
| Packed decode | `nkilib.core.attention.attention_tkg_seq_packed` | Packed multi-sequence decode |

Each kernel is an `@nki.jit` function; the `*_torch.py` file next to it is a
PyTorch **reference** for checking numerics, not a wrapper. Call the kernel on
`neuron` tensors directly, or register it with `torch_neuronx.nki_op` so it sits
inside the compiled graph without a break; see [`nki-kernels.md`](nki-kernels.md).
Read the kernel's docstring for its layout contract: these kernels often want
transposed or tiled inputs, and the layout is where integrations go wrong.

Design notes for these kernels are vendored in the `neuron-nki-docs` skill under
`references/reference/library/` (`attention-cte.md`, `attention-tkg.md`).

## 3. Your own NKI kernel

When a profile shows attention dominating and neither of the above fits the
shape, write one with the `neuron-nki-writing` skill and wire it in with
`nki_op`.

## Pitfalls

- **Materializing `[B, H, q, kv]` scores in FP32** for long contexts: the
  activation peak caps batch size on a 24 GiB core. Use SDPA (flash path) or a
  library kernel.
- **A Python-int sequence length** in the region: each new length is a new
  graph. Lengths and positions are tensors; extents are buckets.
- **Comparing against an eager CPU reference in BF16**: compare in FP32 or
  FP64 on CPU, and expect ~1e-2 differences from BF16 accumulation order.

## See also

- [`static-kv-cache.md`](static-kv-cache.md): the cache attention reads from.
- [`nki-kernels.md`](nki-kernels.md): calling NKI kernels from PyTorch.
