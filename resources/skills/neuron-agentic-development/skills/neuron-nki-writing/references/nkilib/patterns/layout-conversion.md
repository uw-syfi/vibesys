# Layout Conversion

## Overview
Layout conversion patterns for transforming between interleaved and contiguous memory layouts in the partition dimension, primarily used for Rotary Position Embedding (RoPE). These patterns use permutation matrices and SBUF matmul for efficient in-SBUF layout changes when tensor sizes are small enough.

## Quick Reference

| Function | Signature | Description |
|----------|-----------|-------------|
| `_compute_convert_to_interleaved_mat` | `(x_sb) -> nl.ndarray` | Generate permutation matrix for layout conversion |
| `_convert_from_interleaved` | `(x_sb, mat) -> nl.ndarray` | Interleaved to contiguous: `[e0,o0,e1,o1,...] -> [e0,e1,...,o0,o1,...]` |
| `_convert_to_interleaved` | `(x_sb, mat) -> nl.ndarray` | Contiguous to interleaved: `[e0,e1,...,o0,o1,...] -> [e0,o0,e1,o1,...]` |
| `RoPE_sbuf` | `(x_in_sb, cos_sb, sin_sb, x_out_sb, convert_from_interleaved) -> nl.ndarray` | Apply RoPE rotation entirely in SBUF |

## Import Options

**Default** — inline the source into your kernel file.
See the "Full Source Implementation" section below, or the bundled source files in `references/nkilib/core/`.

**If nkilib is installed** in the user's environment:
```python
from nkilib.core.embeddings.rope import (
    RoPE_sbuf,
    _compute_convert_to_interleaved_mat,
    _convert_from_interleaved,
    _convert_to_interleaved,
)
```

## API Documentation

### `_compute_convert_to_interleaved_mat(x_sb) -> nl.ndarray`

Generate a permutation matrix P for converting between contiguous and interleaved layouts in the partition (d_head) dimension.

- `P @ X`: contiguous to interleaved: `[e0,e1,...,o0,o1,...] -> [e0,o0,e1,o1,...]`
- `P^T @ X`: interleaved to contiguous: `[e0,o0,e1,o1,...] -> [e0,e1,...,o0,o1,...]`

**Args:**
- `x_sb` (nl.ndarray): SBUF tensor with shape `[d_head, B, n_heads, S]` (used only for shape information)

**Returns:**
- `nl.ndarray`: Permutation matrix of shape `[d_head, d_head]` in SBUF

**Constraints:**
- `d_head` must be even
- `B * n_heads * S <= nl.tile_size.gemm_moving_fmax` (required for nc_matmul)

**Implementation Notes:**
- Builds the permutation matrix by applying strided access on an identity matrix
- Uses `nisa.tensor_copy` with `scalar_engine` and strided access patterns
- For d_head=4, the matrix maps: row 0->col 0, row 1->col 2, row 2->col 1, row 3->col 3

**Example:**
```python
import nki.language as nl

x_sb = nl.ndarray((128, 2, 4, 32), dtype=nl.bfloat16, buffer=nl.sbuf)
convert_mat = _compute_convert_to_interleaved_mat(x_sb)
# convert_mat is [128, 128] permutation matrix in SBUF
```

---

### `_convert_from_interleaved(x_sb, convert_to_interleaved_mat) -> nl.ndarray`

Convert interleaved to contiguous layout using matrix multiplication: `P^T @ x_sb`.

**Args:**
- `x_sb` (nl.ndarray): Input tensor in SBUF with shape `[d_head, B, n_heads, S]` in interleaved layout
- `convert_to_interleaved_mat` (nl.ndarray): Permutation matrix from `_compute_convert_to_interleaved_mat`

**Returns:**
- `nl.ndarray`: New SBUF tensor with shape `[d_head, B, n_heads, S]` in contiguous layout

**Constraints:**
- Input must be in SBUF
- `B * n_heads * S <= nl.tile_size.gemm_moving_fmax`

**Implementation Notes:**
- Uses `nisa.nc_matmul` with the permutation matrix as stationary and x_sb reshaped to 2D as moving
- Copies PSUM result back to SBUF via `nisa.activation` with `nl.copy`
- Returns a new buffer (does not modify input)

---

### `_convert_to_interleaved(x_sb, convert_to_interleaved_mat) -> nl.ndarray`

Convert contiguous to interleaved layout using matrix multiplication: `P @ x_sb`.

**Args:**
- `x_sb` (nl.ndarray): Input tensor in SBUF with shape `[d_head, B, n_heads, S]` in contiguous layout
- `convert_to_interleaved_mat` (nl.ndarray): Permutation matrix from `_compute_convert_to_interleaved_mat`

**Returns:**
- `nl.ndarray`: Same buffer with interleaved layout applied in-place

