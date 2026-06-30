# NKI Indexing Patterns Reference

Production-proven indexing patterns. Use these patterns in order of preference.

## Pattern Summary

| Pattern | When to Use | Reference |
|---------|-------------|-----------|
| `tensor[start:end, :]` | Contiguous access, tiling | Examples below |
| `TensorView.slice(step=N)` | Strided/interleaved | [tensor-view.md](nkilib/core/tensor-view.md) |
| `.ap(pattern=...)` | Complex layouts, dynamic | Examples below |
| `nl.ds(start, size)` | Runtime-computed bounds | Examples below |
| **`nl.mgrid` - NOT USED** | N/A - avoid | 0 occurrences in production |

## Memory Type Indexing Rules

Different memory types have different indexing constraints and capabilities. Understanding these is critical for correct kernel design.

### HBM (High Bandwidth Memory)

HBM is external memory with no partition structure. Standard N-dimensional indexing applies.

```python
# HBM indexing - standard multidimensional
input_hbm = nl.ndarray((batch, seq, hidden), dtype=nl.float32, buffer=nl.shared_hbm)

# Any dimension can be sliced freely
tile = input_hbm[0:batch, seq_start:seq_end, 0:hidden]

# Strided access allowed (though may impact performance)
every_other = input_hbm[::2, :, :]
```

**HBM characteristics:**
- No partition dimension restriction
- Any dimension ordering allowed
- Strided access permitted (DMA handles it)
- Shape only limited by available memory

### SBUF (State Buffer)

SBUF has 128 physical partitions. The first dimension maps to partitions.

```python
# SBUF: shape[0] = partition dimension, must be ≤ 128
tile_sb = nl.ndarray((128, 512), dtype=nl.float32, buffer=nl.sbuf)

# Valid: partition dim ≤ 128, free dim ≤ 32767
valid_sb = nl.ndarray((64, 4096), dtype=nl.float32, buffer=nl.sbuf)

# INVALID: partition exceeds 128
# invalid_sb = nl.ndarray((256, 512), ...)  # Error!

# When loading: partition must align to first dimension
nisa.dma_copy(
    dst=tile_sb[0:p_size, 0:f_size],  # P first, F second
    src=hbm_tensor[p_start:p_end, f_start:f_end]
)
```

**SBUF constraints:**
- Partition dimension (shape[0]): **≤ 128**
- Free dimension (shape[1:]): **≤ 32767**
- First dimension IS the partition dimension
- Cannot transpose partition to other dimensions without explicit instruction

### PSUM (Partial Sum Buffer)

PSUM is used for accumulation, with tighter constraints than SBUF.

```python
# PSUM: more restrictive than SBUF
psum_tile = nl.ndarray((128, 512), dtype=nl.float32, buffer=nl.psum)

# Valid for PSUM
valid_psum = nl.ndarray((64, 256), dtype=nl.float32, buffer=nl.psum)

# INVALID: free dimension > 512
# invalid_psum = nl.ndarray((128, 1024), ...)  # Error!

# MatMul result goes to PSUM
nisa.nc_matmul(
    dst=psum_tile,       # Must be PSUM buffer
    stationary=a_sb,     # From SBUF
    moving=b_sb          # From SBUF
)

# Copy from PSUM to SBUF for further processing
result_sb = nl.ndarray((128, 512), dtype=nl.float32, buffer=nl.sbuf)
nisa.nc_transpose(dst=result_sb, data=psum_tile)
```

**PSUM constraints:**
- Partition dimension (shape[0]): **≤ 128**
- Free dimension (shape[1]): **≤ 512 (gen2/gen3)** or **≤ 4096 (gen4)**
- Used primarily for matrix multiply accumulation
- Cannot be directly written to HBM (must go through SBUF)

### Memory Type Comparison Table

| Feature | HBM | SBUF | PSUM |
|---------|-----|------|------|
| Max partition (P) | Unlimited | 128 | 128 |
| Max free (F) | Unlimited | 32767 | 512 (gen2/3) / 4096 (gen4) |
| Direct DMA to HBM | - | Yes | No |
| MatMul destination | No | No | Yes |
| General compute | No | Yes | No |
| Strided access | Yes | Via `.ap()` | No |
| Multi-dimensional | Yes (N-D) | 2D logical | 2D only |

