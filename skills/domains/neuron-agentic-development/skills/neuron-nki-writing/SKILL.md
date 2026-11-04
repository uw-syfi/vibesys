---
name: neuron-nki-writing
description: |
  Guide for writing and modifying NKI kernels. Covers new kernel creation from
  PyTorch/NumPy/natural language, editing existing kernels, adding shape/dtype support,
  refactoring tiling strategies, and implementing new features in NKI code.
  Use when user says "write NKI kernel", "convert PyTorch to NKI", "translate numpy to NKI",
  "create NKI kernel", "implement in NKI", "NKI version of", "how to write NKI kernel",
  "add support for <shape/dtype>", "modify this NKI kernel", "extend kernel to handle",
  "refactor tiling", "change tile size", "add batch dimension", "support variable length",
  "fix this kernel logic", "update kernel for gen4", or needs NKI API guidance for kernel changes.
argument-hint: "[operation, PyTorch/Numpy code, or existing kernel file]"
---

# Writing NKI Kernels

This skill guides writing and modifying NKI (Neuron Kernel Interface) kernels — from new kernel creation (PyTorch/NumPy/natural language translation) to editing existing kernels (adding shape/dtype support, refactoring tiling, implementing new features). Focus on correctness using documented APIs.

## Critical: NKI Language Constraints

BEFORE writing any NKI code, read `references/nki-language-constraint.md` for the complete list of required and forbidden API patterns. Each rule is labelled with the release that made it a breaking change (`0.x.0+`), so the file covers every current release rather than one migration step. Violating ANY rule is a compilation failure.

### Hardware limits: don't hardcode the PSUM free dim

The PSUM free-dimension limit is generation- and dtype-gated and has changed
across releases. Current values:

- **gen2 / gen3:** 512 (one PSUM bank)
- **gen4:** 4096 for a `float32` `dst`, 8192 for a `bfloat16` `dst` (entire PSUM)

This is **not** a queryable `tile_size` field — `nl.tile_size.psum_bank_fmax` is
a fixed 512 (fp32 elements per bank) and is neither dtype- nor generation-aware,
so do not use it as the free-dim limit. Authoritative source: the `nc_matmul`,
`nc_matmul_mx`, and `dma_transpose` API blocks via `/neuron-nki-docs`. Treat any
PSUM free-dim number inlined in this skill's reference files as illustrative;
confirm against those API blocks.

`tile_size` *does* authoritatively report other limits — use it for those:
`nl.tile_size.pmax` (128), `nl.tile_size.psum_num_banks` (bank cycling),
`nl.tile_size.gemm_moving_fmax` (matmul moving-operand SBUF free dim), and
`nl.tile_size.sbuf_fmax` / `sbuf_fmax_bytes` (SBUF capacity).

## Quick Start

Minimal working kernel structure:

```python
import nki
import nki.isa as nisa
import nki.language as nl

@nki.jit
def my_kernel(input_hbm: nl.ndarray) -> nl.ndarray:
    """One-line description of kernel operation."""
    # 1. Allocate SBUF tile
    tile = nl.ndarray(input_hbm.shape, dtype=input_hbm.dtype, buffer=nl.sbuf)

    # 2. Load from HBM to SBUF
    nisa.dma_copy(dst=tile, src=input_hbm[0:input_hbm.shape[0], 0:input_hbm.shape[1]])

    # 3. Compute (example: element-wise exp)
    result = nl.ndarray(tile.shape, dtype=tile.dtype, buffer=nl.sbuf)
    nisa.activation(dst=result, data=tile, op=nl.exp)

    # 4. Allocate and store to HBM
    output = nl.ndarray(input_hbm.shape, dtype=input_hbm.dtype, buffer=nl.shared_hbm)
    nisa.dma_copy(dst=output, src=result)

    return output
```

## Complexity Assessment

Before reading references, classify the task to avoid unnecessary overhead:

**Simple** (element-wise op, single reduction, activation, layernorm, add/multiply):
- Use the Quick Start template and Step 4 API table directly
- Skip utility library references entirely
- **Start writing code immediately** — consult references only when stuck
- Target: working kernel within 5 minutes