**Constraints:**
- Input must be in SBUF
- `B * n_heads * S <= nl.tile_size.gemm_moving_fmax`

**Implementation Notes:**
- Pre-transposes the permutation matrix (via `nisa.nc_transpose`) to compensate for `nc_matmul`'s implicit transpose of the stationary operand
- Modifies input buffer in-place

---

### `RoPE_sbuf(x_in_sb, cos_sb, sin_sb, x_out_sb, convert_from_interleaved=False) -> nl.ndarray`

Apply Rotary Position Embedding entirely in SBUF, for megakernel fusion scenarios where data is already in SBUF.

**RoPE Formula:**
```
out[even] = x[even] * cos - x[odd] * sin
out[odd]  = x[odd]  * cos + x[even] * sin
```

**Args:**
- `x_in_sb` (nl.ndarray): `[d_head, B, n_heads, S]` in SBUF - input embeddings
- `cos_sb` (nl.ndarray): `[d_head//2, B, S]` in SBUF - cosine frequencies
- `sin_sb` (nl.ndarray): `[d_head//2, B, S]` in SBUF - sine frequencies
- `x_out_sb` (nl.ndarray): `[d_head, B, n_heads, S]` in SBUF - output buffer
- `convert_from_interleaved` (bool): Convert from interleaved to contiguous layout before computation. Default: False

**Returns:**
- `nl.ndarray`: `x_out_sb` with RoPE applied (modified in-place)

**Constraints:**
- `d_head` must be 64 or 128
- `B` must be in (0, 64]
- `S` must be in (0, 512]
- `n_heads` must be in (0, 16]
- cos/sin shapes must be `(d_head//2, B, S)`
- cos and sin dtypes must match
- x_in_sb and x_out_sb dtypes must match
- For `convert_from_interleaved=True`: `B * n_heads * S <= nl.tile_size.gemm_moving_fmax`

**Example:**
```python
import nki.language as nl

d_head, B, n_heads, S = 128, 4, 8, 64
half_d = d_head // 2

x_in_sb = nl.ndarray((d_head, B, n_heads, S), dtype=nl.bfloat16, buffer=nl.sbuf)
cos_sb = nl.ndarray((half_d, B, S), dtype=nl.bfloat16, buffer=nl.sbuf)
sin_sb = nl.ndarray((half_d, B, S), dtype=nl.bfloat16, buffer=nl.sbuf)
x_out_sb = nl.ndarray((d_head, B, n_heads, S), dtype=nl.bfloat16, buffer=nl.sbuf)

# Apply RoPE in SBUF (contiguous layout)
RoPE_sbuf(x_in_sb, cos_sb, sin_sb, x_out_sb)
```

## Usage Examples

### Pattern 1: RoPE in a fused attention kernel
```python
import nki.isa as nisa
import nki.language as nl

def apply_rope_in_attention(q_sb, k_sb, cos_sb, sin_sb):
    """Apply RoPE to Q and K tensors already in SBUF."""
    d_head, B, n_heads, S = q_sb.shape

    q_out = nl.ndarray(q_sb.shape, dtype=q_sb.dtype, buffer=nl.sbuf)
    k_out = nl.ndarray(k_sb.shape, dtype=k_sb.dtype, buffer=nl.sbuf)

    RoPE_sbuf(q_sb, cos_sb, sin_sb, q_out, convert_from_interleaved=False)
    RoPE_sbuf(k_sb, cos_sb, sin_sb, k_out, convert_from_interleaved=False)

    return q_out, k_out
```

### Pattern 2: Layout conversion for interleaved-format models
```python
import nki.isa as nisa
import nki.language as nl

def convert_layout_for_rope(x_sb):
    """Convert interleaved layout to contiguous, apply operation, convert back."""
    d_head, B, n_heads, S = x_sb.shape

    # Build permutation matrix once
    convert_mat = _compute_convert_to_interleaved_mat(x_sb)

    # Convert: [e0,o0,e1,o1,...] -> [e0,e1,...,o0,o1,...]
    x_contiguous = _convert_from_interleaved(x_sb, convert_mat)

    # ... perform operations in contiguous layout ...

    # Convert back: [e0,e1,...,o0,o1,...] -> [e0,o0,e1,o1,...]
    x_interleaved = _convert_to_interleaved(x_contiguous, convert_mat)

    return x_interleaved
```

### Pattern 3: Standalone RoPE kernel with strided DMA fallback
```python
import nki.language as nl

# For large tensors where B*n_heads*S > gemm_moving_fmax,
# use the standalone RoPE kernel which handles strided DMA:
from nkilib.core.embeddings.rope import RoPE

# RoPE automatically selects between:
# - SBUF matmul layout conversion (small tensors, relayout_in_sbuf=True)
# - Strided DMA gather/scatter (large tensors, default)
output = RoPE(x_in, cos, sin, lnc_shard=True, contiguous_layout=False)
```