### Memory Type Index Examples

```python
# Complete workflow: HBM → SBUF → compute → PSUM → SBUF → HBM

# 1. HBM input - any shape
input_hbm = nl.ndarray((256, 1024), dtype=nl.float32, buffer=nl.shared_hbm)

# 2. Tile into SBUF-compatible chunks
for p_tile in range(0, 256, 128):
    p_size = min(128, 256 - p_tile)

    for f_tile in range(0, 1024, 512):
        f_size = min(512, 1024 - f_tile)

        # 3. Load to SBUF (respects P≤128, F≤32767)
        tile_sb = nl.ndarray((p_size, f_size), dtype=nl.float32, buffer=nl.sbuf)
        nisa.dma_copy(
            dst=tile_sb[0:p_size, 0:f_size],
            src=input_hbm[p_tile:p_tile+p_size, f_tile:f_tile+f_size]
        )

        # 4. If doing matmul, result goes to PSUM (F≤512)
        # psum_result = nl.ndarray((p_size, f_size), buffer=nl.psum)
        # nisa.nc_matmul(dst=psum_result, ...)
```

## Partition Dimension Rules

The partition dimension is the most constrained aspect of NKI programming. These rules are enforced by the compiler.

### What Cannot Be Done with Partitions

**1. Reshape partition dimension:**
```python
# INVALID: Cannot reshape 128x512 to 64x1024 (changes partition count)
sbuf_tile = nl.ndarray((128, 512), dtype=nl.float32, buffer=nl.sbuf)
# reshaped = sbuf_tile.reshape(64, 1024)  # Error!

# VALID: Reshape only free dimensions
sbuf_3d = nl.ndarray((128, 32, 16), dtype=nl.float32, buffer=nl.sbuf)
# Flatten free dims: (128, 32, 16) → (128, 512)
```

**2. Strided partition access:**
```python
# INVALID: Cannot stride across partitions
# tile = sbuf[::2, :]  # Error: strided partition access

# VALID: Contiguous partition slice
tile = sbuf[0:64, :]  # First 64 partitions
tile = sbuf[64:128, :]  # Last 64 partitions
```

**3. Flatten partition with free dims:**
```python
# INVALID: Cannot flatten partition into free
# flat = sbuf.flatten()  # Error!

# VALID: Use explicit tiling loops instead
for p in nl.affine_range(num_p_tiles):
    process_tile(sbuf[p*64:(p+1)*64, :])
```

**4. Negative strides on partition:**
```python
# INVALID: Cannot reverse partition order
# reversed_p = sbuf[::-1, :]  # Error!

# VALID: Process in reverse order via loop
for p in range(num_tiles - 1, -1, -1):
    process_tile(sbuf[p*64:(p+1)*64, :])
```

**5. Transpose without explicit instruction:**
```python
# INVALID: Cannot transpose via indexing
# transposed = sbuf.T  # Error!

# VALID: Use nc_transpose instruction
transposed = nl.ndarray((512, 128), dtype=nl.float32, buffer=nl.sbuf)
nisa.nc_transpose(dst=transposed, data=sbuf)
```

### Valid Partition Operations

**Contiguous slicing:**
```python
# Full partition range
full = sbuf[0:128, :]

# Partial partition range (contiguous)
partial = sbuf[32:96, :]  # 64 partitions

# Single partition
single = sbuf[0:1, :]
```

**Dynamic offset with nl.ds:**
```python
# Dynamic partition offset (must still be contiguous)
for i in nl.affine_range(num_tiles):
    p_offset = i * 64
    tile = sbuf[nl.ds(p_offset, 64), :]
```

### Common Partition Errors and Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `Partition dimension exceeds 128` | Shape[0] > 128 | Tile with outer loop |
| `Strided partition access` | Using `::stride` on dim 0 | Use contiguous slice + loop |
| `Cannot reshape partition` | Reshape changes dim 0 | Reshape only free dimensions |
| `Invalid transpose` | Using `.T` or `np.transpose` | Use `nisa.nc_transpose()` |

