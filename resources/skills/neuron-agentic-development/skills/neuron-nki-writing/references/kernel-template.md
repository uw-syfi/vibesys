# Kernel Template for NKI

Standard template for writing NKI kernels with proper structure, documentation, and patterns.
For comprehensive utility documentation, see `nkilib/core/` reference files.

## Self-Contained Utility Functions

These utilities are simple enough to inline. Use these for standalone kernels without library dependencies.

### kernel_assert

```python
def kernel_assert(condition: bool, error_text: str):
    """Assert with NKI-formatted error message."""
    assert condition, f"[INTERNAL_ERROR] [NCC_INKI016] Kernel validation exception: {error_text}"
```

### div_ceil

```python
def div_ceil(n: int, d: int) -> int:
    """Ceiling division: smallest integer >= n/d."""
    return (n + d - 1) // d
```

---

## Full Template (Self-Contained)

```python
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
One-line module description.

Extended description of what this kernel does and when to use it.
"""

import nki
import nki.isa as nisa
import nki.language as nl


# === Self-contained utilities ===

def kernel_assert(condition: bool, error_text: str):
    """Assert with NKI-formatted error message."""
    assert condition, f"[INTERNAL_ERROR] [NCC_INKI016] Kernel validation exception: {error_text}"

def div_ceil(n: int, d: int) -> int:
    """Ceiling division: smallest integer >= n/d."""
    return (n + d - 1) // d


# === Hardware constants ===
P_MAX = 128  # Partition dimension max
F_TILE_SIZE = 2048  # Free dimension tile size


@nki.jit
def my_kernel(
    input_tensor: nl.ndarray,
    param_tensor: nl.ndarray,
) -> nl.ndarray:
    """
    Short description of kernel operation.

    Extended description with algorithm details, optimization notes,
    and usage guidance.

    Dimensions:
        B: Batch size
        S: Sequence length
        H: Hidden dimension

    Args:
        input_tensor (nl.ndarray): [B, S, H] @ HBM, input data tensor
        param_tensor (nl.ndarray): [H] @ HBM, parameter vector

    Returns:
        nl.ndarray: [B, S, H] @ HBM, transformed output tensor

    Notes:
        - Constraint 1: H must be divisible by 128
        - Constraint 2: Uses float32 for internal accumulation
        - Performance: O(B * S * H) compute, O(B * S * H) memory

    Pseudocode:
        for batch_tile in tiles(B, P_MAX):
            for seq_tile in tiles(S, F_TILE_SIZE):
                tile = load(input[batch_tile, seq_tile, :])
                result = transform(tile, param)
                store(output[batch_tile, seq_tile, :], result)
    """
    # === Input Validation ===
    kernel_assert(len(input_tensor.shape) == 3, "Input must be 3D [B, S, H]")
    kernel_assert(
        input_tensor.shape[-1] <= P_MAX,
        f"Hidden dim {input_tensor.shape[-1]} exceeds P_MAX {P_MAX}"
    )

    # === Extract Dimensions ===
    batch_size, seq_len, hidden_dim = input_tensor.shape

    # === Allocate Output ===
    output = nl.ndarray(
        input_tensor.shape,
        dtype=input_tensor.dtype,
        buffer=nl.shared_hbm
    )

    # === Calculate Tiling ===
    num_batch_tiles = div_ceil(batch_size, P_MAX)
    num_seq_tiles = div_ceil(seq_len, F_TILE_SIZE)

    # === Main Processing Loop ===
    for b_idx in nl.affine_range(num_batch_tiles):
        b_start = b_idx * P_MAX
        b_end = min(b_start + P_MAX, batch_size)
        b_size = b_end - b_start

        # Load parameters (if needed per batch tile)
        param_sb = nl.ndarray((hidden_dim,), dtype=param_tensor.dtype, buffer=nl.sbuf)
        nisa.dma_copy(dst=param_sb, src=param_tensor[0:hidden_dim])

        for s_idx in nl.affine_range(num_seq_tiles):
            s_start = s_idx * F_TILE_SIZE
            s_end = min(s_start + F_TILE_SIZE, seq_len)
            s_size = s_end - s_start

            # --- Load Input Tile ---
            input_sb = nl.ndarray(
                (b_size, s_size, hidden_dim),
                dtype=input_tensor.dtype,
                buffer=nl.sbuf
            )
            nisa.dma_copy(
                dst=input_sb,
                src=input_tensor[
                    b_start:b_end,
                    s_start:s_end,
                    0:hidden_dim
                ]
            )

            # --- Compute ---
            result_sb = nl.ndarray(input_sb.shape, dtype=input_sb.dtype, buffer=nl.sbuf)
            # ... compute operations ...
            nisa.tensor_copy(dst=result_sb, src=input_sb)  # Placeholder

            # --- Store Output Tile ---
            nisa.dma_copy(
                dst=output[
                    b_start:b_end,
                    s_start:s_end,
                    0:hidden_dim
                ],
                src=result_sb
            )

    return output
```

