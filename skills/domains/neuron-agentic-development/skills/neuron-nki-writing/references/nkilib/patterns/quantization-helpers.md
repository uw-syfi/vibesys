# Quantization Helpers

## Overview
FP8 dtype detection and quantization-compatible dtype selection patterns extracted from attention and MoE kernel implementations. Use these when writing kernels that need to handle FP8 quantized inputs or select compute dtypes based on hardware generation.

## Quick Reference

| Function | Signature | Description |
|----------|-----------|-------------|
| `is_fp8_e4m3` | `(dtype) -> bool` | Check if dtype is FP8 E4M3 format |
| `is_fp8_e5m2` | `(dtype) -> bool` | Check if dtype is FP8 E5M2 format |
| `compatible_dtype` | `(compute_type) -> dtype` | Return gen3+-compatible dtype or float32 fallback |
| `div_ceil` | `(n, d) -> int` | Ceiling division helper |

## Import Options

**Default** — inline the source into your kernel file.
See the "Full Source Implementation" section below, or the bundled source files in `references/nkilib/core/`.

**If nkilib is installed** in the user's environment:
```python
# FP8 detection (from attention utils)
from nkilib.core.attention.attention_tkg_utils import is_fp8_e4m3, is_fp8_e5m2

# Compatible dtype (from MoE CTE utils)
from nkilib.core.moe.moe_cte.moe_cte_utils import compatible_dtype, div_ceil
```

## API Documentation

### `is_fp8_e4m3(dtype) -> bool`

Check if a dtype is FP8 E4M3 format, handling both numpy dtype objects and compiler internal name strings.

**Args:**
- `dtype`: A data type value (e.g., `nl.float8_e4m3` or compiler internal string)

**Returns:**
- `bool`: True if dtype is FP8 E4M3

**Constraints:**
- Handles two representations: `nl.float8_e4m3` object equality and `"float8e4"` string comparison

**Example:**
```python
import nki.language as nl

if is_fp8_e4m3(input_tensor.dtype):
    # Apply FP8 E4M3-specific dequantization
    scale = load_dequant_scale(scale_tensor)
```

---

### `is_fp8_e5m2(dtype) -> bool`

Check if a dtype is FP8 E5M2 format, handling both numpy dtype objects and compiler internal name strings.

**Args:**
- `dtype`: A data type value (e.g., `nl.float8_e5m2` or compiler internal string)

**Returns:**
- `bool`: True if dtype is FP8 E5M2

**Constraints:**
- Handles two representations: `nl.float8_e5m2` object equality and `"float8e5"` string comparison

**Example:**
```python
import nki.language as nl

if is_fp8_e5m2(weight.dtype):
    # FP8 E5M2 has larger range but less precision
    max_val = 57344.0
```

---

### `compatible_dtype(compute_type) -> dtype`

Return a compute-compatible dtype based on the current NeuronCore version. On gen3+ hardware, returns the requested dtype directly. On gen2 (Trn1/Inf2), falls back to `nl.float32` since bfloat16 compute is not fully supported for all operations.

**Args:**
- `compute_type`: Desired compute dtype (e.g., `nl.bfloat16`)

**Returns:**
- `dtype`: `compute_type` on gen3+, `nl.float32` on gen2

**Constraints:**
- Requires `nki.isa` for `get_nc_version()` and `nc_version.gen3`
- Only meaningful at trace time (compile time)

**Example:**
```python
import nki.isa as nisa
import nki.language as nl

# Use bfloat16 compute on gen3+, fall back to float32 on gen2
dtype = compatible_dtype(nl.bfloat16)
intermediate = nl.ndarray((128, 512), dtype=dtype, buffer=nl.sbuf)
```

---

### `div_ceil(n, d) -> int`

Integer ceiling division. Returns the smallest integer >= n/d.

**Args:**
- `n` (int): Numerator
- `d` (int): Denominator

**Returns:**
- `int`: Ceiling of n/d

**Example:**
```python
num_tiles = div_ceil(seq_len, 128)  # Number of 128-element tiles needed
```

## Usage Examples

### Pattern 1: FP8-aware weight loading
```python
import nki.language as nl
import nki.isa as nisa

def load_weights_with_dequant(weights, scale, compute_dtype):
    """Load weights and apply dequantization if FP8."""
    if is_fp8_e4m3(weights.dtype) or is_fp8_e5m2(weights.dtype):
        # Load FP8 weights and dequantize
        w_sb = nl.ndarray(weights.shape, dtype=weights.dtype, buffer=nl.sbuf)
        nisa.dma_copy(dst=w_sb, src=weights)
        # Scale will be applied during matmul or after
        return w_sb, scale
    else:
        w_sb = nl.ndarray(weights.shape, dtype=weights.dtype, buffer=nl.sbuf)
        nisa.dma_copy(dst=w_sb, src=weights)
        return w_sb, None
```

### Pattern 2: Generation-aware compute dtype selection
```python
import nki.isa as nisa
import nki.language as nl

def setup_compute_buffers(hidden_size, seq_len):
    """Allocate compute buffers with generation-appropriate dtype."""
    dtype = compatible_dtype(nl.bfloat16)
    num_tiles = div_ceil(hidden_size, 128)

    buffers = []
    for tile_idx in range(num_tiles):
        tile_size = min(128, hidden_size - tile_idx * 128)
        buf = nl.ndarray((tile_size, seq_len), dtype=dtype, buffer=nl.sbuf)
        buffers.append(buf)
    return buffers
```

### Pattern 3: Conditional FP8 scaling in MoE kernels
```python
import nki.isa as nisa
import nki.language as nl

def apply_projection_with_optional_dequant(proj_result, dequant_scale, compute_dtype):
    """Apply optional FP8 dequantization scale to projection results."""
    if dequant_scale is not None:
        nisa.tensor_scalar(
            dst=proj_result,
            data=proj_result,
            op0=nl.multiply,
            operand0=dequant_scale,
        )
    return proj_result
```

## Dependencies

- `nki.language` (`nl`): Core NKI language module for dtype constants (`nl.float8_e4m3`, `nl.float8_e5m2`, `nl.float32`)
- `nki.isa` (`nisa`): ISA module for `get_nc_version()` and `nc_version.gen3` (used by `compatible_dtype`)
- `nkilib/core/utils/common_types.py`: `ExpertAffinityScaleMode` enum (used alongside quantization in MoE contexts)
- `nkilib/core/utils/kernel_helpers.py`: `get_max_positive_value_for_dtype()` maps FP8 dtypes to max representable values (240.0 for E4M3, 57344.0 for E5M2)

## Full Source Implementation

```python
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import nki.isa as nisa
import nki.language as nl


def is_fp8_e4m3(dtype) -> bool:
    """Check if dtype is FP8 E4M3 (handles both numpy dtype and compiler internal name)."""
    return dtype == nl.float8_e4m3 or str(dtype) == "float8e4"


def is_fp8_e5m2(dtype) -> bool:
    """Check if dtype is FP8 E5M2 (handles both numpy dtype and compiler internal name)."""
    return dtype == nl.float8_e5m2 or str(dtype) == "float8e5"


def compatible_dtype(compute_type):
    """Return compute_type on gen3+, fall back to float32 on gen2."""
    return compute_type if nisa.get_nc_version() >= nisa.nc_version.gen3 else nl.float32


def div_ceil(n, d):
    """Integer ceiling division."""
    return (n + d - 1) // d
```
