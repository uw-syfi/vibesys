# NKI Transpose and Layout Transformation Reference

Production-proven transpose and layout transformation patterns. LLMs often struggle with these operations - use these reliable patterns instead.

## Technique Summary (Quick Reference)

| Technique | When to Use | Hardware Gen | Production Files |
|-----------|-------------|--------------|------------------|
| `nisa.nc_transpose()` | P↔F transpose, after MatMul | All | Examples below, MLP CTE transpose pattern |
| `TensorView` | Zero-copy layout manipulation, broadcast, permute | All | [tensor-view.md](nkilib/core/tensor-view.md), examples below |
| `.ap()` patterns | Complex layouts, custom strides | All | Examples below |
| DMA strided access | Interleaved↔contiguous during DMA | gen3+ (optimized) | [layout-conversion.md](nkilib/patterns/layout-conversion.md), examples below |

### Constraint Quick Reference

| Constraint | Limit | Notes |
|------------|-------|-------|
| **Partition Dimension (P)** | ≤ 128 | First dimension of SBUF/PSUM, cannot reshape or stride |
| **SBUF Free Dimension (F)** | ≤ 32,767 | Second+ dimensions |
| **PSUM Free Dimension (F)** | ≤ 512 (gen2/3) / ≤ 4,096 (gen4) | For nc_transpose destination |
| **nc_transpose step size** | 2 for fp8/int8, 1 otherwise | Generation-specific |
| **TensorView partition rule** | Cannot permute dim 0 in SBUF | Hardware constraint |

---

## Core Technique 1: nc_transpose

Hardware-accelerated transpose using PSUM array as intermediate buffer. Primary method for transposing partition ↔ free dimensions.

### When to Use

- **Primary use case**: Transpose partition dimension (P) ↔ free dimension (F)
- After matrix multiply (PSUM → SBUF with transposed layout)
- Need to swap dimension 0 with any other dimension in SBUF
- Code smell: "I need dimension N to become dimension 0" or vice versa

### Basic Pattern

```python
import nki
import nki.isa as nisa
import nki.language as nl

# Basic nc_transpose: swap P↔F dimensions
input_sb = nl.ndarray((128, 512), dtype=nl.float16, buffer=nl.sbuf)
output_sb = nl.ndarray((512, 128), dtype=nl.float16, buffer=nl.sbuf)

nisa.nc_transpose(dst=output_sb, data=input_sb)
# Result: (128, 512) → (512, 128) swapped dimensions
```

### Production Example: MLP Transpose with Array Patterns

From MLP CTE transpose kernel (nc_transpose with array patterns):

```python
# CONTEXT: Transpose source tensor from (BxS, Hidden) → (Hidden, BxS) for MLP
# INPUTS: source_tile_sbuf [BxS_subtile_bound, Hidden_subtile_bound] @ SBUF
# OUTPUTS: res_psum_tensor [Hidden_subtile_bound, BxS_subtile_bound] @ PSUM
# GENERATION: All, with step_size=2 for fp8/int8

nisa.nc_transpose(
    dst=res_psum_tensor.ap(
        [
            [psum_tile_info.tile_count * psum_tile_info.tile_size * psum_step_size, hidden_subtile_bound],
            [1, 1],  # Intermediate tiling dimension
            [psum_step_size, bxs_subtile_bound],
        ],
        offset=hidden_subtile_idx * H_SUBTILE_SIZE * psum_step_size,
    ),
    data=source_tile_sbuf[
        0:bxs_subtile_bound,
        hidden_dim_tile.get_subtile_indices(hidden_tile_idx, hidden_subtile_idx, hidden_subtile_bound),
    ],
)

# Key points:
# - Destination uses .ap() pattern for complex PSUM layout (multi-bank, tiled)
# - psum_step_size = 2 for quantized (fp8/int8), 1 otherwise
# - Source is simple SBUF slice
# - Pattern [[stride, size], [stride, size], ...] defines memory layout
```

### PSUM Bank Management

For multiple concurrent transposes, use separate PSUM banks to avoid conflicts:

```python
# Allocate PSUM tensors in different banks
PSUM_BANK_SIZE = 2048  # Hardware constant

psum_bank_0 = nl.ndarray(
    (H_SUBTILE_SIZE, BXS_SIZE),
    dtype=nl.float16,
    buffer=nl.psum,
    address=(0, 0 * PSUM_BANK_SIZE)  # Bank 0
)

psum_bank_1 = nl.ndarray(
    (H_SUBTILE_SIZE, BXS_SIZE),
    dtype=nl.float16,
    buffer=nl.psum,
    address=(0, 1 * PSUM_BANK_SIZE)  # Bank 1
)

# Now can transpose into different banks concurrently
nisa.nc_transpose(dst=psum_bank_0, data=source_tile_0)
nisa.nc_transpose(dst=psum_bank_1, data=source_tile_1)
```

### Generation-Specific Constraints

```python
# Check hardware generation for step size
import nki.isa as nisa

# 1-byte dtypes (fp8, int8) require step size of 2 on all generations
psum_step_size = 2 if input_dtype in [nl.float8_e4m3, nl.float8_e5m2, nl.int8] else 1

# PSUM free dimension limits vary by generation
if nki.isa.get_nc_version() == nki.isa.nc_version.gen2:
    max_psum_f = 512
elif nki.isa.get_nc_version() == nki.isa.nc_version.gen3:
    max_psum_f = 512
else:  # gen4
    max_psum_f = 4096
```

