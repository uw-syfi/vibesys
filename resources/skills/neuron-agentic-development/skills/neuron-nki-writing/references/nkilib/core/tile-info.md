# TiledDimInfo

## Overview

TiledDimInfo is a dataclass that encapsulates tiling metadata for a single dimension, including tile size, count, and optional subtile information. It provides methods for computing tile/subtile indices, bounds, and offsets -- useful for kernels that need structured two-level tiling with correct remainder handling.

## When to Use

Adopt TiledDimInfo when:
- **CTE-style kernel** with precomputed tile metadata that multiple functions query (tile counts, last-block sizes, subtile bounds)
- **Two-level tiling** with subtiles nested inside tiles — `build_with_subtiling()` precomputes both levels
- **Parameter structs** that carry tiling config — store a `TiledDimInfo` per tiled dimension instead of loose integers

**Skip when**: `TiledRange` is sufficient for iteration. TiledDimInfo is for *metadata storage and querying*, TiledRange is for *iteration*.

Used in 4 production kernels (output projection CTE, RMSNorm quant, MLP CTE tile info, MLP CTE transpose) where tiling metadata is built once and queried across multiple kernel phases.

## Quick Reference

| Method | Signature | Description |
|--------|-----------|-------------|
| `build` | `(tiled_dim_size, tile_size, subtile_info=None) -> TiledDimInfo` | Factory: create from dimension size and tile size |
| `build_with_subtiling` | `(tiled_dim_size, tile_size, subtile_size) -> TiledDimInfo` | Factory: create with two-level tiling |
| `is_subtiled` | `() -> bool` | Check if subtile info is present |
| `get_tile_indices` | `(tile_num, tile_offset) -> nl.ds` | Get NKI index slice for a tile |
| `get_subtile_indices` | `(tile_num, subtile_num, subtile_offset) -> nl.ds` | Get NKI index slice for a subtile |
| `get_subtile_start` | `(tile_idx, subtile_idx) -> int` | Absolute start position of a subtile |
| `get_local_subtile_start` | `(subtile_idx) -> int` | Local start position within a loaded tile |
| `get_subtile_bound` | `(tile_idx, subtile_idx) -> int` | Valid size of a subtile (handles remainder) |
| `get_local_subtile_bound` | `(tile_idx, subtile_idx) -> int` | Valid local size within loaded tile |
| `get_tile_bound` | `(tile_idx) -> int` | Valid size of a tile (handles remainder) |
| `get_actual_subtile_num` | `(tile_idx) -> int` | Number of subtiles in a given tile |

## Import Options

**Default** — inline the source into your kernel file.
Source: `references/nkilib/core/utils/tile_info.py`

**If nkilib is installed** in the user's environment:
```python
from nkilib.core.utils.tile_info import TiledDimInfo
```

## API Documentation

### `TiledDimInfo.build(tiled_dim_size: int, tile_size: int, subtile_info: TiledDimInfo = None) -> TiledDimInfo` (static)

Factory method to create a TiledDimInfo from dimension size and tile size.

**Args:**
- `tiled_dim_size` (`int`): Total size of the dimension being tiled
- `tile_size` (`int`): Size of each tile
- `subtile_info` (`TiledDimInfo`, optional): Nested subtile information

**Returns:** `TiledDimInfo` instance with `tile_count` computed via ceiling division.

```python
info = TiledDimInfo.build(1024, 256)
# info.tiled_dim_size=1024, info.tile_size=256, info.tile_count=4
```

---

### `TiledDimInfo.build_with_subtiling(tiled_dim_size: int, tile_size: int, subtile_size: int) -> TiledDimInfo` (static)

Factory method to create a TiledDimInfo with two-level tiling (tile + subtile).

**Args:**
- `tiled_dim_size` (`int`): Total size of the dimension
- `tile_size` (`int`): Size of each outer tile
- `subtile_size` (`int`): Size of each inner subtile within a tile

**Returns:** `TiledDimInfo` with nested `subtile_dim_info`.

```python
info = TiledDimInfo.build_with_subtiling(1024, 256, 64)
# info.tile_count=4, info.subtile_dim_info.tile_count=4
```

---

### `is_subtiled() -> bool`

Check whether this dimension has subtile information.

**Returns:** `True` if `subtile_dim_info` is not None.

---

### `get_tile_indices(tile_num, tile_offset) -> nl.ds`

Get an NKI dynamic slice for a given tile.

**Args:**
- `tile_num`: Tile number (0-based)
- `tile_offset`: Offset size for the slice

**Returns:** `nl.ds(tile_num * tile_size, tile_offset)`

```python
idx = info.get_tile_indices(2, 256)  # nl.ds(512, 256) for tile_size=256
```

---

### `get_subtile_indices(tile_num, subtile_num, subtile_offset) -> nl.ds`

Get an NKI dynamic slice for a specific subtile within a tile.

**Args:**
- `tile_num`: Outer tile number
- `subtile_num`: Subtile number within the tile
- `subtile_offset`: Offset size for the slice

