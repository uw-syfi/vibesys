# Common NKI Patterns

Detailed code examples, anti-patterns, and production patterns for common NKI operations.

## Element-wise Operations

Element-wise operations use VectorE/ScalarE and work on any buffer type. For basic pattern:
- Reshape to 2D for simpler tiling (collapse batch dimensions)
- Tile partition dimension (≤128) and free dimension as needed
- Use `nisa.activation()` for element-wise functions (exp, sigmoid, tanh)
- Use `nisa.tensor_tensor()` for binary ops (add, multiply)

## Matrix Multiply - CRITICAL: PSUM Accumulation Pattern

**PERFORMANCE REQUIREMENT**: Matrix multiplication accumulation over the K (contraction) dimension requires careful loop structure to trigger efficient hardware PSUM accumulation. Using `nl.sequential_range` will serialize execution and prevent PSUM accumulation, causing severe performance degradation.

**The Correct Pattern (from production kernels):**

```python
# 1. Allocate PSUM buffer (uninitialized is correct)
result_psum = nl.ndarray((M_tile, N_tile), dtype=nl.float32, buffer=nl.psum)

# 2. Loop over K tiles using affine_range or range (NOT sequential_range)
for k_idx in nl.affine_range(num_k_tiles):
    # Load tiles for this K slice
    a_tile = load_stationary_tile(a, k_idx)  # [K_tile, M_tile]
    b_tile = load_moving_tile(b, k_idx)      # [K_tile, N_tile]

    # 3. Multiple nc_matmul writes to SAME psum buffer
    # Compiler detects multiple writes → triggers hardware accumulation
    nisa.nc_matmul(
        dst=result_psum,
        stationary=a_tile,
        moving=b_tile
    )

# 4. Copy PSUM → SBUF for further operations
result_sbuf = nl.ndarray((M_tile, N_tile), dtype=dtype, buffer=nl.sbuf)
nisa.tensor_copy(dst=result_sbuf, src=result_psum)
```

**Key points:**
- **PSUM allocation**: Uninitialized `nl.ndarray(..., buffer=nl.psum)` is correct (matches production kernels)
- **Loop type**: Use `nl.affine_range()` or `range()` for K-dimension loops, NEVER `nl.sequential_range`
- **Accumulation mechanism**: Multiple writes to the same PSUM buffer trigger hardware accumulation automatically
- **Operand dimensions**:
  - Stationary (left): `[K, M]` where K ≤ 128 (partition), M ≤ 128 (stationary free)
  - Moving (right): `[K, N]` where K ≤ 128 (partition), N ≤ 512 (moving free, gen2/3) or ≤ 4096 (gen4)
  - Result: `[M, N]` in PSUM

**Complete tiled matmul example:**

```python
# Matrix multiply: C[M, N] = A[M, K] @ B[K, N] with tiling
num_m_tiles = div_ceil(M, 128)
num_n_tiles = div_ceil(N, 512)
num_k_tiles = div_ceil(K, 128)

for m_idx in nl.affine_range(num_m_tiles):
    for n_idx in nl.affine_range(num_n_tiles):
        # Allocate PSUM for this output tile
        psum = nl.ndarray((128, 512), dtype=nl.float32, buffer=nl.psum)

        # Accumulate over K dimension
        for k_idx in nl.affine_range(num_k_tiles):  # CRITICAL: affine_range, not sequential_range
            # Load input tiles
            a_tile = nl.ndarray((128, 128), dtype=dtype, buffer=nl.sbuf)
            b_tile = nl.ndarray((128, 512), dtype=dtype, buffer=nl.sbuf)
            nisa.dma_copy(dst=a_tile, src=A[k_idx*128:(k_idx+1)*128, m_idx*128:(m_idx+1)*128])
            nisa.dma_copy(dst=b_tile, src=B[k_idx*128:(k_idx+1)*128, n_idx*512:(n_idx+1)*512])

            # Matmul accumulates into same PSUM
            nisa.nc_matmul(dst=psum, stationary=a_tile, moving=b_tile)

        # Copy result and store
        result = nl.ndarray((128, 512), dtype=dtype, buffer=nl.sbuf)
        nisa.tensor_copy(dst=result, src=psum)
        nisa.dma_copy(dst=C[m_idx*128:(m_idx+1)*128, n_idx*512:(n_idx+1)*512], src=result)
```

**Anti-patterns (NEVER DO THIS):**