### Constraints and Gotchas

| Constraint | Limit | Solution |
|------------|-------|----------|
| PSUM free dimension | ≤ 512 (gen2/3) / ≤ 4,096 (gen4) | Tile the transpose operation |
| Step size | 2 for fp8/int8 | Check dtype, adjust PSUM allocation |
| Partition dimension | ≤ 128 | Standard SBUF limit, tile if needed |
| PSUM → HBM | Not direct | Copy PSUM → SBUF → HBM |

**Common error**: "PSUM dimension exceeds limit"
- **Cause**: Free dimension > 512 (gen2/3) or > 4096 (gen4)
- **Fix**: Tile the transpose into smaller chunks

---

## Core Technique 2: TensorView - Zero-Copy Layout Manipulation

High-level abstraction for changing tensor layout without data movement. Uses stride manipulation to create different views of the same memory.

### When to Use

- Need different logical layout without copying data
- Broadcasting scalar/1D tensors across dimensions
- Permuting dimensions (without involving partition dimension)
- Slicing with stride (gather even/odd elements)
- Fusing operations that expect different shapes
- Code smell: "I just need a different view of the same data"

### TensorView Method Reference

| Method | Purpose | Example | Partition Constraint |
|--------|---------|---------|---------------------|
| `.slice(dim, start, end, step)` | Strided slicing | Every 2nd element | Can slice partition (contiguous only) |
| `.permute(dims)` | Reorder dimensions | (P,H,W) → (P,W,H) | Partition must stay dim 0 |
| `.broadcast(dim, size)` | Expand size-1 dim | (P,1,F) → (P,N,F) | Original dim must be size 1 |
| `.reshape_dim(dim, shape)` | Split/merge dimension | (P,24) → (P,2,3,4) | Cannot reshape partition |
| `.flatten_dims(start, end)` | Merge contiguous dims | (P,2,3,4) → (P,24) | Cannot flatten partition |
| `.expand_dim(dim)` | Add size-1 dimension | (P,F) → (P,1,F) | Any position |
| `.rearrange(src, dst)` | Complex reshape+permute | Einops-style | Complex rules |

### Example 1: Strided DMA for Interleaved Layout

From RoPE kernel (strided DMA gather — see [layout-conversion.md](nkilib/patterns/layout-conversion.md)):

```python
# CONTEXT: Load interleaved tensor to contiguous layout via strided DMA
# INPUTS: x_in [d_head, B, n_heads, S] @ HBM - interleaved [e0,o0,e1,o1,...]
# OUTPUTS: x_in_sb [d_head, B, n_heads, tile_size] @ SBUF - contiguous [e0,e1,...,o0,o1,...]
# GENERATION: All (gen3+ optimized)

from nkilib.core.utils.tensor_view import TensorView  # or inline from references/nkilib/core/utils/tensor_view.py

half_d = d_head // 2

# Gather even indices (0, 2, 4, ...) with step=2
nisa.dma_copy(
    dst=x_in_sb[:half_d, :, :, :],
    src=TensorView(x_in)
        .slice(dim=0, start=0, end=d_head, step=2)  # Even: stride=2, start=0
        .slice(dim=3, start=tile_start, end=tile_start + tile_size)
        .get_view()
)

# Gather odd indices (1, 3, 5, ...) with step=2
nisa.dma_copy(
    dst=x_in_sb[half_d:, :, :, :],
    src=TensorView(x_in)
        .slice(dim=0, start=1, end=d_head, step=2)  # Odd: stride=2, start=1
        .slice(dim=3, start=tile_start, end=tile_start + tile_size)
        .get_view()
)

# Result: Interleaved layout in HBM → Contiguous layout in SBUF
# No explicit loops, handled by DMA with TensorView strides
```

### Example 2: Broadcasting Across Dimensions

From RoPE kernel (broadcasting cos/sin across heads):

```python
# CONTEXT: Broadcast cos/sin tensors across n_heads dimension for RoPE
# INPUTS: cos_sb [half_d, B, S] @ SBUF, x_in_sb [half_d, B, n_heads, S] @ SBUF
# OUTPUTS: even_cos [half_d, B, n_heads, S] @ SBUF
# GENERATION: All

# Compute: even_cos = x_in[:half_d] * cos (broadcast cos across n_heads)
nisa.tensor_tensor(
    even_cos,
    x_in_sb[:half_d, :, :, :],
    TensorView(cos_sb)
        .expand_dim(2)                    # [half_d, B, S] → [half_d, B, 1, S]
        .broadcast(dim=2, size=n_heads)   # [half_d, B, 1, S] → [half_d, B, n_heads, S]
        .get_view(),
    nl.multiply
)

# Key points:
# - expand_dim adds a new dimension of size 1
# - broadcast expands size-1 dimension by setting stride=0
# - Zero-copy: no data movement, just stride manipulation
# - Hardware reuses same cos values across n_heads
```

### Example 3: Permute Dimensions

