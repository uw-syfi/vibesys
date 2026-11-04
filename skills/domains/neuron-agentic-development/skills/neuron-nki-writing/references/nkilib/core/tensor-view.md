# TensorView

## Overview

TensorView is a high-level wrapper around NKI tensors that provides PyTorch-like view operations (slicing, permuting, broadcasting, reshaping) without copying data. It uses NKI's array pattern (`ap`) functionality to efficiently generate memory access patterns from shape/stride/offset metadata.

## When to Use

Adopt TensorView when the kernel needs any of:
- **Strided/interleaved DMA**: `slice(dim, start, end, step=2)` gathers even/odd elements without loops
- **Broadcasting**: `expand_dim(d).broadcast(d, size)` replicates across a dimension (e.g., cos/sin across heads in RoPE)
- **Reshape without copy**: `reshape_dim()` / `flatten_dims()` to reshape multi-dimensional tensors for DMA or matmul
- **Dynamic expert/block selection**: `select(dim, scalar_offset)` for indirect HBM access at runtime (MoE, paged attention)
- **Complex .ap() patterns**: TensorView generates correct `ap()` arguments — avoid hand-coding `ap()` for 3D+ tensors

**Skip when**: simple contiguous access like `tensor[start:end, :]` is sufficient.

Used in 17+ production kernels including attention, RoPE, MLP projections, MoE expert selection, and output projection.

## Quick Reference

| Method | Signature | Description |
|--------|-----------|-------------|
| `__init__` | `(base_tensor: nl.ndarray)` | Create a view from an NKI tensor |
| `get_view` | `() -> nl.ndarray` | Generate the actual NKI tensor with array pattern applied |
| `slice` | `(dim, start, end, step=1) -> TensorView` | Slice along a dimension |
| `permute` | `(dims: List[int]) -> TensorView` | Reorder dimensions |
| `broadcast` | `(dim, size) -> TensorView` | Expand a size-1 dimension |
| `reshape_dim` | `(dim, shape: List[int]) -> TensorView` | Split one dimension into multiple |
| `flatten_dims` | `(start_dim, end_dim) -> TensorView` | Flatten contiguous dimensions into one |
| `expand_dim` | `(dim) -> TensorView` | Insert a size-1 dimension |
| `squeeze_dim` | `(dim) -> TensorView` | Remove a size-1 dimension |
| `select` | `(dim, index) -> TensorView` | Select a single index along a dimension |
| `rearrange` | `(src_pattern, dst_pattern, fixed_sizes=None) -> TensorView` | Einops-style dimension rearrangement |
| `get_dim` | `() -> int` | Return number of dimensions |
| `is_sbuf` | `() -> bool` | Check if base tensor is in SBUF |
| `is_hbm` | `() -> bool` | Check if base tensor is in HBM |
| `has_dynamic_access` | `() -> bool` | Check if view uses dynamic (indirect) indexing |
| `get_trivial_strides` | `(shape, base_stride=1) -> Tuple[int, ...]` | Compute row-major strides (static) |

## Import Options

**Default** — inline the source into your kernel file.
Source: `references/nkilib/core/utils/tensor_view.py`

**If nkilib is installed** in the user's environment:
```python
from nkilib.core.utils.tensor_view import TensorView
```

## API Documentation

### `__init__(base_tensor: nl.ndarray)`

Create a TensorView wrapping an NKI tensor.

**Args:**
- `base_tensor` (`nl.ndarray`): The underlying NKI tensor. Must not be None.

**Returns:** None (constructor)

**Constraints:**
- `base_tensor` must not be `None`

```python
sbuf_tile = nl.ndarray((128, 512), dtype=nl.bfloat16, buffer=nl.sbuf)
view = TensorView(sbuf_tile)
```

---

### `get_view() -> nl.ndarray`

Generate the actual NKI tensor with the current view's array pattern applied. Call this to obtain the `nl.ndarray` you can pass to NKI operations.

**Returns:** `nl.ndarray` with the specified view pattern applied.

```python
view = TensorView(sbuf_tile)
sliced = view.slice(1, 0, 256)
result = sliced.get_view()  # nl.ndarray usable in NKI ops
```

---

### `slice(dim: int, start: int, end: int, step: int = 1) -> TensorView`

Create a sliced view along a specific dimension.

**Args:**
- `dim` (`int`): Dimension to slice
- `start` (`int`): Start index (inclusive), must be >= 0
- `end` (`int`): End index (exclusive), must be > start and <= shape[dim]
- `step` (`int`): Step size (default: 1)

**Returns:** New `TensorView` with sliced dimension.

**Constraints:**
- `dim < get_dim()`
- `0 <= start < end <= shape[dim]`

```python
# shape [128, 512] -> slice dim=1, 0:256 -> shape [128, 256]
view = TensorView(sbuf_tile)
sliced = view.slice(1, 0, 256)
```

---

### `permute(dims: List[int]) -> TensorView`