```python
# Fix: Partition exceeds 128
# Before (Error):
# big_tensor = nl.ndarray((256, 512), buffer=nl.sbuf)

# After (Fixed):
for p_tile in range(0, 256, 128):
    p_size = min(128, 256 - p_tile)
    tile = nl.ndarray((p_size, 512), dtype=nl.float32, buffer=nl.sbuf)
    # Process tile...

# Fix: Strided partition access
# Before (Error):
# even_partitions = sbuf[::2, :]

# After (Fixed - process alternating in loop):
for i in range(0, 128, 2):
    single = sbuf[i:i+1, :]
    # Process single partition...
```

## 1. Simple Slicing (Most Common - Use First)

Basic slicing is the most common pattern in production kernels. Use for contiguous memory access.

```python
# Basic tiling pattern (from cumsum.py)
nisa.dma_copy(
    dst=data_sb[0:p_tile.size, 0:f_size],
    src=x_2d[p_tile.start_offset:p_tile.start_offset + p_tile.size, f_start:f_end]
)

# Use min() for edge case handling
f_start = f_tile_idx * F_TILE_SIZE
f_end = min(f_start + F_TILE_SIZE, last_dim)
f_size = f_end - f_start

tile = tensor[:, f_start:f_end]
```

**Key points:**
- Works for contiguous memory regions
- Use `min()` at boundaries, NOT deprecated `mask=` parameter
- Variables in slices are resolved at compile time

## 2. TensorView for Strided/Complex Access (Recommended)

Use `TensorView` from the production utility library for strided or interleaved access patterns.

```python
from nkilib.core.utils.tensor_view import TensorView  # or inline from references/nkilib/core/utils/tensor_view.py

# Strided access - even indices (step=2, start=0)
nisa.dma_copy(
    dst=x_sb[:half_d, :, :, :],
    src=TensorView(x_hbm)
        .slice(dim=0, start=0, end=d_head, step=2)  # Even: 0,2,4,...
        .slice(dim=3, start=tile_start, end=tile_start + tile_size)
        .get_view()
)

# Strided access - odd indices (step=2, start=1)
nisa.dma_copy(
    dst=x_sb[half_d:, :, :, :],
    src=TensorView(x_hbm)
        .slice(dim=0, start=1, end=d_head, step=2)  # Odd: 1,3,5,...
        .get_view()
)

# Broadcasting across a dimension
nisa.tensor_tensor(
    dst=result,
    data1=input_sb,
    data2=TensorView(cos_sb)
        .expand_dim(2)                    # Add dimension
        .broadcast(dim=2, size=n_heads)   # Broadcast along it
        .get_view(),
    op=nl.multiply
)
```

**TensorView operations:**
- `.slice(dim, start, end, step)` - Slice with optional stride
- `.expand_dim(dim)` - Add dimension of size 1
- `.broadcast(dim, size)` - Broadcast size-1 dim
- `.permute(dims)` - Reorder dimensions
- `.reshape_dim(dim, shape)` - Reshape single dimension
- `.flatten_dims(start, end)` - Flatten dimension range
- `.get_view()` - Return NKI tensor with pattern applied

## 3. Direct .ap() Patterns (Advanced)

For precise hardware control when TensorView doesn't suffice. Pattern format: `[[stride0, size0], [stride1, size1], ...]`

```python
# Static access pattern (from rope.py layout conversion)
# Pattern [[d_head, d_head], [1, 2], [2, half_d]] reads with stride=2 in innermost
identity_sb.ap(pattern=[[d_head, d_head], [1, 2], [2, half_d]])

# Dynamic scalar offset for indirect indexing
tensor.ap(
    pattern=[[512, 128], [1, 256]],
    offset=0,
    scalar_offset=batch_idx_sbuf,  # Runtime value from SBUF
    indirect_dim=0
)
```

**Pattern structure:**
- Each `[stride, size]` pair describes one dimension
- `stride` = elements to skip between consecutive indices
- `size` = number of elements in this dimension
- `offset` = starting offset in elements

## 4. nl.ds() for Dynamic Slices

Use `nl.ds()` when slice bounds are computed at runtime within loops.