```python
# CONTEXT: Reorder free dimensions (partition dimension must stay dim 0)
# INPUTS: tensor [P, H, W, C] @ SBUF
# OUTPUTS: permuted view [P, C, H, W] @ SBUF (zero-copy)

# Valid: Permute free dimensions, partition stays dim 0
permuted = TensorView(tensor).permute([0, 3, 1, 2]).get_view()
# Shape: (P, H, W, C) → (P, C, H, W)

# INVALID: Cannot move partition dimension
# permuted = TensorView(tensor).permute([3, 0, 1, 2]).get_view()  # ERROR!
# Partition (dim 0) must remain dim 0 in SBUF
```

### Partition Dimension Constraints

TensorView enforces critical SBUF rules:

```python
# VALID operations on partition dimension:
TensorView(tensor).slice(dim=0, start=0, end=64, step=1)  # Contiguous slice OK
TensorView(tensor).broadcast(dim=0, size=128)  # If originally size 1

# INVALID operations on partition dimension:
TensorView(tensor).permute([1, 0, 2])  # ERROR: partition moved from dim 0
TensorView(tensor).reshape_dim(0, [2, 64])  # ERROR: cannot reshape partition
TensorView(tensor).flatten_dims(0, 1)  # ERROR: cannot flatten partition
TensorView(tensor).slice(dim=0, start=0, end=128, step=2)  # ERROR: strided partition
```

### Performance Notes

- **Zero-copy**: TensorView operations are free (compile-time stride computation)
- **Hardware support**: Strided access via .ap() patterns supported on all generations
- **DMA performance**: Strided DMA (step > 1) has overhead, but often better than SBUF relayout
- **Generation optimization**: gen3+ has improved strided DMA performance

---

## Deprecated Pattern: nl.mgrid (Don't Use)

**Do not use `nl.mgrid` for transpose operations.** This pattern appears in tutorials but has 0 occurrences in production code.

**Use instead:**
- `TensorView` for zero-copy layout manipulation (Section 3)
- `nisa.nc_transpose()` for P↔F transpose (Section 2)

---

## Core Technique 3: Array Patterns (.ap()) for Complex Layouts

Low-level control for sophisticated memory access patterns. TensorView generates `.ap()` internally, but direct use enables patterns TensorView cannot express.

### When to Use

- TensorView insufficient for your access pattern
- Complex broadcasting with non-standard strides
- Non-contiguous tiled access
- Integrating with nc_transpose destination (custom PSUM layout)
- Hardware-specific optimizations
- Code smell: "I need very specific control over memory access"

### Pattern Syntax

```python
tensor.ap(
    pattern=[[stride0, size0], [stride1, size1], ...],
    offset=starting_element_offset
)
```

- **pattern**: List of `[stride, size]` pairs, one per dimension
  - `stride`: Elements to skip between consecutive indices (in elements, not bytes)
  - `size`: Number of elements in this dimension
- **offset**: Starting offset in elements from tensor base address

### Example 1: Zero-Stride Broadcast

```python
# CONTEXT: Broadcast a single value across an entire dimension
# Pattern: stride=0 means "repeat same element"

# Source: [P, 1] @ SBUF - single value per partition
# Destination: [P, F] @ SBUF - broadcast across F

source = nl.ndarray((128, 1), dtype=nl.float16, buffer=nl.sbuf)
result = nl.ndarray((128, 512), dtype=nl.float16, buffer=nl.sbuf)

nisa.tensor_copy(
    dst=result,
    src=source.ap(
        pattern=[
            [1, 128],    # Partition: normal stride
            [0, 512],    # Free: stride=0 → repeat same element
        ],
        offset=0
    )
)

# Hardware reads source[p, 0] and repeats it 512 times for result[p, :]
```

### Example 2: Custom Transpose Destination (Production)

From MLP CTE transpose kernel (complex PSUM layout with .ap()):

```python
# CONTEXT: Complex PSUM layout for tiled transpose with bank management
# Pattern enables non-contiguous writes to multi-bank PSUM structure

nisa.nc_transpose(
    dst=res_psum_tensor.ap(
        pattern=[
            [psum_tile_info.tile_count * psum_tile_info.tile_size * psum_step_size, hidden_subtile_bound],
            [1, 1],  # Intermediate tiling dimension
            [psum_step_size, bxs_subtile_bound],
        ],
        offset=hidden_subtile_idx * H_SUBTILE_SIZE * psum_step_size,
    ),
    data=source_tile_sbuf[...]
)

# Pattern breakdown:
# - Dimension 0: stride = (tile_count * tile_size * step), size = hidden_subtile_bound
#   → Accounts for multi-tile PSUM layout with quantization step
# - Dimension 1: stride = 1, size = 1
#   → Placeholder for tiling dimension (no actual stride)
# - Dimension 2: stride = psum_step_size, size = bxs_subtile_bound
#   → Step size=2 for fp8/int8, size=1 for other dtypes
# - offset: Positions within current subtile
```

### Example 3: Strided Read/Write

```python
# CONTEXT: Access every Nth element with custom stride

# Read every 4th element starting from offset 0
pattern = [[4, size]]  # stride=4, read 'size' elements
view = tensor.ap(pattern=pattern, offset=0)

# Equivalently with TensorView:
view = TensorView(tensor).slice(dim=0, start=0, end=size*4, step=4).get_view()
```

### Example 4: Tiled Access

