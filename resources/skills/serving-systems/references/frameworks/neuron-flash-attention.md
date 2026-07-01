# Flash attention on Trainium (NKI)

**Flash attention IS available on Trainium** — it is *not* an NVIDIA-only
technique. `FlashAttention`/`FlashInfer` are NVIDIA *libraries*, but the
*algorithm* (tiled, online-softmax attention that never materializes the full
`[heads, q, kv]` score matrix) ships on Neuron as a **NKI kernel**:
`neuronx_distributed.kernels.flash_attn.nki_flash_attn_func`. Don't write the
naive `softmax(QKᵀ)V` and assume that's all Trainium can do.

> Verified on a NeuronCore (Trn2, SDK 2.30): traced via `torch_neuronx.trace`,
> compiled, and matched a naive reference to ~0.2% in BF16.

## Why it matters

The naive attention materializes the full score matrix `[B, H, q, kv]` (often in
FP32) and, with GQA, expands K/V to all query heads — a large activation
footprint, replicated across every layer of a single traced graph. That
**activation peak is a big part of the ~30 GB compile-time HBM OOM** that caps
batch size / decode-unroll width on a single 24 GB logical core. Flash attention
tiles the computation and never builds the full matrix, **cutting the activation
peak** — which is what lets you fit a larger batch (the lever that breaks the
host-bound throughput plateau). See [`aws-trainium.md`](../hardware/aws-trainium.md)
for the HBM math and [`nxd-kv-cache.md`](nxd-kv-cache.md) for the resident cache.

## API

```python
from neuronx_distributed.kernels.flash_attn import nki_flash_attn_func

out = nki_flash_attn_func(
    q, k, v,
    causal=True,
    softmax_scale=head_dim ** -0.5,
    mixed_precision=True,        # BF16 matmuls, FP32 softmax accumulation
    transpose_nki_inputs=False,  # use the (B, H, seq, D) layout below
)
```

## Constraints (these bite — found empirically)

- **`seqlen % 2048 == 0`.** The kernel raises `NotImplementedError("Only support
  sequence as multiples of 2K")` otherwise. Pad/bucket the context to a multiple
  of 2048 (e.g. a 512-token prompt → 2048 bucket). The check is on the **seq**
  axis, which depends on the layout flag below.
- **Layout depends on `transpose_nki_inputs`:**
  - `transpose_nki_inputs=False` → pass `q,k,v` as **`(B, H, seq, head_dim)`**
    (the normal layout; the kernel permutes internally).
  - `transpose_nki_inputs=True` (the **default**) → it reads the *last* axis as
    seq, i.e. it wants `(B, H, head_dim, seq)`. Easy to trip on.
- **Output is `(B, seq, H, head_dim)`** — note seq and heads are swapped vs the
  input; permute to `(B, H, seq, D)` if your downstream expects that.
- **GQA is not auto-handled at this entry point.** It indexes K/V by the *query*
  head, so `nheads_k` must equal `nheads`. **Expand K/V to the query-head count**
  (`repeat_interleave` / `repeat_kv`) before the call — the resident KV cache
  still stores only the GQA `kv_heads` (see [`nxd-kv-cache.md`](nxd-kv-cache.md));
  you expand transiently for the attention call. (Or use NxD's higher-level
  attention module, `neuronx_distributed_inference.modules.attention`, which
  wires GQA + flash for you.)

## Sketch (bespoke attention, NKI flash inner product)

```python
def attention(q, k_cache, v_cache, n_rep, scale):   # your QKV/RoPE already applied
    k = repeat_kv(k_cache, n_rep)                    # (B, H, S, D)  expand GQA -> q-heads
    v = repeat_kv(v_cache, n_rep)
    o = nki_flash_attn_func(q, k, v, causal=True, softmax_scale=scale,
                            mixed_precision=True, transpose_nki_inputs=False)
    return o.permute(0, 2, 1, 3)                     # (B, seq, H, D) -> (B, H, S, D)
```

For prefill you typically have `seq == bucket` (a multiple of 2048); for
single-token **decode** the flash kernel's 2K-seq constraint is on the *context*
length, so bucket the KV/context to 2048 multiples. If flash doesn't fit the
decode shape, the lower-level `neuronxcc.nki.kernels.attention` (incl.
`attention_tkg_fwd_kernel` for token-generation) and the NxD attention modules
are the next options — or write your own with the **`neuron-nki-*`** skills.

## Related

- [`nxd-kv-cache.md`](nxd-kv-cache.md) — device-resident KV cache (pair with this).
- [`aws-trainium.md`](../hardware/aws-trainium.md) — the 24 GB/core HBM budget.
- the bundled **`neuron-nki-*`** skills — to write/extend attention kernels.