**Returns:** `nl.ds(tile_start + subtile_start, subtile_offset)`

**Constraints:** Requires `is_subtiled() == True`

---

### `get_subtile_start(tile_idx, subtile_idx) -> int`

Calculate absolute start position for a subtile.

**Args:**
- `tile_idx`: Outer tile index
- `subtile_idx`: Subtile index within the tile

**Returns:** `tile_idx * tile_size + subtile_idx * subtile_size`

**Constraints:** Requires `is_subtiled() == True`

---

### `get_local_subtile_start(subtile_idx) -> int`

Calculate the local start position of a subtile within a loaded tile.

**Args:**
- `subtile_idx`: Subtile index

**Returns:** `subtile_idx * subtile_size`

**Constraints:** Requires `is_subtiled() == True`

---

### `get_subtile_bound(tile_idx, subtile_idx) -> int`

Calculate valid size of a subtile, clamped to the dimension boundary.

**Args:**
- `tile_idx`: Outer tile index
- `subtile_idx`: Subtile index

**Returns:** `min(tiled_dim_size - subtile_start, subtile_size)`

**Constraints:** Requires `is_subtiled() == True`

---

### `get_local_subtile_bound(tile_idx, subtile_idx) -> int`

Calculate valid local size of a subtile within a loaded tile.

**Args:**
- `tile_idx`: Outer tile index
- `subtile_idx`: Subtile index

**Returns:** `min(subtile_size, tile_bound - local_start)`

**Constraints:** Requires `is_subtiled() == True`

---

### `get_tile_bound(tile_idx) -> int`

Calculate valid size of a tile, clamped to the dimension boundary.

**Args:**
- `tile_idx`: Tile index

**Returns:** `min(tiled_dim_size - tile_start, tile_size)`

```python
info = TiledDimInfo.build(300, 128)
info.get_tile_bound(0)  # 128
info.get_tile_bound(2)  # 44 (remainder)
```

---

### `get_actual_subtile_num(tile_idx) -> int`

Calculate the actual number of subtiles in a given tile (handles partial tiles).

**Args:**
- `tile_idx`: Tile index

**Returns:** Ceiling division of `tile_bound / subtile_size`

**Constraints:** Requires `is_subtiled() == True`

## Usage Examples

### Pattern 1: Simple tiled iteration

```python
import nki.language as nl
from nkilib.core.utils.tile_info import TiledDimInfo

seq_len = 1024
tile_size = 256
dim_info = TiledDimInfo.build(seq_len, tile_size)

for tile_idx in range(dim_info.tile_count):
    bound = dim_info.get_tile_bound(tile_idx)
    idx = dim_info.get_tile_indices(tile_idx, bound)
    data = nl.ndarray((nl.par_dim(128), bound), dtype=input_tensor.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=data, src=input_tensor[partition_idx, idx])
```

### Pattern 2: Two-level tiling with subtiles

```python
import nki.language as nl
from nkilib.core.utils.tile_info import TiledDimInfo

# Tile 2048 elements: outer=512, inner=128
dim_info = TiledDimInfo.build_with_subtiling(2048, 512, 128)

for tile_idx in range(dim_info.tile_count):
    num_subtiles = dim_info.get_actual_subtile_num(tile_idx)
    for subtile_idx in range(num_subtiles):
        bound = dim_info.get_subtile_bound(tile_idx, subtile_idx)
        idx = dim_info.get_subtile_indices(tile_idx, subtile_idx, bound)
        data = nl.ndarray((nl.par_dim(128), bound), dtype=input_tensor.dtype, buffer=nl.sbuf)
        nisa.dma_copy(dst=data, src=input_tensor[partition_idx, idx])
```

### Pattern 3: Local subtile offsets for in-tile operations

```python
import nki.language as nl
from nkilib.core.utils.tile_info import TiledDimInfo

dim_info = TiledDimInfo.build_with_subtiling(1024, 256, 64)

for tile_idx in range(dim_info.tile_count):
    # Load the full tile into SBUF
    tile_bound = dim_info.get_tile_bound(tile_idx)
    tile_data = nl.ndarray((nl.par_dim(128), tile_bound), dtype=input_tensor.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=tile_data, src=input_tensor[partition_idx, dim_info.get_tile_indices(tile_idx, tile_bound)])

    # Process subtiles within the loaded tile
    for subtile_idx in range(dim_info.get_actual_subtile_num(tile_idx)):
        local_start = dim_info.get_local_subtile_start(subtile_idx)
        local_bound = dim_info.get_local_subtile_bound(tile_idx, subtile_idx)
        # Access tile_data[..., local_start:local_start+local_bound]
```

## Dependencies

- **kernel_assert** (`nkilib.core.utils.kernel_assert`): Used for subtile precondition checks
- **kernel_helpers** (`nkilib.core.utils.kernel_helpers`): Uses `get_ceil_quotient` for tile count calculation
- **nki.language** (`nl`): Uses `nl.ds` for dynamic slicing and `NKIObject` as base class

## Source

See `references/nkilib/core/utils/tile_info.py` for the full implementation.