```python
# CONTEXT: Access tiled regions within larger tensor
# HBM layout: [P, F] but need to access as [P, num_tiles, tile_size]

F_TOTAL = 4096
TILE_SIZE = 512
num_tiles = F_TOTAL // TILE_SIZE  # 8 tiles

# Pattern for accessing tile i:
for i in range(num_tiles):
    tile_pattern = [
        [F_TOTAL, P],        # Partition dimension: stride by full F
        [TILE_SIZE, 1],      # Tile selection: jump by tile_size
        [1, TILE_SIZE]       # Within tile: contiguous access
    ]
    tile_offset = i * TILE_SIZE

    tile_view = tensor.ap(pattern=tile_pattern, offset=tile_offset)
    # Process tile...
```

### Relationship to TensorView

```python
# TensorView generates .ap() patterns internally:
tv = TensorView(tensor).slice(dim=1, start=10, end=50, step=2)
ap_pattern, ap_offset = tv._get_pattern_and_offset()
# Returns: pattern=[[stride0, size0], [stride1*2, size1]], offset=10*stride1

# When to use direct .ap():
# 1. Pattern TensorView cannot express (complex tiling, non-standard layouts)
# 2. Performance-critical code needing exact control
# 3. Debugging TensorView behavior

# When to use TensorView:
# 1. Standard operations (slice, permute, broadcast, reshape)
# 2. Readability and maintainability
# 3. Automatic constraint checking (partition dimension rules)
```

### Constraints and Pitfalls

| Issue | Cause | Solution |
|-------|-------|----------|
| Invalid pattern error | Pattern doesn't respect memory layout | Check stride calculations, verify access bounds |
| Partition dimension error | Cannot stride or reshape partition | Keep partition dim with stride=size or simple multiples |
| Compile-time only | Cannot compute pattern at runtime | All strides/sizes must be compile-time constants |
| Hard to debug | Low-level, easy to create invalid patterns | Use TensorView first, only drop to .ap() if needed |

---

## Core Technique 4: DMA with Strided Access

Combine data movement with layout transformation by using TensorView with DMA operations. Enables efficient gather/scatter patterns.

### When to Use

- Interleaved ↔ contiguous layout conversion during load/store
- Loading only even/odd elements from HBM
- Scatter writes to non-contiguous HBM locations
- Different layout needed in HBM vs SBUF
- Code smell: "I need to load/store with a different memory pattern"

### Strided DMA vs SBUF Relayout

| Approach | When to Use | Constraint | Performance |
|----------|-------------|------------|-------------|
| **Strided DMA** | Any tensor size | gen3+ optimized | Medium overhead, but better than 2x DMA |
| **SBUF relayout** | Small tensors | B*n_heads*S ≤ gemm_moving_fmax | Fast for small, won't fit for large |

### Example 1: Interleaved to Contiguous (Strided Load)

From RoPE kernel (interleaved to contiguous strided load — see [layout-conversion.md](nkilib/patterns/layout-conversion.md)):

```python
# CONTEXT: Convert interleaved RoPE layout to contiguous during DMA load
# INPUTS: x_in [d_head, B, n_heads, S] @ HBM - [e0,o0,e1,o1,...] interleaved
# OUTPUTS: x_in_sb [d_head, B, n_heads, tile_size] @ SBUF - [e0,e1,...,o0,o1,...] contiguous
# GENERATION: All (gen3+ optimized for strided DMA)

from nkilib.core.utils.tensor_view import TensorView  # or inline from references/nkilib/core/utils/tensor_view.py

d_head, B, n_heads, S = x_in.shape
half_d = d_head // 2
tile_start, tile_size = 0, S  # For simplicity, could be tiled

# Allocate SBUF buffer with contiguous layout
x_in_sb = nl.ndarray((d_head, B, n_heads, tile_size), dtype=x_in.dtype, buffer=nl.sbuf)

# Load even indices (0, 2, 4, ...) to first half with step=2
nisa.dma_copy(
    dst=x_in_sb[:half_d, :, :, :],
    src=TensorView(x_in)
        .slice(dim=0, start=0, end=d_head, step=2)  # Even: stride=2
        .slice(dim=3, start=tile_start, end=tile_start + tile_size)
        .get_view()
)

# Load odd indices (1, 3, 5, ...) to second half with step=2
nisa.dma_copy(
    dst=x_in_sb[half_d:, :, :, :],
    src=TensorView(x_in)
        .slice(dim=0, start=1, end=d_head, step=2)  # Odd: stride=2, offset=1
        .slice(dim=3, start=tile_start, end=tile_start + tile_size)
        .get_view()
)

# Result: Two DMAs with stride=2 gather interleaved data into contiguous SBUF layout
# Alternative: Single contiguous DMA + SBUF relayout (but requires extra memory)
```

### Example 2: Contiguous to Interleaved (Strided Store)

From RoPE kernel (contiguous to interleaved strided store):