**Medium** (matmul, softmax, multi-step fusion, transpose with tiling):
- Read `references/common-patterns.md`, `references/api-translation.md` and `references/memory-patterns.md`
- Skip utility library references unless tiling is complex
- Target: working kernel within 15 minutes

**Complex** (multi-head attention, transformer blocks, state-space models, MoE):
- Full reference loading appropriate
- Read utility selection guide and relevant patterns
- Target: working kernel within 30 minutes

**Start writing code as soon as possible.** Reference reading should supplement coding, not precede it. Write the kernel structure first, then consult references for specific API details as needed.

## Translation Workflow

### Step 1: Identify Operations

Map PyTorch/NumPy operations to NKI equivalents using `references/api-translation.md`.

### Step 2: Design Tiling Strategy

NKI operates on tiles with hardware constraints:

| Constraint | Limit | Notes |
|------------|-------|-------|
| Partition dimension (P) | ≤ 128 | First dimension of SBUF tensor |
| PSUM free dimension | 512 (gen2/3); gen4: 4096 fp32 / 8192 bf16 | For matrix multiply results — authoritative: `nc_matmul` doc block (see "Hardware limits" above) |

> **SBUF free dim** and **MatMul K (contraction)** are not fixed constants — look
> them up rather than hardcoding. SBUF free-dim capacity is a per-partition byte
> limit (`nl.tile_size.sbuf_fmax` / `sbuf_fmax_bytes`, generation-/dtype-dependent).
> Per-instruction MatMul K is the partition dim, **K ≤ 128** (effective 256/512
> with `double_row` / Quad-MX packing). Authoritative source: the `nc_matmul`
> tile-size block in the `/neuron-nki-docs` skill (`api-nki-isa-tensor.md`).

For tensors exceeding these limits, tile explicitly. Two mechanisms, both remainder-safe:

- **`nl.tiles` / `nl.blocks` (NKI 0.7.0+, language-native)** — view the tensor as a grid
  addressed by tile coordinate, e.g. `src_tiles = nl.tiles(src, tile_shape=(128, 256))` then
  `src_tiles[i, j].data`. Prefer this when the alternative is hand-computed offsets and
  strides, and use `nl.alloc_tiles` / `nl.alloc_blocks` to allocate on-chip grids. Boundary
  tiles clamp to the real extent automatically. See `references/indexing-patterns.md`
  ("Tiled and Blocked Layouts").
- **`TiledRange` (nkilib helper)** — iterate a tiled dimension, giving `.start_offset` /
  `.size` per step for plain slicing. Still valid and widely used in nkilib kernels; see
  Utility Selection Guide below.

### Step 3: Implement Memory Access

Consult the **Utility Selection Guide** to choose the right utilities for the kernel's access patterns,
then follow `references/memory-patterns.md` and `references/transpose-and-layout.md`:

- **Contiguous DMA:** For aligned, sequential access (most efficient)
- **Strided DMA:** Use `TensorView.slice(step=N)` for gather/scatter patterns (see transpose-and-layout.md)
- **Tiling loops:** Use `TiledRange` for dimensions requiring tiling with remainder handling
- **Transpose operations:** Use `nisa.nc_transpose()` for P↔F transpose, `TensorView` for layout manipulation
- **Partition broadcast:** Use `stream_shuffle_broadcast` when a bias/scale in partition 0 must reach all PEs
- **Buffer management:** Use `SbufManager` when the kernel has 4+ SBUF allocations or shared sub-functions

### Step 4: Add Compute Operations

Use ISA functions with explicit `dst` parameter:

```python
# Element-wise
nisa.activation(dst=result, data=input, op=nl.exp)
nisa.tensor_tensor(dst=result, data1=a, data2=b, op=nl.add)
nisa.tensor_scalar(dst=result, data=input, op0=nl.multiply, operand0=2.0)

# Reductions
nisa.tensor_reduce(dst=result, data=input, op=nl.add, axis=1)

# Matrix multiply
nisa.nc_matmul(dst=psum_result, stationary=a, moving=b)
```

### Step 5: Validate Complex Translations

