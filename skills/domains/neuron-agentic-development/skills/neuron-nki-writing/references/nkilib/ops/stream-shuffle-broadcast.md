# Stream Shuffle Broadcast

## Overview

Broadcasts the first partition of a source tensor across all partitions of a destination tensor using the `nc_stream_shuffle` ISA instruction. Use this when you need to replicate a single partition's data across multiple partitions in SBUF, such as broadcasting shared parameters or constants.

## When to Use

Adopt stream_shuffle_broadcast when:
- **Bias/scale addition after DMA load**: a 1D vector (bias, quantization scale, affinity score) was loaded into partition 0 and must be replicated to all 128 partitions before element-wise operations
- **Scalar broadcast**: any value that exists in a single partition but is needed across all PEs

**Skip when**: the value was loaded via contiguous DMA with partition-dimension tiling (it already exists in all partitions), or when TensorView broadcasting along a free dimension is sufficient.

Used in 13+ production kernels including attention (RoPE positions, softmax stats), QKV projection (scales, biases — 5 call sites), MLP (bias broadcast), MoE (expert affinities), and router TopK.

## Quick Reference

| Function | Description |
|----------|-------------|
| `stream_shuffle_broadcast(src, dst)` | Broadcast `src[0:1, :]` to all partitions of `dst` using stream shuffle |

## Import Options

**Default** — inline the source into your kernel file.
Source: `references/nkilib/core/utils/stream_shuffle_broadcast.py`

**If nkilib is installed** in the user's environment:
```python
from nkilib.core.utils.stream_shuffle_broadcast import stream_shuffle_broadcast
```

## API Documentation

### `stream_shuffle_broadcast(src, dst)`

Broadcasts the first partition (`src[0:1, :]`) onto every partition of `dst` using the `nisa.nc_stream_shuffle` instruction with a zero-filled shuffle mask.

**Args:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `src` | `nl.ndarray` (2D) | Source tensor in SBUF. Only partition 0 is read. |
| `dst` | `nl.ndarray` (2D) | Destination tensor in SBUF. All partitions are written. |

**Returns:** None (writes result into `dst`).

**Constraints:**
- Both `src` and `dst` must be 2D tensors.
- The free dimension (axis 1) of `src` must match that of `dst`: `src.shape[1] == dst.shape[1]`.
- Both tensors must reside in SBUF.
- The shuffle mask is all zeros, meaning every destination partition reads from source partition 0.
- Internally processes partitions in chunks of 32 (the stream shuffle hardware width).

**Example:**
```python
import nki.language as nl
from nkilib.core.utils.stream_shuffle_broadcast import stream_shuffle_broadcast

# Broadcast partition 0 of shared_params to all partitions of expanded_params
shared_params = nl.ndarray((1, 512), dtype=nl.float32, buffer=nl.sbuf)
expanded_params = nl.ndarray((128, 512), dtype=nl.float32, buffer=nl.sbuf)
stream_shuffle_broadcast(src=shared_params, dst=expanded_params)
```

## Usage Examples

### Pattern 1: Broadcasting a shared bias across partitions
```python
# Load bias into partition 0, then broadcast to all partitions
bias_p0 = nl.ndarray((1, hidden_dim), dtype=nl.float32, buffer=nl.sbuf)
nisa.dma_copy(dst=bias_p0, src=bias_hbm[0:1, :])

bias_all = nl.ndarray((num_partitions, hidden_dim), dtype=nl.float32, buffer=nl.sbuf)
stream_shuffle_broadcast(src=bias_p0, dst=bias_all)
```

### Pattern 2: Replicating a scaling vector for element-wise ops
```python
# Single-partition scale factor replicated for parallel computation
scale_single = nl.ndarray((1, seq_len), dtype=nl.float32, buffer=nl.sbuf)
scale_broadcast = nl.ndarray((n_heads, seq_len), dtype=nl.float32, buffer=nl.sbuf)
stream_shuffle_broadcast(src=scale_single, dst=scale_broadcast)
# Now use scale_broadcast in element-wise multiply across all heads
```

## Dependencies

- **`nkilib.core.utils.kernel_assert.kernel_assert`** -- Used for input validation (2D shape and matching free dimension).
- **NKI APIs**: `nki.isa` (`nisa.nc_stream_shuffle`).

## Source

See `references/nkilib/core/utils/stream_shuffle_broadcast.py` for the full implementation.