```python
# CONTEXT: Convert contiguous SBUF layout to interleaved during DMA store
# INPUTS: x_out_sb [d_head, B, n_heads, tile_size] @ SBUF - [e0,e1,...,o0,o1,...] contiguous
# OUTPUTS: x_out [d_head, B, n_heads, S] @ HBM - [e0,o0,e1,o1,...] interleaved
# GENERATION: All (gen3+ optimized)

# Allocate HBM output
x_out = nl.ndarray(x_in.shape, dtype=x_in.dtype, buffer=nl.shared_hbm)

# Scatter even indices (first half of SBUF) to even positions in HBM
nisa.dma_copy(
    dst=TensorView(x_out)
        .slice(dim=0, start=0, end=d_head, step=2)  # Even positions
        .slice(dim=3, start=tile_start, end=tile_start + tile_size)
        .get_view(),
    src=x_out_sb[:half_d, :, :, :]
)

# Scatter odd indices (second half of SBUF) to odd positions in HBM
nisa.dma_copy(
    dst=TensorView(x_out)
        .slice(dim=0, start=1, end=d_head, step=2)  # Odd positions
        .slice(dim=3, start=tile_start, end=tile_start + tile_size)
        .get_view(),
    src=x_out_sb[half_d:, :, :, :]
)

# Result: Two DMAs with stride=2 scatter contiguous SBUF data into interleaved HBM layout
```

### Alternative: SBUF Relayout with MatMul

For small tensors (B*n_heads*S ≤ gemm_moving_fmax ≈ 512), use permutation matrix instead of strided DMA:

```python
# CONTEXT: Convert interleaved ↔ contiguous using matrix multiplication
# Constraint: Tensor size must fit in gemm_moving_fmax
# From rope.py:_compute_convert_to_interleaved_mat

def _compute_convert_to_interleaved_mat(x_sb: nl.ndarray) -> nl.ndarray:
    """
    Generate permutation matrix P where:
        P @ X: [e0,e1,...,o0,o1,...] -> [e0,o0,e1,o1,...] (contiguous to interleaved)
        P^T @ X: [e0,o0,e1,o1,...] -> [e0,e1,...,o0,o1,...] (interleaved to contiguous)

    For d_head=4: P = [[1,0,0,0],
                        [0,0,1,0],
                        [0,1,0,0],
                        [0,0,0,1]]
    """
    d_head, B, n_heads, S = x_sb.shape
    half_d = d_head // 2

    # Create identity matrix [d_head, d_head]
    identity_sb = nl.ndarray((d_head, d_head), dtype=x_sb.dtype, buffer=nl.sbuf)
    # Initialize with identity pattern...

    # Build permutation matrix by accessing even/odd rows from first/second halves
    perm_matrix = nl.ndarray((d_head, d_head), dtype=x_sb.dtype, buffer=nl.sbuf)
    perm_matrix.ap(pattern=[[d_head, d_head], [1, 2], [2, half_d]]) = \
        identity_sb.ap(pattern=[[d_head, d_head], [1, 2], [2, half_d]])
    # (Pattern details in production code)

    return perm_matrix

# Use permutation matrix:
x_contiguous = matmul(perm_matrix_transpose, x_interleaved)
x_interleaved = matmul(perm_matrix, x_contiguous)
```

### Performance Considerations

```python
# Strided DMA overhead (gen3+):
# - Single strided DMA: ~1.2-1.5x slower than contiguous
# - Two strided DMAs (gather): ~2-2.5x slower than contiguous
# - Still better than: contiguous DMA + SBUF relayout + another DMA

# Decision guide:
if tensor_size <= nl.tile_size.gemm_moving_fmax:
    # Small tensor: use SBUF matmul relayout (faster)
    perm_matrix = _compute_convert_to_interleaved_mat(x_sb)
    x_converted = matmul(perm_matrix, x_sb)
else:
    # Large tensor: use strided DMA (only option that fits)
    nisa.dma_copy(
        dst=x_sb,
        src=TensorView(x_hbm).slice(dim=0, start=0, end=d, step=2).get_view()
    )
```

### Generation-Specific Notes

```python
# gen2 (Trn1/Inf2): Basic strided DMA support
# gen3 (Trn2): Optimized strided DMA, recommended for use
# gen4 (Trn3): Further optimized, expanded PSUM limits

# No generation check needed - strided DMA works on all generations
# Performance characteristics vary, but functional on gen2+
```

---

## Decision Tree and Constraints

### Primary Decision Tree

```
Need to change tensor layout?
|
+-- Involves partition dimension (dim 0)?
|   +-- YES: Need P↔F transpose?
|   |   +-- Use nisa.nc_transpose() (Section 2)
|   |       - Ensure PSUM free dim ≤ 512 (gen2/3) or ≤ 4096 (gen4)
|   |       - Use step_size=2 for fp8/int8
|   |
|   +-- NO: Continue below
|
+-- Need to move data (not just view)?
|   +-- NO: Use TensorView (Section 3)
|   |       - Zero-copy operations
|   |       - Methods: permute, broadcast, slice, reshape_dim
|   |       - Partition (dim 0) cannot be permuted in SBUF
|   |
|   +-- YES: During DMA transfer?
|       +-- YES: Use TensorView with DMA (Section 4)
|       |       - TensorView.slice(step=N) for strided access
|       |       - Interleaved ↔ contiguous conversion
|       |       - Alternative: SBUF matmul if tensor small enough
|       |
|       +-- NO: Within SBUF?
|           +-- Partition involved? → nc_transpose
|           +-- Free dims only? → TensorView or tensor_copy with .ap()
|
+-- Complex pattern not covered by TensorView?
    +-- YES: Use .ap() directly (Section 3)
            - Manual stride control
            - Zero-stride broadcast
            - Tiled access patterns
            - Custom nc_transpose destination
```