```python
# nl.ds(start, size) creates a dynamic slice
# Equivalent to tensor[:, start:start+size] but for runtime values

for i_bn_tile in range(num_tiles):
    bn_tile_offset = i_bn_tile * BN_STATS_TILE_SIZE
    bn_tile_sz = min(BN_STATS_TILE_SIZE, dims.H - bn_tile_offset)

    nisa.bn_stats(
        dst=result_sb[0:s_tile_sz, nl.ds(i_bn_tile * DST_SIZE, DST_SIZE)],
        data=input_sb[0:s_tile_sz, nl.ds(bn_tile_offset, bn_tile_sz)]
    )

# Use with tensor operations
nisa.tensor_copy(
    dst=output_sb[0:s_tile_sz, nl.ds(head_offset, num_d)],
    src=psum_result[0:s_tile_sz, nl.ds(psum_offset, num_d)]
)
```

**Key points:**
- `nl.ds(start, size)` not `nl.ds(start, end)`
- Works in both source and destination positions
- Commonly used with loop-computed offsets

## Operation-Specific Index Constraints

Different NKI operations have specific indexing requirements. Violating these causes compiler errors or incorrect results.

### nc_matmul Indexing

Matrix multiply has the strictest indexing constraints.

```python
# nc_matmul: C = A @ B
# A (stationary): shape (M, K) - M maps to partition, K is contraction
# B (moving): shape (K, N) - K is contraction, N is output free dim
# C (dst): shape (M, N) - goes to PSUM

# Constraints:
# - K (contraction) dimension: ≤ 2048
# - M (stationary partition): ≤ 128
# - N (dst free dim): ≤ 512 (gen2/gen3) or ≤ 4096 (gen4)

# Example: 128x512 @ 512x256 → 128x256
a_sb = nl.ndarray((128, 512), dtype=nl.float32, buffer=nl.sbuf)  # M=128, K=512
b_sb = nl.ndarray((512, 256), dtype=nl.float32, buffer=nl.sbuf)  # K=512, N=256
c_psum = nl.ndarray((128, 256), dtype=nl.float32, buffer=nl.psum)  # Result

nisa.nc_matmul(dst=c_psum, stationary=a_sb, moving=b_sb)
```

**Stationary vs Moving operand:**
```python
# Stationary operand: held in place, free dim ≤ 128
# Moving operand: streamed through, free dim ≤ 512 (gen2/3) / 4096 (gen4)

# For large N, tile the moving operand
for n_tile in range(0, N, 512):  # Tile N dimension
    n_size = min(512, N - n_tile)
    b_tile = b_sb[:, n_tile:n_tile+n_size]
    c_tile = c_psum[:, n_tile:n_tile+n_size]
    nisa.nc_matmul(dst=c_tile, stationary=a_sb, moving=b_tile)
```

### nc_transpose Indexing

Transpose has tile size constraints.

```python
# nc_transpose: input tile must be ≤ 128x128
# For larger transposes, tile the operation

input_sb = nl.ndarray((128, 128), dtype=nl.float32, buffer=nl.sbuf)
output_sb = nl.ndarray((128, 128), dtype=nl.float32, buffer=nl.sbuf)

# Valid: 128x128 tile
nisa.nc_transpose(dst=output_sb, data=input_sb)

# For larger: tile into 128x128 chunks
for p_tile in range(0, P, 128):
    for f_tile in range(0, F, 128):
        p_size = min(128, P - p_tile)
        f_size = min(128, F - f_tile)

        src_tile = input_large[p_tile:p_tile+p_size, f_tile:f_tile+f_size]
        dst_tile = output_large[f_tile:f_tile+f_size, p_tile:p_tile+p_size]
        nisa.nc_transpose(dst=dst_tile, data=src_tile)
```

### dma_copy / dma_transpose Indexing

DMA operations support three addressing modes for different use cases.

**DGE (Data Gather Engine) Modes:**

| Mode | Parameter | When to Use | Performance |
|------|-----------|-------------|-------------|
| None | (default) | Compile-time known indices | Fastest |
| SWDGE | `dge_mode=dge_mode.swdge` | Loop-variable indices, small iteration count | Medium |
| HWDGE | `dge_mode=dge_mode.hwdge` | Runtime-computed indices, large iteration count | Flexible |