**Important: Always compute reference results on CPU**, not on the XLA device. Every on-device XLA graph generates a separate NEFF file, making it hard to identify the NKI kernel's NEFF during profiling. Use `tensor.cpu()` before computing PyTorch/NumPy references.

For simple kernels (single operation, few tiles), comparing the final output against a PyTorch/NumPy reference is usually sufficient. For complex kernels with multiple computation stages, validate incrementally:

1. **Identify logical stages** in the source operation. For example, a fused attention kernel has: QK matmul → scale → mask → softmax → AV matmul.
2. **Translate and validate one stage at a time.** Write the first stage, store its output to HBM, and compare against the corresponding PyTorch intermediate. Only proceed to the next stage once the current one matches.
3. **Compose validated stages.** Once each stage is verified independently, connect them (keeping intermediates in SBUF instead of round-tripping through HBM) and validate the final output.

This catches translation errors early — a mismatch in the final output of a 5-stage kernel is much harder to diagnose than a mismatch after stage 2.

Use multiple complementary checks (atol/rtol, max absolute difference, tensor norm of the difference, cosine similarity) rather than relying on a single metric.

## Hardware Constraints Quick Reference

| Buffer | Max P | Max F | Use Case |
|--------|-------|-------|----------|
| `nl.sbuf` | 128 | `nl.tile_size.sbuf_fmax` (per-partition byte limit; gen/dtype-dependent) | General compute |
| `nl.psum` | 128 | 512 (gen2/3); gen4: 4096 fp32 / 8192 bf16 | MatMul accumulation |
| `nl.shared_hbm` | - | - | Input/output tensors |

## Loop Types

| Loop Type | Use Case | Unrolling |
|-----------|----------|-----------|
| `range(N)` | **Any compile-time-bounded loop** — the default | Full unroll |
| `nl.fori_loop(lower, upper, body_fun, step=1)` | Counted loop with **runtime bound** | Structured on-chip loop (not unrolled) |
| `nl.while_loop(init, body_fun)` | **Data-dependent** condition-driven loop | Structured on-chip loop (not unrolled) |
| `nl.no_reorder()` (context manager) | Constrain scheduling of a region | n/a — a scheduling directive, not a loop |

**`nl.affine_range` / `nl.sequential_range` / `nl.static_range` are deprecated aliases for
`range()` — use `range()`.** All three are literally `return range(start, stop, step)` on the
default frontend and in the CPU simulator, and each carries an explicit *"This API is deprecated
and will be removed in future releases. Prefer using `range()` directly instead"* warning. They do
**not** differ in unrolling or scheduling:

- There is no "no unroll" or "partial unroll" variant. Every one of them fully unrolls.
- `nl.sequential_range` does **not** serialize execution or express a loop-carried dependency.
  Choosing it over `nl.affine_range` changes nothing. To constrain scheduling, use the
  `nl.no_reorder()` region; to carry state across iterations, keep it in SBUF/PSUM and mutate in
  place — correctness comes from the data dependency, not from the loop function.

**Runtime/data-dependent loops (NKI 0.7.0+):** use `nl.fori_loop` (counted, runtime bound) or
`nl.while_loop` (condition-driven), NOT `for i in nl.dynamic_range(...)` or bare `while reg:` —
those cannot work on the default `standard` frontend, where a runtime register has no value at
trace time. See `references/nki-language-constraint.md` for the before/after patterns and rules, and
`/neuron-nki-docs` (api-nki-language-dims.md) for the `fori_loop` / `while_loop` API signatures.

## Common Patterns

For detailed code examples, anti-patterns, and production patterns (cumsum, rmsnorm_quant), see `references/common-patterns.md`.

### Element-wise Operations

- Reshape to 2D, tile P dimension (≤128), use `nisa.activation()` / `nisa.tensor_tensor()`

### Matrix Multiply — Key Rules

