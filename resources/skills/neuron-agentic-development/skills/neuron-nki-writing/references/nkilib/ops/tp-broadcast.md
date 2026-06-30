# Transpose Broadcast

## Overview

Transposes a column from the source tensor and broadcasts it across all partitions of the destination tensor using a single transpose instruction on the PE (processing engine). Use this when you need to take a column vector in `[P, F]` layout and produce a transposed, broadcast result in `[B, P]` layout across partitions.

## When to Use

Adopt tp_broadcast when:
- **Partition-to-free dimension broadcast**: a value exists as a column vector along the partition dimension and needs to be transposed and replicated across all partitions in the free dimension (e.g., softmax max value in attention)

**Skip when**: `stream_shuffle_broadcast` suffices (simpler, for same-dimension broadcast), or the value is already in the correct layout.

Highly specialized: used in 1 production kernel (attention TKG — broadcasting per-head softmax max across partitions).

## Quick Reference

| Function | Description |
|----------|-------------|
| `tp_broadcast(src, dst, src_offset, psum_address=None)` | Transpose `src[0:1, :]` and broadcast to all partitions of `dst` via PSUM intermediate |

## Import Options

**Default** — inline the source into your kernel file.
Source: `references/nkilib/core/utils/tp_broadcast.py`

**If nkilib is installed** in the user's environment:
```python
from nkilib.core.utils.tp_broadcast import tp_broadcast
```

## API Documentation

### `tp_broadcast(src, dst, src_offset, psum_address=None)`

Transposes then broadcasts `src[0:1, :]` onto all partitions of `dst`. Each partition of `dst` receives a transposed version of the source data. Uses PSUM as an intermediate buffer for the transpose operation.

**Args:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `src` | `nl.ndarray` (2D) | (required) | Source tensor in SBUF. Shape: `[P, F]`. Only partition 0 is read. |
| `dst` | `nl.ndarray` (2D) | (required) | Destination tensor in SBUF. Shape: `[B, P]` where `B` is the broadcast dimension. |
| `src_offset` | `int` | (required) | Offset in the free dimension (F) to select which column to transpose from. |
| `psum_address` | `int` or `None` | `None` | Optional explicit PSUM bank address for the intermediate transpose buffer. |

**Returns:** None (writes result into `dst`).

**Constraints:**
- `src` must be 2D with shape `[P, F]`.
- `dst` must be 2D with shape `[B, P]` where `P` matches `src.shape[0]`.
- The transposed dimension of `dst` (`dst.shape[1]`) must equal `src.shape[0]` (the partition dimension).
- Uses PSUM as intermediate storage -- the transpose result is first written to a `float32` PSUM buffer of shape `[B, P]`, then copied to `dst` in SBUF.
- **Warning:** This function always broadcasts `src[0:1]`. Passing a pre-sliced tensor will raise an error.
- PSUM free dimension limit applies: `B` must be within PSUM limits (512 for gen2/gen3, 4096 for gen4).

**Example:**
```python
import nki.language as nl
from nkilib.core.utils.tp_broadcast import tp_broadcast

src = nl.ndarray((128, 64), dtype=nl.float16, buffer=nl.sbuf)
dst = nl.ndarray((32, 128), dtype=nl.float16, buffer=nl.sbuf)
# Transpose column at offset 0 of src and broadcast to all 32 partitions of dst
tp_broadcast(src=src, dst=dst, src_offset=0)
```

## Usage Examples

### Pattern 1: Broadcasting a column for attention score computation
```python
# Transpose a column from the key tensor and broadcast across query heads
key_buf = nl.ndarray((128, seq_len), dtype=nl.float16, buffer=nl.sbuf)
broadcast_key = nl.ndarray((num_heads, 128), dtype=nl.float16, buffer=nl.sbuf)

# Transpose column at offset `col_idx` and broadcast
tp_broadcast(src=key_buf, dst=broadcast_key, src_offset=col_idx)
```

### Pattern 2: Using explicit PSUM address to avoid conflicts
```python
# When other operations are using PSUM, specify a non-conflicting address
tp_broadcast(
    src=weight_col,
    dst=expanded_weight,
    src_offset=0,
    psum_address=1024,  # Avoid conflict with other PSUM allocations
)
```

## Dependencies

- **`nkilib.core.utils.kernel_assert.kernel_assert`** -- Used for dimension validation.
- **NKI APIs**: `nki.isa` (`nisa.nc_transpose`, `nisa.tensor_copy`), `nki.language` (`nl.ndarray`, `nl.psum`, `nl.float32`).

## Source

See `references/nkilib/core/utils/tp_broadcast.py` for the full implementation.