```python
# Required import for DGE modes
from nki.isa.constants import dge_mode

# No DGE: indices known at compile time
nisa.dma_copy(
    dst=tile_sb[0:128, 0:256],
    src=hbm[0:128, 0:256]
)

# SWDGE: loop index known at compile time, unrolled
for i in nl.affine_range(4):
    offset = i * 256
    nisa.dma_copy(
        dst=tile_sb[0:128, nl.ds(offset, 256)],
        src=hbm[0:128, nl.ds(offset, 256)],
        dge_mode=dge_mode.swdge,  # Software DGE for unrolled loop
    )

# HWDGE: runtime-computed indices
batch_idx = compute_batch_index()  # Runtime value
nisa.dma_copy(
    dst=tile_sb[0:128, 0:256],
    src=hbm[batch_idx*128:(batch_idx+1)*128, 0:256],
    dge_mode=dge_mode.hwdge,  # Hardware DGE for runtime indexing
)
```

**dma_transpose specific:**
```python
# dma_transpose: loads with transpose in single operation
# More efficient than dma_copy + nc_transpose for HBM→SBUF

# Source in HBM: (F, P) layout
# Destination in SBUF: (P, F) layout - transposed
nisa.dma_transpose(
    dst=sbuf_tile[0:p_size, 0:f_size],
    src=hbm_transposed[0:f_size, 0:p_size]
)
```

### tensor_reduce Indexing

Reduction can only operate on free dimensions, not the partition dimension.

```python
# VALID: Reduce along free axis (axis >= 1)
input_sb = nl.ndarray((128, 512), dtype=nl.float32, buffer=nl.sbuf)
result_sb = nl.ndarray((128, 1), dtype=nl.float32, buffer=nl.sbuf)

nisa.tensor_reduce(
    dst=result_sb,
    data=input_sb,
    op=nl.add,
    axis=1  # Reduce free dimension
)

# INVALID: Cannot reduce partition dimension directly
# nisa.tensor_reduce(..., axis=0)  # Error!

# To reduce across partitions: use multi-step approach
# 1. Reduce free dim within each partition
# 2. Gather partition results to single partition
# 3. Reduce gathered results
```

**Multi-axis reduction:**
```python
# For 3D tensor, can reduce multiple free axes
input_3d = nl.ndarray((128, 64, 32), dtype=nl.float32, buffer=nl.sbuf)

# Reduce last axis
partial = nl.ndarray((128, 64, 1), dtype=nl.float32, buffer=nl.sbuf)
nisa.tensor_reduce(dst=partial, data=input_3d, op=nl.add, axis=2)

# Then reduce second-to-last
final = nl.ndarray((128, 1, 1), dtype=nl.float32, buffer=nl.sbuf)
nisa.tensor_reduce(dst=final, data=partial, op=nl.add, axis=1)
```

### Constraint Quick Reference Table

| Operation | Constraint | Limit | Notes |
|-----------|------------|-------|-------|
| `nc_matmul` | K (contraction) | ≤ 2048 | Tile K for larger |
| `nc_matmul` | M (lhs partition) | ≤ 128 | Standard partition limit |
| `nc_matmul` | N (dst free) | ≤ 512 (gen2/3) / ≤ 4096 (gen4) | Tile N for larger |
| `nc_transpose` | Tile size | ≤ 128×128 | Tile both dims |
| `tensor_reduce` | Axis | ≥ 1 (free only) | Cannot reduce partition |
| `dma_copy` | Partition | ≤ 128 | Standard |
| `dma_copy` | Free | ≤ 32767 | SBUF limit |
| Any PSUM op | Free | ≤ 512 (gen2/3) / ≤ 4096 (gen4) | PSUM limit |

## Dynamic Indexing Deep Dive

Understanding when indices are resolved is critical for correct kernel design.

### Static vs Dynamic Index Resolution

| Index Type | Resolution Time | Example | Use Case |
|------------|-----------------|---------|----------|
| Literal | Compile | `tensor[0:128, 0:256]` | Fixed-size tiles |
| Python variable | Compile | `tensor[0:p_size, 0:f_size]` | Variable from Python scope |
| `nl.affine_range` var | Compile (unrolled) | `tensor[i*128:(i+1)*128, :]` | Parallel tiling |
| `nl.ds()` | Runtime | `tensor[:, nl.ds(offset, size)]` | Dynamic bounds |
| `nl.dynamic_range` var | Runtime | True on-chip loop | Data-dependent iteration |