- Allocate PSUM: `nl.ndarray(..., buffer=nl.psum)` — uninitialized is correct; **do NOT `nisa.memset` the PSUM tile before the K loop**
- K-dimension loop: plain `range()` — the deprecated `nl.*_range` aliases are the same function and none of them serializes
- Control accumulation explicitly with `accumulate=(k_idx > 0)`: the first K tile overwrites (initializes) PSUM, later tiles accumulate — this replaces any separate memset/zero-init of PSUM
- Never write PSUM to HBM between accumulation steps
- Operands: stationary `[K≤128, M≤128]`, moving `[K≤128, N≤512/4096]`, result `[M, N]` in PSUM
- Copy PSUM→SBUF via `nisa.tensor_copy()` before further ops
- See `examples/simple_matmul.py` for complete examples

### Fused ScalarE Operations

- `nisa.activation(op=nl.exp, data=x, scale=s)` → `exp(x * s)` in one instruction
- Available: scale, bias, or both before any activation function

### Sequential Operations & Associative Scan

- Use `nisa.tensor_tensor_scan` instead of explicit sequential loops
- Pattern: `out[i] = op0(data[i], out[i-1]) op1 data1[i]`
- Multi-tile: pass final state as `initial=` to next tile's scan
- See `examples/associative_scan.py` for complete pattern

## Skill References

References are tiered to minimize overhead on simple tasks. Load only what you need based on the Complexity Assessment above.

### Always load (core references):
- `references/nki-language-constraint.md` - **MANDATORY**: Required and forbidden API patterns (version-gated per release), frontend selection, reference kernel template
- `references/common-patterns.md` - Full code examples: matmul PSUM accumulation, fused ScalarE, associative scan, production patterns
- `references/api-translation.md` - PyTorch/NumPy to NKI operation mapping
- `references/kernel-template.md` - Standard kernel template with self-contained utilities
- `references/indexing-patterns.md` - **Complete indexing guide**: memory-type rules (HBM/SBUF/PSUM), tiled/blocked layouts (`nl.tiles`, `nl.blocks`, `nl.alloc_tiles`), operation constraints (matmul/transpose/reduce), dynamic indexing with DGE modes
- `references/tile-grain-selection.md` - How to CHOOSE the `tile_shape` / `block_shape` values: size the tile to the consuming `nisa.*` op rather than to the tensor, keep the tunable grain at the block level, two-level loop structure. Authoring heuristics, not compilation rules

### Load when tiling or DMA patterns are needed (medium+ complexity):
- `references/memory-patterns.md` - DMA and tiling patterns with code examples
- `references/nkilib/core/tiled-range.md` - TiledRange: dimension tiling with remainder handling
- `references/nkilib/core/kernel-helpers.md` - Math helpers, SPMD, dtype utilities

### Load when layout manipulation is needed:
- `references/transpose-and-layout.md` - **Transpose and layout transformation guide**: nc_transpose, TensorView, array patterns, strided DMA, decision trees for layout operations
- `references/nkilib/core/tensor-view.md` - TensorView: zero-copy tensor manipulation

### API features documented in `/neuron-nki-docs` (query it, not duplicated here):
- **Native `NkiTensor` view methods** — composable, zero-copy views callable directly on a tensor: `slice`, `select`, `permute`, `broadcast`, `expand_dim`, `squeeze_dim`, `reshape_dim`, `flatten_dims`, `rearrange`, `reshape`, `view`, `vector_select`, plus query methods `is_contiguous` / `is_indirect` and low-level `ap` / `get_pattern`. These are the native tensor methods (e.g. `t.slice(1, 0, 256)`); the `TensorView` helper above wraps the same ops for composing complex 3D+ `.ap()` patterns. Look up signatures/examples via `/neuron-nki-docs` → `api-nki-tensor.md`.
- **Tensor indirection on compute ops (`.indirect()`)** — on NeuronCore-v4+, gather/scatter for **on-chip compute** (not just DMA): pass a `.indirect(index)` view as `dst` or `data` to `nc_matmul`, `nc_matmul_mx`, `tensor_tensor`, `tensor_scalar`, `tensor_reduce`, `tensor_copy`, `tensor_copy_predicated`, `tensor_scalar_reduce`, `tensor_scalar_cumulative`, `activation`, `activation_reduce`, `activate2`, `exponential`. Subject to quadrant/partition-alignment rules (group size 16 for vector/scalar/gpsimd, 32 for tensor engine). This extends the DMA-only `vector_offset` indirection to compute. Look up `NkiTensor.indirect` and the per-op notes via `/neuron-nki-docs`.