Create a permuted view by reordering dimensions.

**Args:**
- `dims` (`List[int]`): New order of dimensions. Must be a valid permutation.

**Returns:** New `TensorView` with permuted dimensions.

**Constraints:**
- Length of `dims` must equal number of dimensions
- No duplicate indices
- For SBUF tensors, `dims[0]` must be `0` (partition dimension stays outermost)

```python
# shape [128, 4, 512] -> permute [0, 2, 1] -> shape [128, 512, 4]
view = TensorView(sbuf_tile_3d)
permuted = view.permute([0, 2, 1])
```

---

### `broadcast(dim: int, size: int) -> TensorView`

Expand a size-1 dimension by broadcasting (stride set to 0, same element repeated).

**Args:**
- `dim` (`int`): Dimension to broadcast (must currently have size 1)
- `size` (`int`): New size for the dimension

**Returns:** New `TensorView` with broadcasted dimension.

**Constraints:**
- `shape[dim]` must be 1
- For SBUF tensors, partition dim cannot be broadcast beyond `nl.tile_size.pmax`

```python
# shape [128, 1, 512] -> broadcast dim=1 to 8 -> shape [128, 8, 512]
view = TensorView(sbuf_tile_3d)
broadcasted = view.broadcast(1, 8)
```

---

### `reshape_dim(dim: int, shape: List[int]) -> TensorView`

Split a single dimension into multiple dimensions. Supports `-1` for one inferred dimension.

**Args:**
- `dim` (`int`): Dimension to reshape
- `shape` (`List[int]`): New sizes (product must equal original dim size; at most one `-1`)

**Returns:** New `TensorView` with reshaped dimension.

**Constraints:**
- Product of `shape` must equal `self.shape[dim]`
- For SBUF, partition dim (dim 0) cannot be reshaped (except trivially)

```python
# shape [128, 24, 512] -> reshape_dim(1, [2, 3, 4]) -> shape [128, 2, 3, 4, 512]
view = TensorView(sbuf_tile)
reshaped = view.reshape_dim(1, [2, -1, 4])  # -1 inferred as 3
```

---

### `flatten_dims(start_dim: int, end_dim: int) -> TensorView`

Flatten a contiguous range of dimensions into a single dimension.

**Args:**
- `start_dim` (`int`): First dimension to flatten (inclusive)
- `end_dim` (`int`): Last dimension to flatten (inclusive)

**Returns:** New `TensorView` with flattened dimensions.

**Constraints:**
- `start_dim < end_dim`
- Dimensions must be contiguous in memory
- For SBUF, `start_dim > 0` (cannot flatten partition dim)

```python
# shape [128, 2, 3, 4, 512] -> flatten_dims(1, 3) -> shape [128, 24, 512]
view = TensorView(sbuf_tile_5d)
flat = view.flatten_dims(1, 3)
```

---

### `expand_dim(dim: int) -> TensorView`

Insert a new dimension of size 1 at the specified position.

**Args:**
- `dim` (`int`): Position to insert (0 to get_dim() inclusive)

**Returns:** New `TensorView` with additional dimension.

**Constraints:**
- For SBUF, `dim > 0` (cannot expand before partition dim)

```python
# shape [128, 512] -> expand_dim(1) -> shape [128, 1, 512]
view = TensorView(sbuf_tile)
expanded = view.expand_dim(1)
```

---

### `squeeze_dim(dim: int) -> TensorView`

Remove a dimension that has size 1.

**Args:**
- `dim` (`int`): Dimension to remove (must have size 1)

**Returns:** New `TensorView` with dimension removed.

**Constraints:**
- `shape[dim]` must be 1
- For SBUF, `dim > 0`

```python
# shape [128, 1, 512] -> squeeze_dim(1) -> shape [128, 512]
view = TensorView(sbuf_tile_3d)
squeezed = view.squeeze_dim(1)
```

---

### `select(dim: int, index: Union[int, nl.ndarray]) -> TensorView`

Select a single element along a dimension, reducing dimensionality by one.

**Args:**
- `dim` (`int`): Dimension to select from
- `index` (`int` or `nl.ndarray`): Static integer index, or a scalar NKI tensor for dynamic indexing

**Returns:** New `TensorView` with one fewer dimension.

```python
# Static: shape [128, 8, 512] -> select(1, 3) -> shape [128, 512]
view = TensorView(sbuf_tile_3d)
selected = view.select(1, 3)

# Dynamic: use a scalar tensor for runtime index
idx_tensor = nl.ndarray((1, 1), dtype=nl.int32, buffer=nl.sbuf)
dynamic_selected = view.select(0, idx_tensor)
```

---

### `rearrange(src_pattern, dst_pattern, fixed_sizes=None) -> TensorView`

Einops-style dimension rearrangement combining reshape, permute, and flatten.

