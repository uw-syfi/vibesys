# Choosing `tile_shape` and `block_shape`

`references/indexing-patterns.md` covers the **syntax** of `nl.tiles`, `nl.blocks`,
`nl.alloc_tiles` and `nl.alloc_blocks`. This file covers how to pick the **numbers** you pass
them.

These are authoring heuristics, not compilation rules — a kernel that ignores them can still
compile and pass numerics. What it cannot do is stay readable or re-tune for a new shape. The
failure this guide prevents: picking a tile shape off the tensor's dimensions
(`tile_shape=(128, 2048)` because the tensor is 2048 wide), then re-tiling and reshaping further
down the kernel to feed each actual instruction.

## The grain rule

**`tile_shape` is set by the ISA instruction that consumes the tile — never by the tensor's
logical width.** Work backwards from the consuming `nisa.*` op and size the tile so the loaded
tile feeds that op directly, with no re-tiling in between.

**`block_shape` is the data-movement and grouping grain.** Because `tile_shape` is pinned by the
op, the block level is where you adapt to SBUF budget, core count and DMA efficiency. It is the
knob you tune. Note `block_shape` is expressed in **tile counts**, not elements.

A whole logical dimension — full hidden, full sequence, full `n_heads * d_head`, full fused-QKV
intermediate — appearing anywhere inside a `tile_shape=` is the smell. That dimension belongs at
the block level. A full-dimension tile either exceeds the consuming op's per-operand limit
(forcing the re-tile you were trying to avoid) or wastes SBUF.

This applies to **outputs and accumulators too**, not just loaded inputs. An
`nl.alloc_tiles(tile_shape=...)` PSUM accumulator is bound by the limits of the op that writes
it.

## Size each axis to the op's per-operand limit

The right free extent depends on *which operand of which op* the tile feeds. For
`nisa.nc_matmul` the two operands differ: the **stationary** operand is limited to `128 x 128`,
the **moving** operand reaches `128 x 512`.

Prefer the named constants over literal 128/512 — they self-document and track the target:

```python
TILE_K = nl.tile_size.pmax                   # 128, the partition axis
TILE_N = nl.tile_size.gemm_moving_fmax       # 512, matmul moving operand
TILE_M = nl.tile_size.gemm_stationary_fmax   # 128, matmul stationary operand

moving_tiles     = nl.tiles(x, tile_shape=(TILE_K, TILE_N))
stationary_tiles = nl.tiles(w, tile_shape=(TILE_K, TILE_M))
```

### Where the real limits are written down

Do not take the limit from this file — look up the op you are actually calling. Limits differ per
op, per operand, and per hardware generation. In `/neuron-nki-docs`:

| What you need | Where |
|---|---|
| The constant table — `pmax`, `gemm_stationary_fmax`, `gemm_moving_fmax`, `psum_bank_fmax`, `sbuf_fmax`, `psum_num_banks`, alignment minimums | `programming/api/api-nki-language-dims.md#nki-language-tile_size` |
| Per-operand limits for a specific ISA op | that op's entry on its engine's page (below) — the limits are stated in the op's own text, not centrally |
| Tensor Engine — `nc_matmul`, `nc_matmul_mx`, `nc_stream_shuffle`, `nc_find_index8` | `programming/api/api-nki-isa-tensor.md` |
| DMA / layout — `dma_copy`, `dma_transpose`, `nc_transpose`, `tensor_copy`, `memset`, `local_gather` | `programming/api/api-nki-isa-memory.md` |
| Scalar Engine — `activation`, `activate2`, `activation_reduce` | `programming/api/api-nki-isa-scalar.md` |
| Vector Engine — `bn_stats`, `bn_aggr` | `programming/api/api-nki-isa-vector.md` |
| PSUM free-dimension limit by generation | `architecture/trainium_inferentia2_arch.md` (NeuronCore-v2/v3), `architecture/trainium3_arch.md` (v4) |

`nc_matmul` is the worked example of why the op page matters:
`api-nki-isa-tensor.md#nki-isa-nc_matmul` states that `stationary`'s free dimension must not exceed
128, that `moving`'s must not exceed 512 **on NeuronCore-v2 and -v3**, and that the partition
dimension of both must be identical and one of 32, 64 or 128. The 512 is generation-scoped — do
not carry it to a v4 target; check the op page and the architecture page for that generation
instead.

```python
# BAD: tile shape taken from the tensor
out_tiles = nl.tiles(out, tile_shape=(TILE_S, I))     # full fused-QKV intermediate
hid_tiles = nl.tiles(hid, tile_shape=(128, H))        # full hidden dim
# Each tile must now be re-sliced per op, or it blows the SBUF/PSUM budget.

# GOOD: tile shape taken from the consuming op; the big dim moves up to the block level
out_blocks = nl.blocks(out, tile_shape=(TILE_S, TILE_N), block_shape=(1, I // TILE_N))
```

## Write two loop levels: blocks outside, tiles inside

Loop over blocks on the outside, and over the tiles a loaded block exposes on the inside. This
is what lets one kernel re-tune for a different input shape, TP degree, model dimension or SBUF
budget by changing `block_shape` alone — without touching the compute loop. A kernel that bakes
the data-movement grain into the compute loop cannot be tuned that way.