### nl.ds() Usage Patterns

**Basic usage:**
```python
# nl.ds(start, size) creates a dynamic slice
# The size must be a compile-time constant

offset = i * TILE_SIZE  # Can be loop variable
size = TILE_SIZE        # Must be compile-time constant

tile = tensor[:, nl.ds(offset, size)]
```

**In nested loops:**
```python
for p_idx in nl.affine_range(num_p_tiles):
    for f_idx in nl.affine_range(num_f_tiles):
        p_offset = p_idx * P_TILE
        f_offset = f_idx * F_TILE

        # Both offsets can be dynamic
        tile = tensor[nl.ds(p_offset, P_TILE), nl.ds(f_offset, F_TILE)]
```

**With conditional sizing:**
```python
# Handle edge tiles with min()
for i in nl.affine_range(num_tiles):
    offset = i * TILE_SIZE
    # Size is still compile-time (TILE_SIZE), edge handling via min()
    actual_size = min(TILE_SIZE, total - offset)

    # Load full tile, process only actual_size
    tile = nl.ndarray((128, TILE_SIZE), dtype=nl.float32, buffer=nl.sbuf)
    nisa.dma_copy(
        dst=tile[0:128, 0:actual_size],
        src=hbm[0:128, nl.ds(offset, actual_size)]
    )
```

### DGE Mode Selection Guide

Choose the appropriate DGE mode based on your indexing pattern:

```
Index pattern?
|
+-- All indices compile-time constants?
|   +-- YES: No DGE needed (default)
|         nisa.dma_copy(dst=a, src=b[0:128, 0:256])
|
+-- Loop variable from affine_range/sequential_range?
|   +-- YES: Use SWDGE
|         for i in nl.affine_range(N):
|             nisa.dma_copy(..., dge_mode=dge_mode.swdge)
|
+-- Runtime-computed index (dynamic_range, data-dependent)?
|   +-- YES: Use HWDGE
|         nisa.dma_copy(..., dge_mode=dge_mode.hwdge)
|
+-- Indirect indexing (index from another tensor)?
    +-- YES: Use .ap() with scalar_offset
          tensor.ap(pattern=..., scalar_offset=idx_tensor, indirect_dim=0)
```

### Performance Implications

| Mode | Overhead | When Optimal |
|------|----------|--------------|
| No DGE | Lowest | Fixed access patterns |
| SWDGE | Low | Small, unrolled loops (< 16 iterations) |
| HWDGE | Medium | Large loops, runtime indices |
| `.ap()` indirect | Higher | True gather/scatter |

**Example: Choosing between SWDGE and HWDGE:**
```python
# SWDGE: better for small, unrolled loops
# Loop is unrolled, each iteration becomes separate instruction
for i in nl.affine_range(4):  # Small iteration count
    nisa.dma_copy(dst=..., src=...[nl.ds(i*256, 256)], dge_mode=dge_mode.swdge)

# HWDGE: better for large loops or runtime bounds
# True on-chip loop, index computed at runtime
for i in nl.affine_range(64):  # Large iteration count
    nisa.dma_copy(dst=..., src=...[nl.ds(i*256, 256)], dge_mode=dge_mode.hwdge)

# Or with runtime iteration count
for i in nl.dynamic_range(runtime_count):
    nisa.dma_copy(dst=..., src=...[nl.ds(i*256, 256)], dge_mode=dge_mode.hwdge)
```

## Compile-Time vs Runtime

| Evaluated At | Constructs | Notes |
|--------------|------------|-------|
| Compile-time | `range()`, `tensor.shape`, `print()`, slice literals | Loop unrolled, values baked in |
| Unrolled at compile | `nl.affine_range()`, `nl.sequential_range()` | Loop body replicated N times |
| Runtime (on-device) | `nl.dynamic_range()`, registers | True on-chip iteration |

```python
# Compile-time: shape known, loop unrolled
for i in range(4):  # Unrolled to 4 separate blocks
    tile = tensor[:, i*128:(i+1)*128]

# Loop unrolled at compile time, iteration variable available
for i in nl.affine_range(num_tiles):  # Parallel iterations
    offset = i * TILE_SIZE
    tile = tensor[:, nl.ds(offset, TILE_SIZE)]

# Runtime: true on-device loop
for i in nl.dynamic_range(runtime_count):  # On-chip loop
    # i is a runtime value
```