### Hardware Generation Comparison

| Feature | gen2 (Trn1/Inf2) | gen3 (Trn2) | gen4 (Trn3) |
|---------|------------------|-------------|-------------|
| **PSUM Free Dim** | ≤ 512 | ≤ 512 | ≤ 4,096 |
| **nc_transpose** | Full support | Full support | Full support |
| **TensorView** | Full support | Full support | Full support |
| **Strided DMA** | Basic | Optimized | Optimized |
| **FP8 Support** | No | Yes | Yes (+ MXFP8/4) |
| **nc_transpose step=2** | For int8 | For fp8/int8 | For fp8/int8/mx |

### Constraint Reference

#### Dimension Limits

| Constraint | Limit | Memory Type | Notes |
|------------|-------|-------------|-------|
| **Partition (P)** | ≤ 128 | SBUF, PSUM | First dimension, fixed hardware limit |
| **Free (F)** | ≤ 32,767 | SBUF | Second+ dimensions |
| **PSUM Free (F)** | ≤ 512 (gen2/3) / ≤ 4,096 (gen4) | PSUM | nc_transpose destination, matmul result |
| **MatMul K** | ≤ 2,048 | Any | Contraction dimension |

#### Partition Dimension Rules (SBUF)

**Cannot do:**
- ❌ Reshape: `(128, 512) → (64, 1024)` - changes partition count
- ❌ Stride: `tensor[::2, :]` - non-contiguous partition access
- ❌ Flatten with free: `tensor.flatten()` - merges partition into free dims
- ❌ Permute to non-0: `permute([1, 0, 2])` - partition must stay dim 0
- ❌ Negative stride: `tensor[::-1, :]` - reverse partition order

**Can do:**
- ✅ Contiguous slice: `tensor[0:64, :]` or `tensor[32:96, :]`
- ✅ Full range: `tensor[0:128, :]`
- ✅ Dynamic offset: `tensor[nl.ds(offset, size), :]` (contiguous only)
- ✅ Transpose with nc_transpose: Swap P↔F using hardware instruction

#### Operation-Specific Constraints

| Operation | Constraint | Limit | Workaround |
|-----------|------------|-------|------------|
| `nisa.nc_transpose()` | PSUM free dim | ≤ 512 / ≤ 4,096 (gen) | Tile transpose into chunks |
| `nisa.nc_transpose()` | Step size | 2 for fp8/int8, 1 else | Check dtype, allocate PSUM accordingly |
| `TensorView.permute()` | Partition | Must stay dim 0 | Use nc_transpose for P↔F swap |
| `TensorView.reshape_dim()` | Partition | Cannot reshape | Only reshape free dimensions |
| `TensorView.flatten_dims()` | Partition | Cannot flatten with free | Keep partition separate |
| DMA strided access | Performance | gen3+ optimized | Works on gen2, but slower |

---

## Common Pitfalls and Solutions

### Error Symptoms and Solutions

| Error Message / Symptom | Likely Cause | Solution | Section |
|-------------------------|--------------|----------|---------|
| "Partition dimension exceeds 128" | P > 128 | Tile outer loop to keep P ≤ 128 | 7 |
| "PSUM dimension exceeds limit" | F > 512 (gen2/3) / 4,096 (gen4) | Tile nc_transpose operation | 2 |
| "Cannot reshape partition" | Reshape changes dim 0 | Only reshape free dimensions (dim≥1) | 3, 7 |
| "Partition must stay outermost" | TensorView.permute moved dim 0 | Keep partition at dim 0, or use nc_transpose | 3, 7 |
| "Stride not supported on partition" | Used `tensor[::2, :]` | Use contiguous slice + loop instead | 7 |
| Strided access slower than expected | Using gen2 hardware | Expected on gen2, gen3+ has optimization | 4 |

### Common Anti-Patterns

#### Anti-pattern 1: Using nl.mgrid (Don't Use)

```python
# ❌ Don't use: nl.mgrid for transpose (deprecated, not used in production)
# Use TensorView or nc_transpose instead
```

#### Anti-pattern 2: Trying to Permute Partition Dimension

```python
# ❌ Anti-pattern: Permute partition to non-0 position
tensor = nl.ndarray((128, 64, 32), dtype=nl.float16, buffer=nl.sbuf)
# permuted = TensorView(tensor).permute([1, 0, 2]).get_view()  # ERROR!

# ✅ Fix 1: Keep partition at dim 0, permute only free dims
permuted = TensorView(tensor).permute([0, 2, 1]).get_view()  # (128, 32, 64) OK

# ✅ Fix 2: Use nc_transpose to move partition to free dimension first
# Step 1: Flatten free dims
tensor_2d = TensorView(tensor).flatten_dims(1, 2).get_view()  # (128, 2048)

# Step 2: Transpose P↔F with nc_transpose
transposed = nl.ndarray((2048, 128), dtype=nl.float16, buffer=nl.sbuf)
nisa.nc_transpose(dst=transposed, data=tensor_2d)  # P↔F swap

# Step 3: Reshape free dimensions as needed
final = TensorView(transposed).reshape_dim(0, [64, 32]).get_view()  # (64, 32, 128)

# Why: SBUF partition dimension (dim 0) is physically distributed across 128 hardware
# partitions. Cannot logically rearrange without nc_transpose hardware instruction.
```