### Load when advanced patterns are needed (complex kernels only):
- `references/performance-basics.md` - Optimization patterns (fusion, double buffering)
- `references/nkilib/core/allocator.md` - SbufManager: stack/heap SBUF allocation
- `references/nkilib/core/tile-info.md` - TiledDimInfo: tile tracking with subtile support
- `references/nkilib/ops/` - Copy/broadcast operations (stream-shuffle, tp-broadcast)
- `references/nkilib/types/` - Enum types and logging utilities
- `references/nkilib/patterns/` - Reusable kernel patterns (quantization, normalization, layout conversion, MoE)

### Bundled Source Code

Full source for nkilib/core utilities and subkernels:

- `references/nkilib/core/utils/` - Utility source (TensorView, TiledRange, kernel_helpers, SbufManager, etc.)
- `references/nkilib/core/subkernels/` - Reusable sub-operations (RMSNorm, LayerNorm, normalization utils)

## Configuration

**Default**: inline nkilib utility source directly into the user's kernel file from the bundled source above.
**If nkilib is installed** in the user's environment, use `from nkilib.core.utils.X import Y` imports instead.

## Utility Selection Guide

### Always Use

| Utility | Adopt When |
|---------|-----------|
| `div_ceil(n, d)` | Any tile count computation. **Never** write `(n + d - 1) // d` inline. |
| `kernel_assert()` | Any input validation. **Never** use Python `assert`. |

### Use When Pattern Matches

| Utility | Adopt When | Reference |
|---------|-----------|-----------|
| `nl.tiles` / `nl.blocks` | Addressing a tensor by tile (or block) coordinate instead of computing offsets and strides by hand | `references/indexing-patterns.md` ("Tiled and Blocked Layouts") |
| `nl.alloc_tiles` / `nl.alloc_blocks` | Allocating on-chip storage that is itself a tile/block grid | `references/indexing-patterns.md` ("Tiled and Blocked Layouts") |
| `TiledRange` | Tiled dimension iteration with remainder handling | `references/nkilib/core/tiled-range.md` |
| `TensorView` | Strided/interleaved DMA, broadcasting, reshape without copy, dynamic selection | `references/nkilib/core/tensor-view.md` |
| `stream_shuffle_broadcast` | Replicate partition-0 value (bias, scale) to all 128 partitions | `references/nkilib/ops/stream-shuffle-broadcast.md` |
| `SbufManager` | 4+ SBUF tensors or sub-functions sharing SBUF | `references/nkilib/core/allocator.md` |
| `NkiTensor` view methods | Zero-copy reshape/slice/permute/broadcast directly on a tensor (`t.slice(...)`, `t.reshape_dim(...)`, etc.) | `/neuron-nki-docs` → `api-nki-tensor.md` |
| `.indirect()` on compute ops | On-chip gather/scatter (NeuronCore-v4+) for matmul/tensor/activation ops via an index tensor | `/neuron-nki-docs` → `NkiTensor.indirect` |

**Specialized:** `TiledDimInfo` (subtile metadata), `tp_broadcast` (P→F broadcast, very rare).

### Decision Flowchart