**Args:**
- `src_pattern` (`Tuple[Union[str, Tuple[str]]]`): Source dimension names. Tuples indicate grouped dims to split.
- `dst_pattern` (`Tuple[Union[str, Tuple[str]]]`): Destination dimension names. Tuples indicate dims to flatten.
- `fixed_sizes` (`Dict[str, int]`, optional): Known sizes for dimensions used in reshaping.

**Returns:** New `TensorView` with rearranged dimensions.

```python
# (batch, height*width, channels) -> (batch, channels, height, width)
view = TensorView(sbuf_tile)
rearranged = view.rearrange(
    ('p', ('h', 'w'), 'c'),
    ('p', 'c', 'h', 'w'),
    {'h': 32}
)
```

---

### `get_trivial_strides(shape: List[int], base_stride: int = 1) -> List[int]` (static)

Compute row-major (C-style) strides for a given shape.

**Args:**
- `shape` (`List[int]`): Dimension sizes
- `base_stride` (`int`): Stride of innermost dimension (default: 1)

**Returns:** `List[int]` of strides.

```python
strides = TensorView.get_trivial_strides([2, 3, 4])  # [12, 4, 1]
```

---

### `is_hbm() -> bool`

Check if the base tensor is in an HBM buffer (hbm, shared_hbm, or private_hbm).

**Returns:** `True` if the base tensor is in HBM.

```python
view = TensorView(hbm_tensor)
view.is_hbm()  # True

view = TensorView(sbuf_tensor)
view.is_hbm()  # False
```

---

### `reshape(new_shape: Tuple[int, ...]) -> TensorView`

Reshape the tensor to new dimensions without copying data. The total number of elements must be unchanged. Fails if the current memory layout is not compatible with the requested shape.

For non-HBM tensors (SBUF/PSUM), the partition dimension (dim 0) size must be preserved in the new shape. For HBM tensors, all dimensions participate in reshape.

The algorithm has three phases:
1. **Remove unit dims** -- strip size-1 dims whose strides are irrelevant
2. **Collapse contiguous** -- merge adjacent dims with contiguous strides into blocks
3. **Repartition** -- assign new strides by splitting/merging blocks to match `new_shape`

**Args:**
- `new_shape` (`Tuple[int, ...]`): New dimension sizes (total elements must match)

**Returns:** New `TensorView` with reshaped dimensions.

**Constraints:**
- Total element count must match between old and new shapes
- For non-HBM tensors, `new_shape[0]` must equal current `shape[0]`
- Layout must be compatible (fails if reshape would require a data copy)

```python
# Reshape a 2D tensor to 3D
view = TensorView(sbuf_tile)  # shape (128, 1024)
reshaped = view.reshape((128, 4, 256))  # split free dim
```

---

### `has_dynamic_access() -> bool`

Check if the view uses dynamic (indirect) indexing via a scalar offset tensor.

**Returns:** `True` if dynamic access is configured.

## Usage Examples

### Pattern 1: Slicing and permuting a loaded tile

```python
import nki.language as nl

# Allocate SBUF tile and create view
tile = nl.ndarray((128, 4, 256), dtype=nl.bfloat16, buffer=nl.sbuf)
view = TensorView(tile)

# Slice to first 2 heads, then transpose head and feature dims
sliced = view.slice(1, 0, 2)          # [128, 2, 256]
transposed = sliced.permute([0, 2, 1]) # [128, 256, 2]
result = transposed.get_view()          # nl.ndarray for NKI operations
```

### Pattern 2: Reshape for tiled matrix multiply

```python
import nki.language as nl

# Tile with fused dimensions: partition=128, free=2048
tile = nl.ndarray((128, 2048), dtype=nl.bfloat16, buffer=nl.sbuf)
view = TensorView(tile)

# Split free dim into (num_blocks=4, block_size=512) for tiled matmul
reshaped = view.reshape_dim(1, [4, 512])  # [128, 4, 512]

# Access each block
for block_idx in range(4):
    block = reshaped.select(1, block_idx)  # [128, 512]
    block_tensor = block.get_view()
    # Use block_tensor in nl.matmul(...)
```

### Pattern 3: Einops-style rearrangement for attention

```python
import nki.language as nl

# Multi-head attention tile: partition=128, free=num_heads*head_dim
tile = nl.ndarray((128, 1024), dtype=nl.bfloat16, buffer=nl.sbuf)
view = TensorView(tile)

# Rearrange from (p, num_heads*head_dim) to (p, head_dim, num_heads)
rearranged = view.rearrange(
    ('p', ('nh', 'hd')),
    ('p', 'hd', 'nh'),
    {'nh': 8}  # 8 heads, head_dim=128 inferred
)
result = rearranged.get_view()  # [128, 128, 8]
```

## Dependencies

- **kernel_assert** (`nkilib.core.utils.kernel_assert`): Used for runtime assertions throughout
- **logging** (`nkilib.core.utils.logging`): Logger instance for debug output

## Source

See `references/nkilib/core/utils/tensor_view.py` for the full implementation.
