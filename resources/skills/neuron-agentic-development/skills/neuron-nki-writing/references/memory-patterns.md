# Memory Patterns for NKI

This reference covers DMA patterns and tiling strategies for NKI kernels.

## Buffer Types

| Buffer | Syntax | Max P | Max F | Use Case |
|--------|--------|-------|-------|----------|
| SBUF | `buffer=nl.sbuf` | 128 | 32767 | General compute storage |
| PSUM | `buffer=nl.psum` | 128 | 512 (gen2/3) / 4096 (gen4) | MatMul accumulation |
| HBM | `buffer=nl.shared_hbm` | - | - | Input/output tensors |

## Basic Contiguous DMA

The most efficient pattern for aligned, sequential memory access.

**Pattern from cumsum kernel** (contiguous DMA load/store):

```python
# Allocate SBUF tile with hardware-friendly shape
data_sb = nl.ndarray((P_MAX, F_TILE_SIZE), dtype=x.dtype, buffer=nl.sbuf)

# Load contiguous slice from HBM to SBUF
nisa.dma_copy(
    dst=data_sb[0:p_size, 0:f_size],
    src=x_hbm[p_start:p_start + p_size, f_start:f_end],
)

# Store contiguous slice from SBUF to HBM
nisa.dma_copy(
    dst=y_hbm[p_start:p_start + p_size, f_start:f_end],
    src=result_sb[0:p_size, 0:f_size],
)
```

**Key points:**
- Source and destination slices must have matching shapes
- Use `min()` to handle edge cases: `f_end = min(f_start + F_TILE_SIZE, total_f)`
- Contiguous access maximizes DMA bandwidth

## Strided DMA Pattern

For gather/scatter operations with non-contiguous access.

**Pattern from RoPE kernel** (strided DMA with TensorView — see [tensor-view.md](nkilib/core/tensor-view.md)):

```python
from nkilib.core.utils.tensor_view import TensorView  # or inline from references/nkilib/core/utils/tensor_view.py

# Gather even indices with stride=2 in dimension 0
nisa.dma_copy(
    dst=x_sb[:half_d, :, :, :],
    src=TensorView(x_hbm)
        .slice(dim=0, start=0, end=d_head, step=2)  # stride=2
        .slice(dim=3, start=tile_start, end=tile_start + tile_size)
        .get_view(),
)

# Gather odd indices
nisa.dma_copy(
    dst=x_sb[half_d:, :, :, :],
    src=TensorView(x_hbm)
        .slice(dim=0, start=1, end=d_head, step=2)  # start=1, stride=2
        .slice(dim=3, start=tile_start, end=tile_start + tile_size)
        .get_view(),
)
```

**Key points:**
- Use `TensorView` helper for strided access patterns
- Less efficient than contiguous DMA - use when necessary
- Useful for interleaved data layouts

## TiledRange Pattern

For processing tensors larger than hardware limits.

**Pattern from cumsum kernel** (TiledRange — see [tiled-range.md](nkilib/core/tiled-range.md), [kernel-helpers.md](nkilib/core/kernel-helpers.md)):

```python
from nkilib.core.utils.tiled_range import TiledRange  # or inline from references/nkilib/core/utils/tiled_range.py
from nkilib.core.utils.kernel_helpers import div_ceil  # or inline from references/nkilib/core/utils/kernel_helpers.py

P_MAX = 128  # Hardware partition limit
F_TILE_SIZE = 2048  # Free dimension tile size

# Calculate number of tiles
num_f_tiles = div_ceil(last_dim, F_TILE_SIZE)

# Loop over partition tiles
for p_tile in TiledRange(outer_dim, P_MAX):
    # p_tile.start_offset: starting row index
    # p_tile.size: number of rows in this tile (may be < P_MAX for last tile)

    # Loop over free dimension tiles
    for f_tile_idx in nl.sequential_range(num_f_tiles):
        f_start = f_tile_idx * F_TILE_SIZE
        f_end = min(f_start + F_TILE_SIZE, last_dim)
        f_size = f_end - f_start

        # Process tile at [p_tile.start_offset:p_tile.start_offset+p_tile.size, f_start:f_end]
        ...
```

**TiledRange attributes:**
- `p_tile.start_offset` - Starting index in partition dimension
- `p_tile.size` - Size of current tile (handles edge cases)
- `p_tile.end_offset` - End index (start_offset + size)

## Multi-dimensional Tiling

For tensors with multiple large dimensions.

```python
# Process 3D tensor: (batch, seq, hidden)
for b_tile in TiledRange(batch_size, P_MAX):
    for s_tile_idx in nl.affine_range(num_seq_tiles):
        s_start = s_tile_idx * seq_tile_size
        s_end = min(s_start + seq_tile_size, seq_len)

        for h_tile_idx in nl.affine_range(num_hidden_tiles):
            h_start = h_tile_idx * hidden_tile_size
            h_end = min(h_start + hidden_tile_size, hidden_dim)

            # Load tile
            tile = nl.ndarray((b_tile.size, s_end - s_start, h_end - h_start),
                             dtype=x.dtype, buffer=nl.sbuf)
            nisa.dma_copy(
                dst=tile,
                src=x[b_tile.start_offset:b_tile.end_offset, s_start:s_end, h_start:h_end]
            )
```

## Reshape for 2D Processing

Common pattern: reshape ND tensor to 2D for simpler tiling.

```python
# Original: x of shape (B, S, H)
# Collapse to 2D: (B*S, H)

# Compute product of all dimensions except last
outer_dim = 1
for dim in x.shape[:-1]:
    outer_dim *= dim
last_dim = x.shape[-1]
shape_2d = (outer_dim, last_dim)

x_2d = x.reshape(shape_2d)

# Allocate output with same original shape
y = nl.ndarray(x.shape, dtype=x.dtype, buffer=nl.shared_hbm)
y_2d = y.reshape(shape_2d)

# Process in 2D...

return y  # Return with original shape
```

## PSUM to SBUF Transfer

For matrix multiply results that need further processing.

```python
# MatMul accumulates in PSUM
psum_result = nl.ndarray((P, F), dtype=nl.float32, buffer=nl.psum)
nisa.nc_matmul(dst=psum_result, stationary=a_tile, moving=b_tile)

# Transfer to SBUF for element-wise ops
sbuf_result = nl.ndarray((P, F), dtype=output_dtype, buffer=nl.sbuf)
nisa.tensor_copy(dst=sbuf_result, src=psum_result)
```

## Further Reading

- [tiled-range.md](nkilib/core/tiled-range.md) - Complete TiledRange API (used in tiling examples above)
- [tensor-view.md](nkilib/core/tensor-view.md) - Complete TensorView API (used in strided DMA above)
- [kernel-helpers.md](nkilib/core/kernel-helpers.md) - `div_ceil`, dtype utilities, SPMD helpers

Use `/neuron-nki-docs dma` or `/neuron-nki-docs memory` for detailed API documentation.