#### Anti-pattern 3: Excessive Data Movement

```python
# ❌ Anti-pattern: Unnecessary copies for layout change
temp1 = nl.ndarray((P, F), dtype=nl.float16, buffer=nl.sbuf)
nisa.tensor_copy(dst=temp1, src=input_sb)  # Copy 1

# Reshape by copying to new buffer
temp2 = nl.ndarray((P, F1, F2), dtype=nl.float16, buffer=nl.sbuf)
nisa.tensor_copy(dst=temp2, src=temp1)  # Copy 2

# Permute by copying again
output = nl.ndarray((P, F2, F1), dtype=nl.float16, buffer=nl.sbuf)
nisa.tensor_copy(dst=output, src=temp2)  # Copy 3

# ✅ Fix: Use TensorView for zero-copy operations
output = TensorView(input_sb) \
    .reshape_dim(1, [F1, F2]) \
    .permute([0, 2, 1]) \
    .get_view()
# Zero copies! Just stride manipulation at compile time.

# Why: TensorView operations are compile-time stride calculations. No runtime cost.
# Only use tensor_copy when you truly need data in different physical locations
# (e.g., to reuse SBUF space, or when modifying in-place).
```

#### Anti-pattern 4: Ignoring Generation Constraints

```python
# ❌ Anti-pattern: Hardcoded PSUM limits without generation check
PSUM_FREE_MAX = 512  # Assumes gen2/gen3, fails on gen4

psum_result = nl.ndarray((128, PSUM_FREE_MAX), dtype=nl.float16, buffer=nl.psum)
nisa.nc_transpose(dst=psum_result, data=input_large)  # May be suboptimal on gen4

# ✅ Fix: Check generation and use appropriate limits
import nki.isa as nisa

if nki.isa.get_nc_version() == nki.isa.nc_version.gen4:
    PSUM_FREE_MAX = 4096  # gen4 supports larger
else:
    PSUM_FREE_MAX = 512   # gen2/gen3

psum_result = nl.ndarray((128, PSUM_FREE_MAX), dtype=nl.float16, buffer=nl.psum)
nisa.nc_transpose(dst=psum_result, data=input_large)

# Why: gen4 (Trn3) supports 8x larger PSUM free dimension (4096 vs 512).
# Tiling more than necessary hurts performance. Check generation for optimal tile sizes.
```

### Debugging Strategies

1. **Print tensor shapes at each step**
   ```python
   print(f"Input shape: {input_sb.shape}")  # Compile-time print
   transformed = TensorView(input_sb).permute([0, 2, 1]).get_view()
   print(f"After permute: {transformed.shape}")
   ```

2. **Inspect TensorView patterns**
   ```python
   tv = TensorView(tensor).slice(dim=1, start=0, end=100, step=2)
   pattern, offset = tv._get_pattern_and_offset()
   print(f"Generated pattern: {pattern}, offset: {offset}")
   # Compare with expected .ap() pattern
   ```

3. **Validate with small test cases first**
   ```python
   # Test with minimal sizes to verify logic
   test_input = nl.ndarray((8, 16), dtype=nl.float16, buffer=nl.sbuf)  # Small
   # ... test transpose ...
   # Then scale up to full size (128, 512)
   ```

4. **Check generated .ap() patterns**
   ```python
   # Manual .ap() pattern for debugging
   manual_pattern = [[stride0, size0], [stride1, size1]]
   manual_view = tensor.ap(pattern=manual_pattern, offset=0)

   # Compare with TensorView-generated pattern
   tv_view = TensorView(tensor).slice(...).get_view()

   # Both should produce same access pattern
   ```

5. **Verify constraints before kernel launch**
   ```python
   from nkilib.core.utils.kernel_assert import kernel_assert  # or inline from references/nkilib/core/utils/kernel_assert.py

   kernel_assert(P <= 128, f"Partition dimension {P} exceeds limit 128")
   kernel_assert(F <= 32767, f"Free dimension {F} exceeds SBUF limit 32767")

   if buffer == nl.psum:
       psum_limit = 4096 if nki.isa.get_nc_version() == nki.isa.nc_version.gen4 else 512
       kernel_assert(F <= psum_limit, f"PSUM free dimension {F} exceeds limit {psum_limit}")
   ```

---

## Production Examples

### Self-Contained Reference Guide

| Topic | Primary Technique | Key Pattern | Reference |
|-------|-------------------|-------------|-----------|
| **TensorView** | Zero-copy views | slice, permute, broadcast, reshape, .ap() generation | [tensor-view.md](nkilib/core/tensor-view.md) |
| **Layout conversion** | TensorView + DMA | Interleaved↔contiguous via strided DMA or permutation matrix | [layout-conversion.md](nkilib/patterns/layout-conversion.md) |
| **Stream shuffle** | nc_stream_shuffle | Partition dimension broadcasting | [stream-shuffle-broadcast.md](nkilib/ops/stream-shuffle-broadcast.md) |
| **Tile tracking** | TiledDimInfo | Subtile indexing for nc_transpose destinations | [tile-info.md](nkilib/core/tile-info.md) |

### Minimal Working Examples

#### Example 1: Simple nc_transpose