## Common Mistakes to Avoid

| Mistake | Why It's Wrong | Correct Approach |
|---------|---------------|------------------|
| Using `nl.mgrid[]` | Not used in production (0 occurrences) | Use slicing or `.ap()` |
| Using `nl.load()`/`nl.store()` | Deprecated in Beta 2 | Use `nisa.dma_copy()` |
| Using `nl.arange()` | Deprecated | Use slicing or `nl.ds()` |
| Using `mask=` for bounds | Deprecated | Use `min()` for edge cases |
| Compile-time `print()` confusion | `print()` runs at compile time | Use `nl.device_print()` for runtime |

## Decision Tree

### Primary: Pattern Selection

```
Need to index a tensor?
|
+-- Contiguous access?
|   +-- YES: Use simple slicing
|         tensor[start:end, f_start:f_end]
|
+-- Strided/interleaved access?
|   +-- YES: Use TensorView
|         TensorView(tensor).slice(dim, start, end, step=N).get_view()
|
+-- Complex layout transformation?
|   +-- YES: Use .ap() directly
|         tensor.ap(pattern=[[stride, size], ...])
|
+-- Runtime-computed offset in loop?
|   +-- YES: Use nl.ds()
|         tensor[:, nl.ds(loop_offset, tile_size)]
|
+-- Edge case at boundary?
    +-- YES: Use min() for bounds
          f_end = min(f_start + TILE_SIZE, total_f)
```

### Memory Type Considerations

```
What memory type?
|
+-- HBM (input/output)?
|   +-- No partition constraints
|   +-- Any dimension ordering allowed
|   +-- Strided access OK (DMA handles it)
|
+-- SBUF (compute buffer)?
|   +-- shape[0] ≤ 128 (partition)
|   +-- shape[1:] ≤ 32767 (free)
|   +-- No strided partition access
|   +-- Tile if exceeds limits
|
+-- PSUM (matmul accumulator)?
    +-- shape[0] ≤ 128 (partition)
    +-- shape[1] ≤ 512 (gen2/3) / ≤ 4096 (gen4) (free)
    +-- Must copy to SBUF before HBM write
```

### Operation-Specific Constraints

```
Which operation?
|
+-- nc_matmul?
|   +-- K ≤ 2048, N ≤ 512 (gen2/3) / 4096 (gen4)
|   +-- Result must go to PSUM
|   +-- Tile K and N if needed
|
+-- nc_transpose?
|   +-- Tile ≤ 128×128
|   +-- Tile both dimensions for larger
|
+-- tensor_reduce?
|   +-- axis ≥ 1 (free dims only)
|   +-- Cannot reduce partition directly
|
+-- dma_copy with dynamic indices?
    +-- Compile-time indices: no DGE
    +-- affine_range loop var: dge_mode=dge_mode.swdge
    +-- Runtime/dynamic_range: dge_mode=dge_mode.hwdge
```

### DGE Mode Selection

```
Index type in DMA?
|
+-- All compile-time constants?
|   +-- No DGE (default, fastest)
|
+-- Loop variable, small iteration (<16)?
|   +-- SWDGE (dge_mode=dge_mode.swdge)
|
+-- Loop variable, large iteration (≥16)?
|   +-- HWDGE (dge_mode=dge_mode.hwdge)
|
+-- Runtime value (dynamic_range, computed)?
|   +-- HWDGE (dge_mode=dge_mode.hwdge)
|
+-- Indirect (index from tensor)?
    +-- .ap() with scalar_offset
```

## Further Reading

| Pattern | Self-Contained Reference |
|---------|------------------------|
| TiledRange for tiling loops | [tiled-range.md](nkilib/core/tiled-range.md) |
| TensorView strided access | [tensor-view.md](nkilib/core/tensor-view.md) |
| Layout conversion (.ap()) | [layout-conversion.md](nkilib/patterns/layout-conversion.md) |
| div_ceil, dtype helpers | [kernel-helpers.md](nkilib/core/kernel-helpers.md) |
