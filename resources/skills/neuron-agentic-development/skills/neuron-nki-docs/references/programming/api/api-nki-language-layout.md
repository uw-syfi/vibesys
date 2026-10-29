# nki.language — Tiled & Blocked Layouts

> **Module**: nki.language
> **Functions**: 8

## Overview

Tiled & Blocked Layouts instructions.

## Functions

### nki.language.NkiBlockGrid {#nki-language-NkiBlockGrid}

`nki.language.NkiBlockGrid()`

**Signature:**
```python
language.NkiBlockGrid
```

A tensor view navigated in block coordinates.

A block groups a fixed number of tiles along every tiled dimension.
`NkiGrid.shape` counts blocks, `block_shape` gives tiles per
block, and `NkiGrid.tile_shape` gives source elements per tile. This
separates a coarser transfer or reuse grain from the tile grain consumed by
compute instructions.

Partial final blocks and tiles are retained and clamped to the exact source
extent. Creating and indexing a block view allocates no storage and moves
no data.

Examples:

```python
import nki
import nki.isa as nisa
import nki.language as nl

@nki.jit
def copy_block_grid(src):
    tile_shape = (128, 256)
    block_shape = (2, 2)
    src_blocks = nl.blocks(
        src, tile_shape=tile_shape, block_shape=block_shape
    )
    dst = nl.ndarray(
        src.shape, dtype=src.dtype, buffer=nl.shared_hbm
    )
    dst_blocks = nl.blocks(
        dst, tile_shape=tile_shape, block_shape=block_shape
    )

    for i in range(src_blocks.shape[0]):
        for j in range(src_blocks.shape[1]):
            src_block = src_blocks[i, j]
            block = nl.blocks(
                nl.ndarray(
                    src_block.element_shape,
                    dtype=src.dtype,
                    buffer=nl.sbuf,
                ),
                tile_shape=src_block.tile_shape,
                block_shape=src_block.block_shape,
            )
            nisa.dma_copy(dst=block.data, src=src_block.data)
            nisa.dma_copy(
                dst=dst_blocks[i, j].data,
                src=block.data,
            )
    return dst

```

See Also:
    `nki.language.blocks`: Create or regroup a block grid.

**Attributes**

| Name | Description |
| --- | --- |
| `block_shape` | Return the nominal number of tiles grouped along each block axis. |

---

### nki.language.NkiGrid {#nki-language-NkiGrid}

`nki.language.NkiGrid()`

**Signature:**
```python
language.NkiGrid
```

Base class for tensor views organized into tiles or blocks.

`NkiGrid` is the common interface for `NkiTileGrid` and
`NkiBlockGrid`. A grid describes an existing tensor region through
logical tile or block coordinates while retaining its dtype, buffer, and
underlying storage.

Users do not construct `NkiGrid` directly. Grid views are created via
`nki.language.tiles` and `nki.language.blocks`:

```python
tile_grid = nl.tiles(src, tile_shape=(128, 256))
block_grid = nl.blocks(
    src,
    tile_shape=(128, 256),
    block_shape=(2, 2),
)

```

`nki.language.tiles` returns an `NkiTileGrid`, and
`nki.language.blocks` returns an `NkiBlockGrid`. Both concrete
objects are instances of `NkiGrid`. Refer to those factory functions for
the tiling, block-grouping, and boundary behavior of the returned view.

See Also:
    `nki.language.tiles`: Create a tile-coordinate view.
    `nki.language.blocks`: Group tiles into block coordinates.

**Attributes**

| Name | Description |
| --- | --- |
| `shape` | Return the extents of the grid coordinates available for indexing. |
| `tile_shape` | Return the nominal source-element shape of one tile. |
| `element_shape` | Return the number of selected elements along each source dimension. |
| `dtype` | Return the element dtype of the backing tensor. |
| `buffer` | Return the memory region containing the backing tensor. |
| `is_remainder` | Return whether this selection contains a partial boundary tile. |
| `is_data_representable` | Return whether `data` can represent this selection exactly. |
| `index_step_elements` | Return each grid-coordinate step in source-element units. |
| `data` | Expose the complete grid selection as one native tensor operand. |

---

### nki.language.NkiTileGrid {#nki-language-NkiTileGrid}

`nki.language.NkiTileGrid()`

