# Performance Basics for NKI

This reference covers fundamental performance patterns for NKI kernels. Focus on correctness first, then apply these optimizations.

## Priority 1: Contiguous DMA

Large, aligned memory transfers are most efficient.

**Pattern from cumsum kernel** (contiguous DMA):

```python
# GOOD: Contiguous slice, large transfer
nisa.dma_copy(
    dst=data_sb[0:p_tile.size, 0:f_size],
    src=x_2d[p_tile.start_offset:p_tile.start_offset + p_tile.size, f_start:f_end],
)

# BAD: Many small transfers
for i in range(p_size):
    for j in range(f_size):
        nisa.dma_copy(dst=data_sb[i:i+1, j:j+1], src=x_2d[i:i+1, j:j+1])
```

**Guidelines:**
- Transfer entire tiles at once, not element by element
- Align tile sizes to hardware boundaries (P=128, common F sizes: 512, 2048)
- Use reshape to enable contiguous access patterns

## Priority 2: Operation Fusion

Avoid unnecessary SBUF→HBM→SBUF roundtrips by keeping data in SBUF.

**Pattern from RoPE kernel** (dual-mode: standalone + fusible):

```python
# RoPE provides two interfaces:

# 1. Standalone kernel with full HBM I/O
@nki.jit
def RoPE(x_in, cos, sin, ...):
    """Full kernel: HBM -> compute -> HBM"""
    x_in_sb = nl.ndarray(..., buffer=nl.sbuf)
    nisa.dma_copy(dst=x_in_sb, src=x_in[...])  # Load from HBM

    x_out_sb = nl.ndarray(..., buffer=nl.sbuf)
    RoPE_sbuf(x_in_sb, cos_sb, sin_sb, x_out_sb)  # Compute in SBUF

    nisa.dma_copy(dst=x_out[...], src=x_out_sb)  # Store to HBM
    return x_out

# 2. Fusible helper operating in SBUF only
def RoPE_sbuf(x_in_sb, cos_sb, sin_sb, x_out_sb, ...):
    """SBUF-only: can be fused into megakernels"""
    # All operations on SBUF tensors, no HBM access
    ...
```

**Pattern:** Create both standalone (HBM I/O) and fusible (SBUF-only) versions of reusable operations.

## Priority 3: Minimize PSUM Pressure

PSUM has limited free dimension (512 on gen2/3, 4096 on gen4). Tile large matrix multiplies.

```python
# If N > 512, tile the matmul
F_MAX_PSUM = 512
num_f_tiles = div_ceil(N, F_MAX_PSUM)

for f_idx in nl.affine_range(num_f_tiles):
    f_start = f_idx * F_MAX_PSUM
    f_end = min(f_start + F_MAX_PSUM, N)
    f_size = f_end - f_start

    # MatMul into PSUM (respects 512 limit)
    psum_tile = nl.ndarray((M, f_size), dtype=nl.float32, buffer=nl.psum)
    nisa.nc_matmul(dst=psum_tile, stationary=a_tile, moving=b_tile[:, f_start:f_end])

    # Copy out of PSUM immediately
    result_sb = nl.ndarray((M, f_size), dtype=result_dtype, buffer=nl.sbuf)
    nisa.tensor_copy(dst=result_sb, src=psum_tile)
```

## Loop Type Selection

Choose the right loop type for your access pattern.

| Loop Type | When to Use | Unrolling |
|-----------|-------------|-----------|
| `nl.affine_range(N)` | Independent iterations, no dependencies between iterations | Full unroll |
| `nl.sequential_range(N)` | Loop-carried dependencies (e.g., cumsum, running max) | No unroll |
| `nl.static_range(N)` | Small constant N, want partial unroll control | Configurable |
| `TiledRange(total, tile)` | Partition dimension tiling with edge handling | Full unroll |

See [tiled-range.md](nkilib/core/tiled-range.md) for full TiledRange API documentation.

```python
# Parallel: each iteration independent
for i in nl.affine_range(num_tiles):
    process_tile(i)  # Can run in parallel

# Sequential: iteration i depends on i-1
for i in nl.sequential_range(num_tiles):
    carry = process_with_carry(i, carry)  # Must be sequential
```

## Advanced: Double Buffering & Software Pipelining

These techniques overlap memory transfers with compute to hide DMA latency. They are
**out of scope for initial kernel writing** — write correct single-buffered code first,
then apply these optimizations based on profiling data.

## Memory Hierarchy Performance

| Memory | Bandwidth | Latency | Use For |
|--------|-----------|---------|---------|
| SBUF | Highest | Lowest | Active compute |
| PSUM | High | Low | MatMul accumulation |
| HBM | Lower | Higher | Input/output storage |

**Guideline:** Minimize HBM accesses. Load once, compute multiple operations, store once.

## Common Anti-patterns

### 1. Loading same data multiple times

```python
# BAD: Loads x from HBM twice
nisa.dma_copy(dst=tile1, src=x[...])
y1 = compute1(tile1)
nisa.dma_copy(dst=tile2, src=x[...])  # Redundant!
y2 = compute2(tile2)

# GOOD: Load once, reuse
nisa.dma_copy(dst=tile, src=x[...])
y1 = compute1(tile)
y2 = compute2(tile)
```

### 2. Small DMA transfers

```python
# BAD: Many small transfers
for i in range(128):
    nisa.dma_copy(dst=sb[i:i+1, :], src=hbm[i:i+1, :])

# GOOD: Single large transfer
nisa.dma_copy(dst=sb[0:128, :], src=hbm[0:128, :])
```

### 3. Unnecessary type conversions

```python
# BAD: Convert types unnecessarily
fp32_temp = nl.ndarray(..., dtype=nl.float32)
nisa.tensor_copy(dst=fp32_temp, src=fp16_input)  # fp16 -> fp32
# ... compute ...
nisa.tensor_copy(dst=fp16_output, src=fp32_result)  # fp32 -> fp16

# BETTER: Keep in native type when precision allows
# Use fp32 only where needed (reductions, accumulation)
```

## Further Reading

- [tiled-range.md](nkilib/core/tiled-range.md) - TiledRange for loop type selection (see table above)
- [layout-conversion.md](nkilib/patterns/layout-conversion.md) - RoPE dual-mode pattern (standalone + fusible)

Use `/neuron-nki-docs optimization` for detailed performance documentation.