```python
import nki
import nki.isa as nisa
import nki.language as nl

@nki.jit
def simple_transpose_kernel(input_hbm: nl.ndarray) -> nl.ndarray:
    """
    Transpose (P, F) → (F, P) using nc_transpose.

    Args:
        input_hbm: [128, 512] @ HBM

    Returns:
        [512, 128] @ HBM - transposed
    """
    P, F = input_hbm.shape

    # Load to SBUF
    input_sb = nl.ndarray((P, F), dtype=input_hbm.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=input_sb, src=input_hbm)

    # Transpose to PSUM
    output_psum = nl.ndarray((F, P), dtype=input_hbm.dtype, buffer=nl.psum)
    nisa.nc_transpose(dst=output_psum, data=input_sb)

    # Copy PSUM → SBUF
    output_sb = nl.ndarray((F, P), dtype=input_hbm.dtype, buffer=nl.sbuf)
    nisa.tensor_copy(dst=output_sb, src=output_psum)

    # Store to HBM
    output_hbm = nl.ndarray((F, P), dtype=input_hbm.dtype, buffer=nl.shared_hbm)
    nisa.dma_copy(dst=output_hbm, src=output_sb)

    return output_hbm
```

#### Example 2: TensorView Broadcasting

```python
import nki
import nki.isa as nisa
import nki.language as nl
from nkilib.core.utils.tensor_view import TensorView  # or inline from references/nkilib/core/utils/tensor_view.py

@nki.jit
def broadcast_multiply_kernel(data_hbm: nl.ndarray, scale_hbm: nl.ndarray) -> nl.ndarray:
    """
    Multiply data by scale, broadcasting scale across middle dimension.

    Args:
        data_hbm: [P, N, F] @ HBM
        scale_hbm: [P, 1, F] @ HBM - will be broadcast to [P, N, F]

    Returns:
        [P, N, F] @ HBM - data * scale (broadcast)
    """
    P, N, F = data_hbm.shape

    # Load data
    data_sb = nl.ndarray((P, N, F), dtype=data_hbm.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=data_sb, src=data_hbm)

    # Load scale (size 1 in middle dimension)
    scale_sb = nl.ndarray((P, 1, F), dtype=scale_hbm.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=scale_sb, src=scale_hbm)

    # Broadcast and multiply
    result_sb = nl.ndarray((P, N, F), dtype=data_hbm.dtype, buffer=nl.sbuf)
    nisa.tensor_tensor(
        dst=result_sb,
        data1=data_sb,
        data2=TensorView(scale_sb).broadcast(dim=1, size=N).get_view(),  # Broadcast
        op=nl.multiply
    )

    # Store result
    result_hbm = nl.ndarray((P, N, F), dtype=data_hbm.dtype, buffer=nl.shared_hbm)
    nisa.dma_copy(dst=result_hbm, src=result_sb)

    return result_hbm
```

#### Example 3: Strided DMA Gather

```python
import nki
import nki.isa as nisa
import nki.language as nl
from nkilib.core.utils.tensor_view import TensorView  # or inline from references/nkilib/core/utils/tensor_view.py

@nki.jit
def gather_even_odd_kernel(input_hbm: nl.ndarray) -> tuple:
    """
    Separate even and odd elements from interleaved input using strided DMA.

    Args:
        input_hbm: [d, B, S] @ HBM - interleaved [e0,o0,e1,o1,...]

    Returns:
        even_hbm: [d//2, B, S] @ HBM - [e0,e1,e2,...]
        odd_hbm: [d//2, B, S] @ HBM - [o0,o1,o2,...]
    """
    d, B, S = input_hbm.shape
    half_d = d // 2

    # Allocate SBUF buffers
    even_sb = nl.ndarray((half_d, B, S), dtype=input_hbm.dtype, buffer=nl.sbuf)
    odd_sb = nl.ndarray((half_d, B, S), dtype=input_hbm.dtype, buffer=nl.sbuf)

    # Gather even elements (start=0, step=2)
    nisa.dma_copy(
        dst=even_sb,
        src=TensorView(input_hbm).slice(dim=0, start=0, end=d, step=2).get_view()
    )

    # Gather odd elements (start=1, step=2)
    nisa.dma_copy(
        dst=odd_sb,
        src=TensorView(input_hbm).slice(dim=0, start=1, end=d, step=2).get_view()
    )

    # Store results
    even_hbm = nl.ndarray((half_d, B, S), dtype=input_hbm.dtype, buffer=nl.shared_hbm)
    odd_hbm = nl.ndarray((half_d, B, S), dtype=input_hbm.dtype, buffer=nl.shared_hbm)
    nisa.dma_copy(dst=even_hbm, src=even_sb)
    nisa.dma_copy(dst=odd_hbm, src=odd_sb)

    return even_hbm, odd_hbm
```

### Further Reading

All utility documentation is self-contained — no external library needed:

- [tensor-view.md](nkilib/core/tensor-view.md) - TensorView complete API and source
- [tile-info.md](nkilib/core/tile-info.md) - TiledDimInfo for subtile tracking in nc_transpose
- [stream-shuffle-broadcast.md](nkilib/ops/stream-shuffle-broadcast.md) - Partition broadcast utility
- [layout-conversion.md](nkilib/patterns/layout-conversion.md) - Interleaved↔contiguous conversion patterns (from RoPE)