## Dependencies

- `nki.isa` (`nisa`): `nc_matmul`, `nc_transpose`, `tensor_copy`, `tensor_tensor`, `dma_copy`, `activation`, `dma_transpose`
- `nki.language` (`nl`): `nl.tile_size.gemm_moving_fmax` for SBUF matmul size limit
- `nki.tensor` (`ntensor`): `ntensor.identity()` for generating identity matrix
- `nkilib/core/utils/tensor_view.py`: `TensorView` class for `expand_dim`, `broadcast`, `slice`, `get_view`
- `nkilib/core/utils/kernel_assert.py`: `kernel_assert()` for input validation
- `nkilib/core/utils/kernel_helpers.py`: `get_verified_program_sharding_info()` for LNC sharding

## Full Source Implementation

```python
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import nki.isa as nisa
import nki.language as nl
import nki.tensor as ntensor

from nkilib.core.utils.kernel_assert import kernel_assert
from nkilib.core.utils.tensor_view import TensorView


def RoPE_sbuf(
    x_in_sb: nl.ndarray,
    cos_sb: nl.ndarray,
    sin_sb: nl.ndarray,
    x_out_sb: nl.ndarray,
    convert_from_interleaved: bool = False,
) -> nl.ndarray:
    """
    Apply RoPE on tensors in SBUF (for megakernel fusion).

    RoPE Formula:
        out[even] = x[even]*cos - x[odd]*sin
        out[odd] = x[odd]*cos + x[even]*sin

    Args:
        x_in_sb: [d_head, B, n_heads, S] @ SBUF
        cos_sb: [d_head//2, B, S] @ SBUF
        sin_sb: [d_head//2, B, S] @ SBUF
        x_out_sb: [d_head, B, n_heads, S] @ SBUF
        convert_from_interleaved: convert from interleaved to contiguous layout

    Returns:
        x_out_sb with RoPE applied (modified in-place)
    """
    d_head, B, n_heads, S = x_out_sb.shape
    half_d = d_head // 2

    kernel_assert(x_in_sb.dtype == x_out_sb.dtype, 'RoPE_sbuf: dtype mismatch between x_in_sb and x_out_sb')

    if convert_from_interleaved:
        convert_to_interleaved_mat = _compute_convert_to_interleaved_mat(x_in_sb)
        x_in_sb = _convert_from_interleaved(x_in_sb, convert_to_interleaved_mat)

    sb_odd = nl.ndarray((half_d, B, n_heads, S), dtype=x_in_sb.dtype, buffer=nl.sbuf)
    nisa.tensor_copy(dst=sb_odd, src=x_in_sb[half_d:, :, :, :])

    even_cos = nl.ndarray((half_d, B, n_heads, S), dtype=x_in_sb.dtype, buffer=nl.sbuf)
    odd_cos = nl.ndarray((half_d, B, n_heads, S), dtype=x_in_sb.dtype, buffer=nl.sbuf)
    even_sin = nl.ndarray((half_d, B, n_heads, S), dtype=x_in_sb.dtype, buffer=nl.sbuf)
    odd_sin = nl.ndarray((half_d, B, n_heads, S), dtype=x_in_sb.dtype, buffer=nl.sbuf)

    nisa.tensor_tensor(
        even_cos, x_in_sb[:half_d, :, :, :],
        TensorView(cos_sb).expand_dim(2).broadcast(dim=2, size=n_heads).get_view(),
        nl.multiply,
    )
    nisa.tensor_tensor(
        odd_cos, sb_odd[:half_d, :, :, :],
        TensorView(cos_sb).expand_dim(2).broadcast(dim=2, size=n_heads).get_view(),
        nl.multiply,
    )
    nisa.tensor_tensor(
        even_sin, x_in_sb[:half_d, :, :, :],
        TensorView(sin_sb).expand_dim(2).broadcast(dim=2, size=n_heads).get_view(),
        nl.multiply,
    )
    nisa.tensor_tensor(
        odd_sin, sb_odd[:half_d, :, :, :],
        TensorView(sin_sb).expand_dim(2).broadcast(dim=2, size=n_heads).get_view(),
        nl.multiply,
    )

    nisa.tensor_tensor(x_out_sb[:half_d, :, :, :], even_cos, odd_sin, nl.subtract)
    nisa.tensor_tensor(x_out_sb[half_d:, :, :, :], odd_cos, even_sin, nl.add)

    if convert_from_interleaved:
        x_out_sb = _convert_to_interleaved(x_out_sb, convert_to_interleaved_mat)

    return x_out_sb


def _compute_convert_to_interleaved_mat(x_sb: nl.ndarray) -> nl.ndarray:
    """
    Generate permutation matrix for layout conversion.

    Creates matrix P where:
        P @ X: [e0,e1,...,o0,o1,...] -> [e0,o0,e1,o1,...] (contiguous to interleaved)
        P^T @ X: [e0,o0,e1,o1,...] -> [e0,e1,...,o0,o1,...] (interleaved to contiguous)

    Returns:
        [d_head, d_head] @ SBUF - permutation matrix
    """
    d_head, B, n_heads, S = x_sb.shape
    half_d = d_head // 2
    kernel_assert(d_head % 2 == 0, f'_compute_convert_to_interleaved_mat: d_head must be even, got {d_head}')
    kernel_assert(
        B * n_heads * S <= nl.tile_size.gemm_moving_fmax,
        f'_compute_convert_to_interleaved_mat: B*n_heads*S={B * n_heads * S} '
        f'exceeds gemm_moving_fmax={nl.tile_size.gemm_moving_fmax}',
    )

    identity_hbm = nl.shared_constant(ntensor.identity(d_head, nl.float32))
    identity_sb = nl.ndarray((d_head, d_head), dtype=x_sb.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=identity_sb, src=identity_hbm)

    convert_to_interleaved_mat = nl.ndarray((d_head, d_head), dtype=x_sb.dtype, buffer=nl.sbuf)
    nisa.tensor_copy(
        dst=convert_to_interleaved_mat.reshape((d_head, 2, half_d)),
        src=identity_sb.ap(pattern=[[d_head, d_head], [1, 2], [2, half_d]]),
        engine=nisa.scalar_engine,
    )

    return convert_to_interleaved_mat


def _convert_from_interleaved(x_sb: nl.ndarray, convert_to_interleaved_mat: nl.ndarray) -> nl.ndarray:
    """
    Convert interleaved to contiguous layout: [e0,o0,e1,o1,...] -> [e0,e1,...,o0,o1,...].
    Uses P^T @ x_sb via nc_matmul. Returns a new buffer (does not modify input).
    """
    d_head, B, n_heads, S = x_sb.shape
    kernel_assert(x_sb.buffer == nl.sbuf, '_convert_from_interleaved: input must be in SBUF')
    kernel_assert(
        B * n_heads * S <= nl.tile_size.gemm_moving_fmax,
        f'_convert_from_interleaved: B*n_heads*S={B * n_heads * S} '
        f'exceeds gemm_moving_fmax={nl.tile_size.gemm_moving_fmax}',
    )

    x_converted_sb = nl.ndarray(x_sb.shape, dtype=x_sb.dtype, buffer=nl.sbuf)
    x_psum = nl.ndarray((d_head, B * n_heads * S), dtype=nl.float32, buffer=nl.psum)
    nisa.nc_matmul(dst=x_psum, stationary=convert_to_interleaved_mat, moving=x_sb.reshape((d_head, B * n_heads * S)))
    nisa.activation(dst=x_converted_sb, op=nl.copy, data=x_psum.reshape((d_head, B, n_heads, S)))
    return x_converted_sb


def _convert_to_interleaved(x_sb: nl.ndarray, convert_to_interleaved_mat: nl.ndarray) -> nl.ndarray:
    """
    Convert contiguous to interleaved layout: [e0,e1,...,o0,o1,...] -> [e0,o0,e1,o1,...].
    Uses P @ x_sb via nc_matmul (with pre-transpose to compensate for implicit transpose).
    """
    d_head, B, n_heads, S = x_sb.shape
    kernel_assert(x_sb.buffer == nl.sbuf, '_convert_to_interleaved: input must be in SBUF')
    kernel_assert(
        B * n_heads * S <= nl.tile_size.gemm_moving_fmax,
        f'_convert_to_interleaved: B*n_heads*S={B * n_heads * S} '
        f'exceeds gemm_moving_fmax={nl.tile_size.gemm_moving_fmax}',
    )

    convert_from_interleaved_sb = nl.ndarray((d_head, d_head), dtype=convert_to_interleaved_mat.dtype, buffer=nl.sbuf)
    convert_from_interleaved_psum = nl.ndarray((d_head, d_head), dtype=convert_to_interleaved_mat.dtype, buffer=nl.psum)
    nisa.nc_transpose(dst=convert_from_interleaved_psum, data=convert_to_interleaved_mat)
    nisa.tensor_copy(dst=convert_from_interleaved_sb, src=convert_from_interleaved_psum, engine=nisa.scalar_engine)

    x_psum = nl.ndarray((d_head, B * n_heads * S), dtype=nl.float32, buffer=nl.psum)
    nisa.nc_matmul(dst=x_psum, stationary=convert_from_interleaved_sb, moving=x_sb.reshape((d_head, B * n_heads * S)))
    nisa.tensor_copy(dst=x_sb, src=x_psum.reshape((d_head, B, n_heads, S)), engine=nisa.scalar_engine)
    return x_sb
```
