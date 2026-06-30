# TiledRange

## Overview

TiledRange divides a dimension into fixed-size tiles, handling remainder logic for the last tile automatically. It returns a tuple of `TiledRangeIterator` objects, each carrying its size, index, and absolute start offset -- making it easy to iterate over tiled dimensions in NKI kernels with correct boundary handling.

## When to Use

Adopt TiledRange when:
- **Tiling any dimension** where the size may not be evenly divisible by the tile size (remainder handling)
- **Nested tiling**: pass an outer `TiledRangeIterator` as input to create subtiles — avoids manual two-level remainder logic
- **Multiple tiled dimensions**: each `TiledRangeIterator` carries `.size`, `.start_offset`, `.index` so DMA copies use correct bounds

**Skip when**: the iteration count is compile-time constant and evenly divides, or a simple `nl.affine_range(N)` suffices.

Used in 8+ production kernels including cumsum, RMSNorm, router TopK, and MLP projections (with up to 20+ call sites in complex kernels like MLP CTE).

## Quick Reference

| Name | Signature | Description |
|------|-----------|-------------|
| `TiledRange` | `(size, tile_size: int) -> Tuple[TiledRangeIterator, ...]` | Divide a dimension into tiles and return iterators |
| `TiledRangeIterator` | `(tile_size, tile_index, start_offset, end_offset)` | Single tile with `.size`, `.index`, `.start_offset`, `.end_offset` properties |
| `TiledRangeIterator.__repr__` | `() -> str` | String representation for debugging |

## Import Options

**Default** — inline the source into your kernel file.
Source: `references/nkilib/core/utils/tiled_range.py`

**If nkilib is installed** in the user's environment:
```python
from nkilib.core.utils.tiled_range import TiledRange, TiledRangeIterator
```

## API Documentation

### `TiledRange(size, tile_size: int) -> Tuple[TiledRangeIterator, ...]`

Divide a dimension into tiles and return a tuple of iterators.

**Args:**
- `size` (`int` or `TiledRangeIterator`): Total size to tile, or a `TiledRangeIterator` for nested (sub)tiling
- `tile_size` (`int`): Size of each tile

**Returns:** Tuple of `TiledRangeIterator` objects. The last tile may be smaller than `tile_size` if the dimension is not evenly divisible.

**Constraints:**
- `tile_size` should be > 0
- When `size` is a `TiledRangeIterator`, tiling operates on that tile's `.size` and offsets are computed relative to the parent tile's `.start_offset`

```python
# Tile dimension of size 300 into tiles of 128
tiles = TiledRange(300, 128)
# tiles[0]: size=128, index=0, start_offset=0,   end_offset=128
# tiles[1]: size=128, index=1, start_offset=128, end_offset=256
# tiles[2]: size=44,  index=2, start_offset=256, end_offset=300
```

---

### `TiledRangeIterator(tile_size: int, tile_index: int, start_offset: int, end_offset: int)`

Represents a single tile in a tiled range.

**Attributes:**
- `size` (`int`): Size of this tile (may be < tile_size for last tile)
- `index` (`int`): 0-based index of this tile in the range
- `start_offset` (`int`): Absolute starting offset in the original dimension
- `end_offset` (`int`): Absolute ending offset in the original dimension

**Returns:** `TiledRangeIterator` instance.

```python
tile = TiledRangeIterator(128, 0, 0, 128)
print(tile.size)          # 128
print(tile.index)         # 0
print(tile.start_offset)  # 0
print(tile.end_offset)    # 128
```

## Usage Examples

### Pattern 1: Simple tiled iteration over a free dimension

```python
import nki.language as nl
from nkilib.core.utils.tiled_range import TiledRange

# Iterate over a 1024-element free dimension in tiles of 512
for tile in TiledRange(1024, 512):
    # tile.size=512 for both tiles, tile.start_offset=0 then 512
    data = nl.ndarray((nl.par_dim(128), tile.size), dtype=input_tensor.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=data, src=input_tensor[partition_idx, tile.start_offset:tile.start_offset + tile.size])
    # ... process data
```

### Pattern 2: Tiled iteration with remainder handling

```python
import nki.language as nl
from nkilib.core.utils.tiled_range import TiledRange

seq_len = 300
tile_size = 128

for tile in TiledRange(seq_len, tile_size):
    # tile 0: size=128, offset=0
    # tile 1: size=128, offset=128
    # tile 2: size=44,  offset=256 (remainder)
    if tile.size < tile_size:
        # Handle partial tile (e.g., mask or use nl.ds with tile.size)
        idx = nl.ds(tile.start_offset, tile.size)
    else:
        idx = nl.ds(tile.start_offset, tile_size)
    # Use idx for load/store operations
```

### Pattern 3: Nested (sub)tiling for two-level tiling

```python
import nki.language as nl
from nkilib.core.utils.tiled_range import TiledRange

# Two-level tiling: outer=128, inner=64
for outer_tile in TiledRange(300, 128):
    for inner_tile in TiledRange(outer_tile, 64):
        # inner_tile offsets are absolute (relative to original dimension)
        # outer tile 0, inner tiles: (size=64, offset=0), (size=64, offset=64)
        # outer tile 2, inner tile:  (size=44, offset=256) -- single subtile
        idx = nl.ds(inner_tile.start_offset, inner_tile.size)
        # Use idx for fine-grained access within the outer tile
```

## Dependencies

- **nki.language** (`NKIObject`): Base class for NKI-compatible objects
- **math** (standard library): Used for `math.ceil` in tile count calculation

No dependencies on other nkilib files.

## Source

See `references/nkilib/core/utils/tiled_range.py` for the full implementation.