```
Kernel needs tiling?
├─ Tiles addressed by coordinate (and would otherwise need manual offset/stride math)?
│  └─ Yes → nl.tiles(src, tile_shape=...) → src_tiles[i, j].data
│           Allocate on-chip grids with nl.alloc_tiles(...)
├─ Coarser transfer/reuse grain above the compute tile?
│  └─ Yes → nl.blocks(src, tile_shape=..., block_shape=...) → block.data for one DMA
├─ Plain iteration over a tiled dimension → TiledRange for each tiled dimension
│        Use div_ceil() for tile count computations
├─ Nested tiles (subtiles within tiles)?
│  └─ Yes → TiledRange supports nesting: TiledRange(outer_tile, subtile_size)
│           For CTE-style metadata tracking → also consider TiledDimInfo
└─ No tiling → Plain range()

Kernel accesses tensors non-contiguously?
├─ Strided/interleaved → TensorView.slice(step=N)
├─ Broadcasting → TensorView.broadcast(dim, size)
├─ Reshape without copy → TensorView.reshape_dim() / flatten_dims()
├─ Dynamic expert selection → TensorView.select(dim, scalar_offset)
└─ Simple contiguous → plain tensor[start:end, :]

Kernel needs to broadcast a scalar/vector to all partitions?
├─ 1D value in partition 0 → stream_shuffle_broadcast(src, dst)
└─ Column vector (P-dim) to row (F-dim) → tp_broadcast(src, dst, ...)

Kernel allocates many SBUF buffers?
├─ 4+ buffers, or sub-functions share SBUF → SbufManager
└─ 1-3 simple buffers → plain nl.ndarray(..., buffer=nl.sbuf)
```

## Coding Conventions

Follow these conventions unless the user's instructions or existing project style indicate otherwise.

- **Prefer `kernel_assert()` over Python `assert`** - Produces structured error messages (`[NCC_INKI016] Kernel validation exception: ...`) that clearly identify errors originating from NKI kernels, which is helpful when kernels run inside larger frameworks. The kernel template (`references/kernel-template.md`) provides an inline definition; alternatively, import from nkilib if installed.
- **Include docstrings** with Args/Returns/Notes sections for non-trivial kernels
- **Validate inputs** with shape checks before the kernel body
- **Use descriptive variable names** (e.g., `partition_idx` not `p`)

## Kernel Efficiency Guidelines

These are basic efficiency practices to follow when writing any kernel. They do not require
advanced allocation or pipelining — just sensible layout, tiling, and data flow choices.

### 1. Tensor Layout Flexibility (Conditional)

If the input/output tensor layout would make the kernel significantly harder to write
(e.g., requiring many strided DMAs or complex reshaping), ask the user:

> "The current tensor layout [describe issue] would require [strided DMA / reshaping].
> Would you allow changing the layout to [proposed layout]? This would simplify the
> kernel and improve performance."

Layout changes that typically help:
- Putting the reduction dimension last (contiguous in memory)
- Aligning dimensions to 128 (partition) and 512 (PSUM free)
- Transposing to avoid strided DMA patterns

### 2. Large Contiguous Free Dimension in DMA (≥2KB)

DMA efficiency depends on the **free dimension** (second dimension onwards) being large and **contiguous**
in memory. Target ≥2KB contiguous free dimension to saturate memory bandwidth.

**Key concept:** In a tile `[P, F]`, the partition dimension P (first dim) is distributed across
hardware partitions. The free dimension F (second dim onwards) should be large and contiguous.

| Data Type | Minimum Free Dimension (Contiguous) |
|-----------|-------------------------------------|
| float32   | 512 elements (2KB)                  |
| bfloat16  | 1024 elements (2KB)                 |
| float8    | 2048 elements (2KB)                 |

**What "contiguous" means:** The free dimension elements are adjacent in HBM memory with stride=1.

**Production example** from `mlp_tkg_gate_up_projection.py:169-181`:
```python
# Weight layout: [H, I] where I is the contiguous free dimension
# Load weight tile [HTile=2048, I] where I is large and contiguous
# HTile = 2048 for non-quantized (reshapes to [128, 16, I] in SBUF)

nisa.dma_copy(
    dst=weight_tiles[weight_idx][0:H0, 0:h1_tiles, 0:I],  # SBUF: [128, h1_tiles, I]
    src=unsharded_weight.ap(
        pattern=[
            [H1 * unsharded_weight.shape[1], H0],  # Partition dim
            [unsharded_weight.shape[1], h1_tiles], # Batch dim
            [1, I],                                # Free dim - contiguous (stride=1)
        ],
        offset=h_offset * dims.I + weight_shard_offset,
    ),
)
# Here I (intermediate dimension) is the large contiguous free dimension
```