```python
# outer: blocks -- the tunable knob, one kernel serving many configs
for bk in range(x_blocks.shape[0]):
    x_blk = x_blocks[bk, bn].load()        # one coalesced DMA at the block grain
    w_blk = w_blocks[bk, bn].load()        # SAME bk drives the coupled weight, in lock-step
    # inner: the tiles the loaded block exposes -- no k*TILE offset math
    for kt in range(x_blk.shape[0]):
        nisa.nc_matmul(psums[...].data, x_blk[kt].data, w_blk[kt, ...].data)
```

Two tensors stepped by the same loop variable should share the same `block_shape` on that axis,
so one index drives both. That includes auxiliaries — a RoPE `cos`/`sin` table, a bias, a mask
co-iterated on the sequence axis. Tiling an auxiliary at a different grain than its siblings
forces `aux[parent * per_block + local]` realignment arithmetic, which is the signal that the
grain was wrong at declaration.

## Anti-patterns

### 1. Offset slicing an axis that should have been tiled

```python
# BAD: manual offset arithmetic on raw memory
x_k = xT_sb[:, k * TILE_S:(k + 1) * TILE_S]

# GOOD: tile that axis, then index by coordinate
x_tiles = nl.tiles(xT_sb, tile_shape=(TILE_K, TILE_S))
x_k = x_tiles[..., k].data
```

If you are writing `x[:, k*T:(k+1)*T]`, that axis should have been tiled and `k` should index the
view. The same mistake wears `start:stop` syntax too — `w_blocks[:, g*HEAD_BLK : g*HEAD_BLK +
heads_here]` to peel group `g` is block-offset math; block that axis so the group is a
coordinate, `w_blocks[:, g]`.

### 2. A hand-rolled grouping loop over flat tiles

```python
# BAD: GROUP is a block_shape written by hand, with a manual remainder
GROUP = 8
for g in range(ceil(n / GROUP)):
    h0 = g * GROUP
    heads_here = min(GROUP, n - h0)
    for h in range(heads_here):
        tile = flat_tiles[..., h0 + h].load()      # per-tile DMA

# GOOD: express the group as a block -- count, coalesced DMA and partial
# trailing group all come for free
blocks = nl.blocks(w, tile_shape=(TILE_K, d_head), block_shape=(1, GROUP))
for g in range(blocks.shape[1]):
    blk = blocks[0, g].load()                      # ONE DMA for the whole group
```

The magic `GROUP` *is* a `block_shape`; the `min()` *is* the partial trailing block, which a
blocked view carries for you.

### 3. Slicing a sub-unit out of a wide tile

```python
# BAD: peel one head out of a wide PSUM/SBUF tile by offset
head_cols = bank[:, hh * d_head:(hh + 1) * d_head]

# GOOD: tile at the sub-unit grain so the head IS a coordinate
psums = nl.alloc_tiles(tile_shape=(TILE_S, d_head), element_shape=(TILE_S, n_heads * d_head),
                       buffer=nl.psum, dtype=nl.float32)
head_tile = psums[..., hh].data
```

### 4. Re-viewing only to realign grain

Adding an extra `nl.tiles(...)` / `nl.blocks(...)` purely to make two tensors' indices line up
means the grain was wrong where they were declared — fix it there instead. Descending a
coalesced-loaded block to its own tile grid is a different thing and is the normal idiom; only a
re-view whose *purpose* is realignment is the smell.

## Before you write, answer these

1. For each tile view: which `nisa.*` op consumes the tile, and what is that operand's limit?
2. Does any `tile_shape=` contain a whole tensor dimension? If so, move it to `block_shape`.
3. Which axis carries the tunable `block_shape`, and what re-tunes when it changes?
4. Are the tensors stepped by the same loop variable — including auxiliaries — sharing a
   `block_shape` on that axis, so one index drives them?
5. Is there a `k*TILE` slice or a `min()` remainder left anywhere? Both mean an axis wants
   tiling.

Answering "no" to 2 or 5 is a structural issue to fix in the plan, not a style nit to patch with
index arithmetic.

## Out of scope here

Buffer and pipeline depth (double buffering), `element_shape=` sizing for partial extents,
fp32 accumulation chains, and sharding across cores are covered in
`references/performance-basics.md`, `references/indexing-patterns.md` and
`references/memory-patterns.md`.

## Further Reading

- `references/indexing-patterns.md` — tiled and blocked layout syntax, grid metadata, remainders
- `references/performance-basics.md` — contiguous DMA, fusion, PSUM pressure, double buffering
- `references/memory-patterns.md` — DMA and tiling patterns
- `/neuron-nki-docs` → `programming/api/api-nki-language-layout.md` — the authoritative contract for
  `nl.tiles` / `nl.blocks` / `nl.alloc_tiles` / `nl.alloc_blocks`
- `/neuron-nki-docs` → `programming/api/api-nki-language-dims.md#nki-language-tile_size` — the
  hardware constant table every tile shape should be derived from
- `/neuron-nki-docs` → `programming/api/api-nki-isa-*.md` — per-op operand limits, stated in each
  op's own entry