**Signature:**
```python
language.NkiTileGrid
```

A tensor view navigated in tile coordinates.

Each tiled entry in `NkiGrid.shape` counts tiles, while
`NkiGrid.tile_shape` gives the nominal source-element shape of one
tile. Untiled HBM dimensions form a leading prefix and are indexed one slab
at a time.

Integer indexing consumes coordinates but retains `NkiTileGrid`.
`NkiGrid.data` exposes the exact `NkiTensor` operand for the
selected view.

Instances come from `nki.language.tiles` or a block-to-tile
transition. Creating and indexing the view allocates no storage and moves
no data.

Examples:

```python
import nki
import nki.isa as nisa
import nki.language as nl

@nki.jit
def copy_tile_grid(src):
    tile_shape = (128, 256)
    src_tiles = nl.tiles(src, tile_shape=tile_shape)
    dst = nl.ndarray(
        src.shape, dtype=src.dtype, buffer=nl.shared_hbm
    )
    dst_tiles = nl.tiles(dst, tile_shape=tile_shape)

    for i in range(src_tiles.shape[0]):
        for j in range(src_tiles.shape[1]):
            src_tile = src_tiles[i, j]
            tile = nl.ndarray(
                src_tile.element_shape,
                dtype=src.dtype,
                buffer=nl.sbuf,
            )
            nisa.dma_copy(dst=tile.data, src=src_tile.data)
            nisa.dma_copy(
                dst=dst_tiles[i, j].data,
                src=tile.data,
            )
    return dst

```

See Also:
    `NkiBlockGrid`: A grid whose cells group multiple tiles.
    `nki.language.tiles`: Create or retile a tile grid.

---

### nki.language.alloc_blocks {#nki-language-alloc_blocks}

`nki.language.alloc_blocks(tile_shape, block_shape, element_shape, buffer, dtype, address, name)`

**Engine:** DMA Engine

**Signature:**
```python
language.alloc_blocks(tile_shape, block_shape, element_shape, buffer, dtype, address=None, name='')
```

Allocate new storage organized as a grid of tile blocks.

`alloc_blocks` creates the same tile geometry as
`nki.language.alloc_tiles`, then adds a block-coordinate level above
it. `tile_shape` defines the compute grain, `block_shape` defines the
number of tiles in one transfer or reuse grain, and `element_shape`
remains the exact logical tensor extent.

Block grouping does not change physical tile order or add per-block
allocations. HBM uses the compact logical extent as backing storage; SBUF
and PSUM use the same dense rank-two capacity layout as
`nki.language.alloc_tiles`.

The returned `NkiBlockGrid` owns one independent uninitialized allocation.

- **tile_shape** — The positive source-element shape of one compute tile. It
    must have at least two dimensions and applies to the trailing
    dimensions of `element_shape`.
- **block_shape** — The positive tile counts grouped along each block
    dimension. Its rank must equal `tile_shape`.
- **element_shape** — The exact positive logical element extent. Any
    dimensions preceding `tile_shape` remain untiled.
- **buffer** — Destination `MemoryRegion`, such as
    `nl.shared_hbm`, `nl.sbuf`, or `nl.psum`.
- **dtype** — Destination NKI element dtype.
- **address** — Optional manual address
    `(partition_offset, free_offset)` with the same contract as
    `nki.language.ndarray`.
- **name** — the name of the tensor, used in scheduling.
**Returns:** A new `NkiBlockGrid` over independent, uninitialized
    storage. Partial boundary blocks and tiles retain exact logical extents;
    aggregate `data` access requires one exact native access pattern.
- **Raises `TypeError`** — if a shape is not a tuple of exact Python integers or
    `buffer` is not an NKI `MemoryRegion`.
- **Raises `ValueError`** — if a shape entry is nonpositive, `tile_shape` has fewer
    than two dimensions or greater rank than `element_shape`,
    `block_shape` and `tile_shape` ranks differ, or `buffer` is
    unsupported.
- **Raises `AssertionError`** — if the native allocation violates a
    `nki.language.ndarray` constraint, including PSUM address
    alignment.

Examples:

```python
import nki
import nki.isa as nisa
import nki.language as nl

@nki.jit
def initialize_block_grid():
    staging = nl.alloc_blocks(
        tile_shape=(128, 128),
        block_shape=(2, 2),
        element_shape=(512, 512),
        buffer=nl.sbuf,
        dtype=nl.bfloat16,
    )
    dst = nl.ndarray(
        (512, 512), dtype=nl.bfloat16, buffer=nl.shared_hbm
    )
    dst_blocks = nl.blocks(
        dst,
        tile_shape=(128, 128),
        block_shape=(2, 2),
    )

    nisa.memset(dst=staging.data, value=0.0)
    nisa.dma_copy(dst=dst_blocks.data, src=staging.data)
    return dst

```

See Also:
    `nki.language.blocks`: Group tiles in existing storage.
    `nki.language.alloc_tiles`: Allocate a tile grid without blocks.

---

### nki.language.alloc_tiles {#nki-language-alloc_tiles}

`nki.language.alloc_tiles(tile_shape, element_shape, buffer, dtype, address, name)`

**Engine:** DMA Engine

**Signature:**
```python
language.alloc_tiles(tile_shape, element_shape, buffer, dtype, address=None, name='')
```

Allocate independent, uninitialized storage organized as a grid of tiles.

The returned `NkiTileGrid` owns one backing allocation and provides
both tile navigation and an aggregate `NkiGrid.data` operand.

- **tile_shape** — The positive source-element shape of one tile. It must be
    a tuple containing at least two exact Python integers and applies to
    the trailing dimensions of `element_shape`.
- **element_shape** — The exact positive logical element extent.
- **buffer** — Destination `MemoryRegion`, such as
    `nl.shared_hbm`, `nl.sbuf`, or `nl.psum`.
- **dtype** — Destination NKI element dtype.
- **address** — Optional manual address
    `(partition_offset, free_offset)` with the same contract as
    `nki.language.ndarray`.
- **name** — the name of the tensor, used in scheduling.
**Returns:** A new `NkiTileGrid` over independent, uninitialized
    storage. Its `shape` counts tiles and its boundary selections expose
    their exact clamped extents.
- **Raises `TypeError`** — if a shape is not a tuple of exact Python integers or
    `buffer` is not an NKI `MemoryRegion`.
- **Raises `ValueError`** — if a shape entry is nonpositive, `tile_shape` has fewer
    than two dimensions or greater rank than `element_shape`, or
    `buffer` is unsupported.
- **Raises `AssertionError`** — if the native allocation violates a
    `nki.language.ndarray` constraint, including PSUM address
    alignment.

Examples:

```python
import nki
import nki.isa as nisa
import nki.language as nl

@nki.jit
def initialize_tile_grid():
    staging = nl.alloc_tiles(
        tile_shape=(128, 256),
        element_shape=(256, 512),
        buffer=nl.sbuf,
        dtype=nl.bfloat16,
    )
    dst = nl.ndarray(
        (256, 512), dtype=nl.bfloat16, buffer=nl.shared_hbm
    )
    dst_tiles = nl.tiles(dst, tile_shape=(128, 256))

    nisa.memset(dst=staging.data, value=0.0)
    nisa.dma_copy(dst=dst_tiles.data, src=staging.data)
    return dst

```

See Also:
    `nki.language.tiles`: View existing storage as a tile grid.
    `nki.language.alloc_blocks`: Allocate tiles grouped into blocks.

---

### nki.language.blocks {#nki-language-blocks}

`nki.language.blocks(source, tile_shape, block_shape)`

**Engine:** DMA Engine

**Signature:**
```python
language.blocks(source, tile_shape=None, block_shape)
```

Create a block view over a tensor where each block contains multiple tiles.

`blocks()` adds a second level of grouping above the tile grid. A block is
a rectangular group of tiles intended as a coarser transfer or reuse grain,
while `tile_shape` remains the compute grain presented to `nisa.*`
operations. When one native access pattern can represent the complete
block, `block.data` allows it to participate in one aggregate operation.

`block_shape` is expressed in tile counts. For a `4 x 4` tile grid,
`block_shape=(2, 2)` creates a `2 x 2` block grid:

```text
block grid: 2 x 2
+-------------------+-------------------+
| tile (0,0) (0,1)  | tile (0,2) (0,3)  |
| tile (1,0) (1,1)  | tile (1,2) (1,3)  |
+-------------------+-------------------+
| tile (2,0) (2,1)  | tile (2,2) (2,3)  |
| tile (3,0) (3,1)  | tile (3,2) (3,3)  |
+-------------------+-------------------+

```

Blocks are logical views over the source. Creating, regrouping, slicing,
or indexing a block grid allocates no storage, copies no data, and emits no
instruction.

> **Note:**
>
> Indexing preserves `NkiBlockGrid`. Call
> `nki.language.tiles` on a selected block to expose its interior
> tile coordinates, then use `NkiGrid.data` when an instruction
> requires one native tensor operand.
>
> A block containing nonuniform boundary tiles may need to be split into
> uniform tile regions first.
>

- **source** — A direct `NkiTensor`, `NkiTileGrid`, or
    `NkiBlockGrid` whose storage and selected addresses are shared by
    the returned view.
- **tile_shape** — The nominal source-element shape of one compute tile.
    Required for an `NkiTensor`; optional for an existing grid.
- **block_shape** — The positive tile counts grouped along each block
    dimension. It must be a tuple with the same rank as the effective
    `tile_shape`.
**Returns:** An `NkiBlockGrid` over the same storage. Its `shape`
    counts blocks, while `tile_shape` and `block_shape` preserve both
    levels of the hierarchy. Partial boundary blocks and tiles are retained
    at their exact logical extents; aggregate `data` access still requires
    one exact native access pattern.
- **Raises `TypeError`** — if `source` is unsupported, `tile_shape` is omitted
    for an `NkiTensor`, or a shape is not a tuple of exact Python
    integers.
- **Raises `ValueError`** — if a shape entry is nonpositive, `block_shape` and
    `tile_shape` have different ranks, the source cannot be tiled, or
    retiling or grouping cannot preserve the selected addresses.

Examples:

```python
import nki
import nki.isa as nisa
import nki.language as nl

@nki.jit
def scale_by_block(src):
    dst = nl.ndarray(
        src.shape, dtype=src.dtype, buffer=nl.shared_hbm
    )
    src_blocks = nl.blocks(
        src,
        tile_shape=(128, 256),
        block_shape=(2, 2),
    )
    dst_blocks = nl.blocks(
        dst,
        tile_shape=(128, 256),
        block_shape=(2, 2),
    )

    for i in range(src_blocks.shape[0]):
        for j in range(src_blocks.shape[1]):
            src_block = src_blocks[i, j]
            dst_block = dst_blocks[i, j]
            block = nl.blocks(
                nl.ndarray(
                    src_block.element_shape,
                    dtype=src.dtype,
                    buffer=nl.sbuf,
                ),
                tile_shape=src_block.tile_shape,
                block_shape=src_block.block_shape,
            )

            # copy entire block in single instruction
            nisa.dma_copy(dst=block.data, src=src_block.data)

            # demote to tile-grid to access individual tiles
            block_tiles = nl.tiles(block)
            for k in range(block_tiles.shape[0]):
                for p in range(block_tiles.shape[1]):
                    tile = block_tiles[k, p].data
                    nisa.tensor_scalar(
                        dst=tile,
                        data=tile,
                        op0=nl.multiply,
                        operand0=2.0,
                    )

            nisa.dma_copy(
                dst=dst_block.data,
                src=block.data,
            )

    return dst

```

**Source kinds.**

The source can be `NkiTensor` or `NkiTileGrid` or `NkiBlockGrid`.

```text
source              tile_shape          behavior
------------------  -----------------  ------------------------------
NkiTensor           required           tile, then group
NkiTileGrid         omitted            group existing tiles
NkiTileGrid         supplied           retile, then group
NkiBlockGrid        omitted            remove old groups, regroup
NkiBlockGrid        supplied           remove groups, retile, regroup

```

**Remainder blocks.**

Tile counts are ceiling-divided from the source extent, and block counts
are ceiling-divided from the tile counts. A final block can therefore
contain fewer tiles, and its final tile can contain fewer source elements.
Both levels are clamped to the exact source extent.

See Also:
    `nki.language.tiles`: Create a tile grid or expose block tiles.
    `NkiBlockGrid`: Block-grid metadata and indexing behavior.

---

### nki.language.empty_like {#nki-language-empty_like}