## Docstring Format

Always include these sections:

```python
"""
Short one-line description.

Extended description (optional, for complex kernels).

Dimensions:
    NAME: Description, typical range

Args:
    param_name (type): [shape] @ location, description

Returns:
    type: [shape] @ location, description

Notes:
    - Constraints and requirements
    - Performance characteristics
    - Known limitations

Pseudocode:
    High-level algorithm in readable form
"""
```

## Key Patterns

### kernel_assert for Validation

```python
# Use instead of Python assert - provides NKI-specific error formatting
kernel_assert(condition, "Error message")

# Common validations
kernel_assert(len(x.shape) == 2, f"Expected 2D tensor, got {len(x.shape)}D")
kernel_assert(x.shape[0] <= P_MAX, f"P dim {x.shape[0]} exceeds {P_MAX}")
kernel_assert(x.shape[-1] % 128 == 0, f"H dim must be multiple of 128")
```

### div_ceil for Tile Counts

```python
# Calculate number of tiles needed
num_tiles = div_ceil(total_size, tile_size)

# Example: 1000 elements with tile size 128 -> 8 tiles
num_p_tiles = div_ceil(1000, 128)  # = 8
```

### Explicit Tiling Pattern

Use this pattern for tiling dimensions that exceed hardware limits:

```python
# Calculate number of tiles
num_p_tiles = div_ceil(outer_dim, P_MAX)

# Tile loop with explicit bounds calculation
for p_idx in nl.affine_range(num_p_tiles):
    p_start = p_idx * P_MAX
    p_end = min(p_start + P_MAX, outer_dim)  # Handle last tile
    p_size = p_end - p_start

    # Use calculated bounds for slicing
    tile = tensor[p_start:p_end, :]

# Example: outer_dim=300, P_MAX=128
# Iteration 0: p_start=0,   p_end=128, p_size=128
# Iteration 1: p_start=128, p_end=256, p_size=128
# Iteration 2: p_start=256, p_end=300, p_size=44  # Last tile smaller
```

## Mutable Output Tensors

For kernels that write to caller-provided tensors:

```python
import neuronxcc.nki.typing as nt  # ONLY for mutable tensor annotations

@nki.jit
def kernel_with_mutable_output(
    input_tensor: nl.ndarray,
    output_tensor: nt.tensor,  # Mutable: caller allocates, kernel writes
) -> None:
    """Kernel that writes to pre-allocated output tensor."""
    # No return value - writes directly to output_tensor
    nisa.dma_copy(dst=output_tensor[...], src=result_sb)
```

## Reference

- [tiled-range.md](nkilib/core/tiled-range.md) - TiledRange for partition tiling (used in cumsum pattern in SKILL.md)
- [kernel-helpers.md](nkilib/core/kernel-helpers.md) - `div_ceil`, `kernel_assert`, dtype helpers
- [tensor-view.md](nkilib/core/tensor-view.md) - TensorView for strided/broadcast access
- [allocator.md](nkilib/core/allocator.md) - SbufManager for complex SBUF allocation