```python
# WRONG: Using nl.sequential_range for K accumulation
for k_idx in nl.sequential_range(num_k_tiles):  # Serializes execution!
    nisa.nc_matmul(dst=psum, stationary=a_tile, moving=b_tile)

# WRONG: Writing PSUM to HBM between accumulation steps
for k_idx in nl.affine_range(num_k_tiles):
    nisa.nc_matmul(dst=psum, stationary=a_tile, moving=b_tile)
    nisa.dma_copy(dst=hbm_temp, src=psum)  # Unnecessary HBM traffic!

# WRONG: Direct PSUM to HBM dma_copy — no longer supported
nisa.dma_copy(dst=hbm_output, src=psum)  # FORBIDDEN: direct PSUM→HBM
# CORRECT: Always copy PSUM → SBUF first, then SBUF → HBM
sbuf_temp = nl.ndarray(psum.shape, dtype=dtype, buffer=nl.sbuf)
nisa.tensor_copy(dst=sbuf_temp, src=psum)
nisa.dma_copy(dst=hbm_output, src=sbuf_temp)
```

**Why this matters**:
- `nl.sequential_range` forces serialization, preventing the compiler from detecting the accumulation pattern
- Hardware PSUM accumulation is performed in FP32 with very low overhead
- Fallback to VectorEngine accumulation via `tensor_tensor` is significantly slower

**Reference**: See `examples/simple_matmul.py` for a complete working example following this pattern.

## Fused Operations on ScalarE

ScalarE supports "pipelined multiply-add" before applying non-linear functions, allowing two operations at the cost of one. This is useful when translating PyTorch operations like `torch.exp(x * scale)` or `torch.sigmoid(x + bias)`.

**Pattern from Mamba (combine multiplication and exponential):**
```python
# PyTorch: torch.exp(delta * A)
#
# Efficient NKI: Fused multiply + exp in single instruction
deltaA = nl.ndarray((channels, seq_len), dtype=delta.dtype, buffer=nl.sbuf)
nisa.activation(
    dst=deltaA,
    op=nl.exp,           # Non-linear function
    data=delta_i,        # Input tensor
    scale=A_i            # Multiply by this before applying op
)
# Computes: deltaA = exp(delta_i * A_i) in one instruction

# Anti-pattern: Two separate instructions (2x slower)
temp = nl.ndarray((channels, seq_len), dtype=delta.dtype, buffer=nl.sbuf)
nisa.tensor_scalar(dst=temp, data=delta_i, op=nl.multiply, operand=A_i)
nisa.activation(dst=deltaA, op=nl.exp, data=temp)
```

**Available fused patterns:**
- `nisa.activation(op=nl.exp, data=x, scale=s)` → `exp(x * s)`
- `nisa.activation(op=nl.sigmoid, data=x, bias=b)` → `sigmoid(x + b)`
- `nisa.activation(op=nl.tanh, data=x, scale=s, bias=b)` → `tanh(x * s + b)`

**When to use:** Any time you need element-wise multiply/add followed by activation function in PyTorch code.

## Sequential Operations & Associative Scan

For operations with loop-carried dependencies (e.g., cumulative sum, RNN, state space models), use `nisa.tensor_tensor_scan` instead of explicit sequential loops.

**PyTorch pattern:**
```python
# torch.cumsum equivalent, or RNN-like: out[i] = f(out[i-1], x[i])
out = torch.empty_like(x)
for i in range(seq_len):
    prev_state = out[i-1] if i > 0 else 0
    out[i] = deltaA[i] * prev_state + deltaBu[i]
```

**NKI translation using associative scan:**

See `examples/associative_scan.py` for complete pattern demonstrating:
- Single-instruction sequential operations (no explicit loops)
- Internal caching of intermediate scan results in VectorE
- Initial state handling for multi-tile sequences

**Key operation:** `out[i] = op0(data[i], out[i-1]) op1 data1[i]`

```python
# Efficient: Single instruction for entire sequence
scan_result = nl.ndarray(deltaA_tile.shape, dtype=deltaA_tile.dtype, buffer=nl.sbuf)
nisa.tensor_tensor_scan(
    dst=scan_result,
    data0=deltaA_tile,      # First operand
    data1=deltaBu_tile,     # Second operand
    initial=0,        # Starting state (out[-1])
    op0=nl.multiply,  # First operation (with previous state)
    op1=nl.add        # Second operation (with data1)
)
# Computes: out[i] = deltaA[i] * out[i-1] + deltaBu[i]

# Anti-pattern: Explicit loop (seq_len instructions, very slow)
for i in nl.sequential_range(seq_len - 1):
    scan_result[0:channels, i+1] = (
        deltaA[0:channels, i+1] * scan_result[0:channels, i]
        + deltaBu[0:channels, i+1]
    )
```

**Common use cases:**
- `torch.cumsum(x)` → `tensor_tensor_scan(ones, x, initial=0, op0=nl.multiply, op1=nl.add)`
- `torch.cumprod(x)` → `tensor_tensor_scan(x, zeros, initial=1, op0=nl.multiply, op1=nl.add)`
- RNN cell: `h[t] = tanh(W_h @ h[t-1] + W_x @ x[t])` → Use scan with matmul in loop body
- State space models (Mamba, S4) → Associative scan over sequence dimension