`nki.language.empty_like(x, dtype, buffer, name, transpose_axes, address)`

**Engine:** DMA Engine

**Signature:**
```python
language.empty_like(x, dtype=None, buffer=None, name='', transpose_axes=None, address=None)
```

Allocate independent, uninitialized storage with matching geometry.

The destination uses the source dtype and buffer unless either is
specified explicitly. Tensor inputs preserve the source shape. Grid inputs
preserve the concrete grid type, navigation shape, tile shape, block
grouping, and exact remainder extents.

A positive-step grid slice can select cells separated by physical gaps.
The destination packs those cells densely while preserving their logical
hierarchy and boundary extents.

`transpose_axes` prepares an SBUF destination for a later
`nki.isa.dma_transpose`; allocation does not issue the instruction.
Supported tiled-dimension orders are `(1, 0)`, `(2, 1, 0)`, and
`(3, 1, 2, 0)` for ranks two through four. The source free extent that
becomes the destination partition extent must not exceed 128.

- **x** — The `NkiTensor`, `NkiTileGrid`, or
    `NkiBlockGrid` whose geometry defines the allocation.
- **dtype** — Destination element dtype. Defaults to `x.dtype`.
- **buffer** — Destination `MemoryRegion`. Defaults to `x.buffer`.
- **name** — the name of the tensor, used in scheduling.
- **transpose_axes** — Optional tiled-dimension order for a native
    `nki.isa.dma_transpose` destination.
- **address** — Optional manual address
    `(partition_offset, free_offset)` with the same contract as
    `nki.language.ndarray`.
**Returns:** New uninitialized storage with the matching tensor or grid
    abstraction. Transpose allocation permutes the tiled dimensions.
- **Raises `TypeError`** — if `x` is unsupported, `buffer` is not an NKI
    `MemoryRegion`, or `transpose_axes` is not a tuple.
- **Raises `ValueError`** — if transpose rank, order, destination buffer, or
    partition extent is unsupported.
- **Raises `AssertionError`** — if manual `address` validation in
    `nki.language.ndarray` fails.

See Also:
    `nki.language.alloc_tiles`: Allocate from explicit tile geometry.
    `nki.language.alloc_blocks`: Allocate from explicit block geometry.

---

### nki.language.tiles {#nki-language-tiles}

`nki.language.tiles(source, tile_shape)`

**Engine:** DMA Engine

**Signature:**
```python
language.tiles(source, tile_shape=None)
```

Decompose a tensor into a logical grid of fixed-size tiles.

The returned grid is a view over `source`, indexed by
tile coordinate rather than by manually calculated element offsets and
strides. A kernel can select `src_tiles[i, j]` to address one tile,
`src_tiles[i, :]` to address a row of tiles, or `src_tiles[:, j]` to
address a column.

For example, `tile_shape=(128, 256)` over a `(512, 1024)` tensor
produces a `4 x 4` grid:

```text
                              F (free dimension)
                   256       256       256       256
              +---------+---------+---------+---------+
P         128 |  (0,0)  |  (0,1)  |  (0,2)  |  (0,3)  |
(partition)    +---------+---------+---------+---------+
          128 |  (1,0)  |  (1,1)  |  (1,2)  |  (1,3)  |
              +---------+---------+---------+---------+
          128 |  (2,0)  |  (2,1)  |  (2,2)  |  (2,3)  |
              +---------+---------+---------+---------+
          128 |  (3,0)  |  (3,1)  |  (3,2)  |  (3,3)  |
              +---------+---------+---------+---------+

```

> **Note:**
>
> Grid indexing preserves the grid abstraction. `src_tiles[i, j]`
> returns a one-tile `NkiTileGrid`, not an `NkiTensor`.
> Use `src_tiles[i, j].data` when an NKI instruction requires the
> underlying tensor operand. Accessing `data` is also a view operation.
>

- **source** — The tensor region to tile. It may be a direct
    `NkiTensor`, an existing `NkiTileGrid`, or an
    `NkiBlockGrid`. The returned grid shares its storage, dtype,
    buffer, offsets, and physical strides.