**Why this matters:** Strided DMA (free dim not contiguous) has significant overhead.
If your tensor layout requires strided access, consider asking the user to change the layout
(see Tensor Layout Flexibility above).

### 3. Keep Intermediates in SBUF

Avoid unnecessary HBM round-trips by keeping intermediate results in SBUF between operations.

**Common pattern: MatMul → Element-wise → HBM**
```python
# MatMul result in PSUM
psum_result = nl.ndarray((P, F), dtype=nl.float32, buffer=nl.psum)
nisa.nc_matmul(dst=psum_result, stationary=a, moving=b)

# Copy to SBUF for element-wise ops (PSUM → SBUF, no HBM)
sbuf_result = nl.ndarray((P, F), dtype=nl.float32, buffer=nl.sbuf)
nisa.tensor_copy(dst=sbuf_result, src=psum_result)

# Element-wise activation in SBUF (still no HBM)
nisa.activation(dst=sbuf_result, data=sbuf_result, op=nl.gelu)

# Only final result written to HBM
nisa.dma_copy(dst=output_hbm, src=sbuf_result)
```

**Anti-pattern to avoid:**
```python
# BAD: Writing matmul result to HBM, then reading back for activation
nisa.dma_copy(dst=hbm_temp, src=psum_result)      # Unnecessary write
nisa.dma_copy(dst=sbuf_for_act, src=hbm_temp)     # Unnecessary read
nisa.activation(dst=sbuf_for_act, data=sbuf_for_act, op=nl.gelu)
```

### 4. Maximize Hardware Parallelism (P=128)

Always try to use the full partition dimension (128) for hardware parallelism.

**Production example** from `mlp_tkg_constants.py:156`:
```python
# Hardware partition dimension constraint - always use 128
_pmax = nl.tile_size.pmax  # Max partition dimension in SBUF = 128

# Derived dimensions maximize partition usage
H0 = _pmax  # 128 (partition dimension)
H1 = H // H0  # Remaining hidden dimension

# All SBUF tiles use full partition dimension
tile = nl.ndarray((H0, free_dim), dtype=dtype, buffer=nl.sbuf)  # [128, ...]
```

### 5. Minimum Tile Sizes

| Operation | Minimum Tile Size | Rationale |
|-----------|------------------|-----------|
| MatMul (nc_matmul) | (128, 512) | Partition=128, PSUM free=512 for pipelining |
| Vector/Scalar ops | (128, 64) | Partition=128, free dim ≥64 for efficiency |

**Production MatMul example** from `mlp_tkg_gate_up_projection.py:188-204`:
```python
# Standard matmul tile: stationary [128, T], moving [128, 512]
for i_tiles in TiledRange(I, dims._psum_fmax):  # _psum_fmax = 512
    nisa.nc_matmul(
        dst=result_psums[i_tiles.index][
            nl.ds(dims.column_tiling_dim * column_idx, T),
            0 : i_tiles.size,  # Up to 512
        ],
        stationary=hidden.ap(
            pattern=[[T * H1, H0], [H1, T]],
            offset=h_offset + column_tile_offset,
        ),
        moving=weight_tiles[weight_idx][
            0:H0,  # 128 partition dim
            column_tile_offset,
            nl.ds(i_tiles.start_offset, i_tiles.size),  # Up to 512
        ],
    )
```

**Vector/Scalar tile sizing** from `mlp_tkg_constants.py:194-206`:
```python
# column_tiling_dim sets the free dimension for vector/scalar ops
# (e.g., activation functions, element-wise ops after matmul)
# Minimum 64 ensures efficient hardware utilization
if T <= 32:
    column_tiling_dim = 32  # Small T: use 32
elif T <= 64:
    column_tiling_dim = 64  # Medium T: use 64 (minimum for efficiency)
else:
    column_tiling_dim = 128  # Large T: use 128
```


## Related Skills

| Skill | Use When |
|-------|----------|
| `/neuron-nki-docs` | Look up specific API documentation |
| `/neuron-nki-debugging` | Debug compiler errors on device |
| `/neuron-nki-profiling` | Profile kernel performance |
| `/neuron-nki-profile-querying` | Query and analyze kernel profile data |