**Multi-tile sequences with loop-carried dependencies:**
```python
scan_init = nl.zeros((channels, 1), dtype=deltaA.dtype, buffer=nl.sbuf)

for i_seq_tile in nl.static_range(n_seq_tiles):
    seq_start = i_seq_tile * seq_tile_size

    # Load current tiles
    nisa.dma_copy(dst=deltaA_tile, src=deltaA[..., seq_start:seq_start+seq_tile_size])
    nisa.dma_copy(dst=deltaBu_tile, src=deltaBu[..., seq_start:seq_start+seq_tile_size])

    # Scan with previous tile's final state
    scan_tile = nl.ndarray(deltaA_tile.shape, dtype=deltaA_tile.dtype, buffer=nl.sbuf)
    nisa.tensor_tensor_scan(
        dst=scan_tile,
        data0=deltaA_tile, data1=deltaBu_tile,
        initial=scan_init,  # Carry dependency from previous iteration
        op0=nl.multiply, op1=nl.add
    )

    # Update initial state for next iteration
    scan_init = scan_tile[0:channels, seq_tile_size-1]

    # Store result
    nisa.dma_copy(dst=output[..., seq_start:seq_start+seq_tile_size], src=scan_tile)
```

**Why this matters:** Explicit sequential loops create `seq_len` instructions with single-element-per-partition tiles and data dependencies, leading to static instruction overhead dominating execution time. `nisa.tensor_tensor_scan` performs the entire sequence in a single VectorEngine instruction by caching intermediate results internally.

## Production Kernel Patterns

These patterns from production kernels illustrate key NKI techniques. All utility APIs referenced
are documented in `references/nkilib/`.

### cumsum — Simple tiling with TiledRange

Uses `TiledRange` for partition tiling, `sequential_range` for the carry-dependent free dimension,
and `tensor_tensor_scan` for the cumulative sum itself.

```python
for p_tile in TiledRange(outer_dim, P_MAX):  # See references/nkilib/core/tiled-range.md
    init_sb = nl.ndarray((P_MAX, 1), dtype=nl.float32, buffer=nl.sbuf)
    nisa.memset(dst=init_sb, value=0.0)

    for f_tile_idx in nl.sequential_range(num_f_tiles):  # Carry dependency
        f_start = f_tile_idx * F_TILE_SIZE
        f_end = min(f_start + F_TILE_SIZE, last_dim)
        f_size = f_end - f_start

        data_sb = nl.ndarray((P_MAX, F_TILE_SIZE), dtype=x.dtype, buffer=nl.sbuf)
        nisa.dma_copy(
            dst=data_sb[0:p_tile.size, 0:f_size],
            src=x_2d[p_tile.start_offset:p_tile.start_offset + p_tile.size, f_start:f_end],
        )

        result_sb = nl.ndarray((P_MAX, F_TILE_SIZE), dtype=x.dtype, buffer=nl.sbuf)
        nisa.tensor_tensor_scan(
            dst=result_sb[0:p_tile.size, 0:f_size],
            data0=ones_sb[0:p_tile.size, 0:f_size],  # multiply by 1 (identity)
            data1=data_sb[0:p_tile.size, 0:f_size],   # add input
            initial=init_sb[0:p_tile.size, 0:1],       # carry from previous tile
            op0=nl.multiply, op1=nl.add,
        )

        nisa.dma_copy(
            dst=y_2d[p_tile.start_offset:p_tile.start_offset + p_tile.size, f_start:f_end],
            src=result_sb[0:p_tile.size, 0:f_size],
        )

        # Update carry for next tile
        if f_tile_idx + 1 < num_f_tiles:
            nisa.tensor_copy(dst=init_sb[0:p_tile.size, 0:1],
                             src=result_sb[0:p_tile.size, f_size-1:f_size])
```

### rmsnorm_quant — Reduction + FP8 quantization

Combines RMS normalization with row-wise FP8 quantization. Demonstrates the pattern of
reshaping to 2D, tiling the outer dimension, and computing reduction + scale per row.

```python
# Key pattern: collapse to 2D, tile outer dimension
tsr_proc_shape = (outer_dim, processing_dim)  # [B*S, H]
in_tsr_hbm_view = hidden.reshape(tsr_proc_shape)

# Per-tile: RMS normalize then quantize
# 1. Compute variance: reduce(x^2) / H
# 2. Normalize: x * rsqrt(variance + eps) * gamma
# 3. Find row max: reduce(abs(normalized), op=max)
# 4. Compute scale: max_fp8_value / row_max
# 5. Quantize: cast(normalized * scale, fp8_e4m3)
# 6. Store scale as 4 fp8 values (reinterpret of fp32) appended to output row
```

See `references/nkilib/patterns/quantization-helpers.md` and `references/nkilib/patterns/normalization-patterns.md`
for self-contained utility functions used in this pattern.