- **tile_shape** — The nominal shape of one tile in source-element units,
    with at least two dimensions. Every entry must be a positive, exact
    Python integer. It is required when `source` is an
    `NkiTensor` and optional when `source` is already a grid.
    For an HBM tensor, fewer dimensions than `source` leaves the leading
    dimensions as batch coordinates. A tile extent may be larger than its
    source extent, producing one partial tile.
**Returns:** An `NkiTileGrid` over `source`. Its `shape` counts
    addressable tiles, its `tile_shape` records the nominal
    `tile_shape`, and its `element_shape` records the number of selected
    elements in each source dimension. Partial boundary tiles expose only
    valid elements, although independently allocated on-chip storage may
    reserve nominal tile capacity behind them.
- **Raises `TypeError`** — if `source` is unsupported, `tile_shape` is omitted
    for an `NkiTensor`, a shape is not a tuple of exact Python
    integers, or the source buffer is not an NKI `MemoryRegion`.
- **Raises `ValueError`** — if `tile_shape` has fewer than two dimensions,
    contains a nonpositive extent, has a rank incompatible with the source,
    the source is an indirect `NkiTensor`, the source buffer is
    unsupported, or an existing grid cannot be retiled while preserving
    its selected addresses.

Examples:

```python
import nki
import nki.isa as nisa
import nki.language as nl

@nki.jit
def scale_by_tile(src):
    dst = nl.ndarray(
        src.shape, dtype=src.dtype, buffer=nl.shared_hbm
    )

    src_tiles = nl.tiles(src, tile_shape=(128, 256))
    dst_tiles = nl.tiles(dst, tile_shape=(128, 256))

    for i in range(src_tiles.shape[0]):
        for j in range(src_tiles.shape[1]):
            src_tile = src_tiles[i, j]
            dst_tile = dst_tiles[i, j]
            tile = nl.ndarray(
                src_tile.element_shape,
                dtype=src.dtype,
                buffer=nl.sbuf,
            )

            nisa.dma_copy(dst=tile.data, src=src_tile.data)
            nisa.tensor_scalar(
                dst=tile.data, data=tile.data, op0=nl.multiply, operand0=2.0
            )
            nisa.dma_copy(dst=dst_tile.data, src=tile.data)

    return dst

```

**Source kinds.**

The source can be `NkiTensor` or `NkiTileGrid` or `NkiBlockGrid`.

```text
source              tile_shape          result
------------------  -----------------  ------------------------------
HBM NkiTensor       required           tile grid over HBM
SBUF NkiTensor      required           tile grid over SBUF
PSUM NkiTensor      required           tile grid over PSUM
NkiTileGrid         omitted            same tile-grid view
NkiTileGrid         supplied           address-preserving retile
NkiBlockGrid        omitted            remove block grouping
NkiBlockGrid        supplied           remove blocks, then retile

```

Both `NkiBlockGrid` forms remove the block-coordinate level.
Omitting `tile_shape` preserves the existing tile structure. Supplying it
rebuilds the tile grid at the requested compute grain. See **Retiling** section
below.

**Remainder tiles.**

Grid dimensions use ceiling division. When a source extent is not an exact
multiple of its tile extent, the final tile is clamped to the remaining
elements. For `source.shape == (300, 600)` and
`tile_shape == (128, 256)`:

```text
                          column 0       column 1      column 2
                          256 elements   256 elements   88 elements
                        +--------------+--------------+-----------+
    row 0: 128 elements |  128 x 256   |  128 x 256   | 128 x 88 |
                        +--------------+--------------+-----------+
    row 1: 128 elements |  128 x 256   |  128 x 256   | 128 x 88 |
                        +--------------+--------------+-----------+
    row 2:  44 elements |   44 x 256   |   44 x 256   |  44 x 88 |
                        +--------------+--------------+-----------+

```

The nominal `tile_shape` remains `(128, 256)`, while
`src_tiles[-1, -1].data.shape` is `(44, 88)`. The selected boundary
tile reports `is_remainder == True`. Its logical extent and `data`
operand contain no padding.

```python
# check if last row/col tiles are partials
src_tiles[-1].is_remainder
src_tiles[:, -1].is_remainder
src_tiles[-1, -1].is_remainder

```

See Also:
    `nki.language.blocks`: Group tiles into blocks.
    `NkiTileGrid`: Tile-grid metadata and indexing.
    `NkiGrid.data`: Access a selected region as an NKI tensor.

---

