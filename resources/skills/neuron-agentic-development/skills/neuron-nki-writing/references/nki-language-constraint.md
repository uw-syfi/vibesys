# NKI Language Constraints — NKI 0.4.0 API (MANDATORY)

All NKI code MUST use NKI 0.4.0 API exclusively. Beta 1, deprecated Beta 2, and removed NKI 0.3.0 patterns will NOT compile on current Neuron SDK (2.30.0+).

## Reference Kernel

```python
import nki
import nki.isa as nisa
import nki.language as nl

@nki.jit
def example_kernel(input_tensor):
    output = nl.ndarray(input_tensor.shape, dtype=input_tensor.dtype, buffer=nl.shared_hbm)
    P, F = input_tensor.shape
    TILE_P = 128
    for p_start in nl.affine_range((P + TILE_P - 1) // TILE_P):
        p_end = min(p_start * TILE_P + TILE_P, P)
        p_sz = p_end - p_start * TILE_P
        tile = nl.ndarray((p_sz, F), dtype=nl.float32, buffer=nl.sbuf)
        nisa.dma_copy(dst=tile, src=input_tensor[p_start * TILE_P:p_end, 0:F])
        result = nl.ndarray(tile.shape, dtype=nl.float32, buffer=nl.sbuf)
        nisa.activation(dst=result, data=tile, op=nl.exp)
        row_max = nl.ndarray((p_sz, 1), dtype=nl.float32, buffer=nl.sbuf)
        nisa.tensor_reduce(dst=row_max, data=result, op=nl.maximum, axis=(1,))
        inv = nl.ndarray(row_max.shape, dtype=nl.float32, buffer=nl.sbuf)
        nisa.reciprocal(dst=inv, data=row_max)
        nisa.dma_copy(dst=output[p_start * TILE_P:p_end, 0:F], src=result)
    return output
```

## Hard Rules — Beta 1 → Beta 2 (Violating ANY is a compilation failure)

| NEVER use (Beta 1) | ALWAYS use (Beta 2+) |
|---|---|
| `import neuronxcc.nki` | `import nki` |
| `nl.load(tensor[...])` | `nisa.dma_copy(dst=sbuf_tile, src=tensor[0:128, 0:512])` |
| `nl.store(tensor[...], value=x)` | `nisa.dma_copy(dst=tensor[0:128, 0:512], src=sbuf_tile)` |
| `result = nisa.func(...)` | `nisa.func(dst=result, ...)` |
| `nl.mgrid[...]` or `nl.arange(...)` | `tensor[0:128, 0:512]` or `nl.ds(offset, size)` |
| `mask=` on any ISA call | `min()` for boundary clamping |
| `nl.max` / `nl.min` | `nl.maximum` / `nl.minimum` |
| `nisa.activation(op=nl.reciprocal)` | `nisa.reciprocal(dst=..., data=...)` |
| `nisa.activation(op=nl.rsqrt)` | `nisa.rsqrt(dst=..., data=...)` |
| `negate=`, `reverse0=`, `dtype=` in reduce | Remove these parameters |
| `np.float32`, `np.add` inside kernels | `nl.float32`, `nl.add` |
| `@nki.jit` on sub-functions | Remove decorator from helpers |

**Mutable tensor annotations:** Use `import neuronxcc.nki.typing as nt` ONLY for annotating mutable output tensors in function signatures (caller allocates, kernel writes).

## Hard Rules — Beta 2 → NKI 0.3.0 (Violating ANY is a compilation failure)

| NEVER use (Beta 2) | ALWAYS use (NKI 0.3.0) |
|---|---|
| `@nki.jit(platform_target=...)` | Set `NEURON_PLATFORM_TARGET_OVERRIDE` env var instead |
| `@nki.jit(mode=...)` | Remove `mode=`; compiler auto-detects framework from arguments |
| `nisa.dma_copy(dst=hbm, src=psum)` | Copy PSUM→SBUF with `nisa.tensor_copy` first, then `nisa.dma_copy` from SBUF |
| `nisa.dma_copy(..., dst_rmw_op=...)` | Use `nisa.dma_compute(dst, srcs=[...], reduce_op=...)` |
| `nisa.dma_copy(..., unique_indices=...)` | Move `unique_indices` to `nisa.dma_compute(...)` |
| `buffer='sbuf'`, `buffer='psum'`, `buffer='hbm'` | Use objects: `buffer=nl.sbuf`, `buffer=nl.psum`, `buffer=nl.hbm` |
| `dge_mode=2` (integer enum constants) | Use named enums: `dge_mode=nisa.dge_mode.hwdge` |
| `buffer=nl.hbm` for kernel output tensors | `buffer=nl.shared_hbm` for all output tensors |
| `nisa.register_move(dst, imm=42)` | `src = nisa.register_alloc(x=42)` then `nisa.register_move(dst, src=src)` |
| `nisa.sendrecv(..., use_gpsimd_dma=True)` | `nisa.sendrecv(..., dma_engine=nisa.dma_engine.gpsimd_dma)` |
| deprecated dynamic-source tensor copy API | `nisa.tensor_copy()` with `.ap()` and `scalar_offset` |
| deprecated dynamic-destination tensor copy API | `nisa.tensor_copy()` with `.ap()` and `scalar_offset` |
| `nisa.memset(dst=int_buf, value=2.0)` | `nisa.memset(dst=int_buf, value=2)` — value dtype must match dst dtype |
| `def kernel(X, *, flag=True):` | `def kernel(X, flag=True):` — no `*` keyword-only separator |
| identity operators in conditionals (`is` / `is not`) | equality operators `==` / `!=` — e.g. `if x != None:` instead of the identity form |
| list-typed default arguments for kernel collection params | tuple defaults — e.g. `stride=(1, 1)` — use tuples, not lists, for kernel arguments |
| `num_channels=N` in collectives | Use `channel_ids=[0, 1, ...]` list in `collective_permute_implicit` |
| `nisa.dma_copy(dst=f4, src=ui16, dge_mode=hwdge)` (mismatched types) | Use `.view()` to match types: `src=src.view(nl.float4_e2m1fn_x4)` |
| `nisa.tensor_reduce(..., axis=1)` on 3D/4D tensors (wrong axis) | Use correct axis for actual tensor dimension (Beta 2 axis handling was buggy) |
| `nisa.dma_compute(dst, srcs, scales, reduce_op)` (Beta 2 order) | `nisa.dma_compute(dst, srcs, reduce_op, scales=None, unique_indices=True)` |
| `nisa.affine_select(dst, pattern, offset, ch_mul, ...)` (positional offset) | `nisa.affine_select(dst, pattern, ch_mul, on_true, on_false, offset=offset)` |

## Hard Rules — NKI 0.3.0 → NKI 0.4.0 (Violating ANY is a compilation failure)

| NEVER use (NKI 0.3.0) | ALWAYS use (NKI 0.4.0) |
|---|---|
| `nisa.dma_transpose` with mismatched src/dst ranks | `dst.shape` must match transposed `src.shape` exactly including rank |
| `nisa.tensor_copy_dynamic_src(...)` | Removed. Use `nisa.tensor_copy()` with `.ap()` and `scalar_offset` |
| `nisa.tensor_copy_dynamic_dst(...)` | Removed. Use `nisa.tensor_copy()` with `.ap()` and `scalar_offset` |
| `import neuronxcc.nki` inside kernels | Now a **compilation error** (was warning). Use `import nki` |
| `nl.tile_size.total_available_sbuf_size` | Deprecated. Use `nl.tile_size.sbuf_fmax_bytes` (per-partition) or `nl.tile_size.sbuf_size_bytes` (total) |

### NKI 0.4.0 tile_size Bytes-Aware Constants

New properties on `nl.tile_size` for SBUF/PSUM capacity checks:

| Constant | Description |
|----------|-------------|
| `nl.tile_size.sbuf_size_bytes` | Total SBUF capacity across all 128 partitions, in bytes |
| `nl.tile_size.sbuf_fmax` | Per-partition usable SBUF free dimension in FP32 elements |
| `nl.tile_size.sbuf_fmax_bytes` | Per-partition usable SBUF free dimension in bytes |
| `nl.tile_size.psum_fmax_bytes` | PSUM bank size in bytes |

**CORRECT — use bytes-aware constants for capacity checks:**
```python
assert F * 4 <= nl.tile_size.sbuf_fmax_bytes  # Check per-partition SBUF capacity in bytes
assert F <= nl.tile_size.sbuf_fmax            # Check per-partition SBUF capacity in elements
```

### NKI 0.4.0 `dma_transpose` Rank Matching — Required Pattern

`nisa.dma_transpose` now enforces that `dst.shape` rank matches the transposed `src.shape` rank exactly.

**FORBIDDEN — do NOT generate this code:**
```python
# FORBIDDEN: 3D dst with 4D src — rank mismatch
src_4d = nl.ndarray((128, 1, 1, 4096), dtype=nl.float32, buffer=nl.sbuf)
dst_3d = nl.ndarray((4096, 1, 128), dtype=nl.float32, buffer=nl.sbuf)
nisa.dma_transpose(dst=dst_3d, src=src_4d, axes=(3, 1, 2, 0))  # FORBIDDEN: ranks differ
```

**CORRECT — match dst rank to src rank:**
```python
src_4d = nl.ndarray((128, 1, 1, 4096), dtype=nl.float32, buffer=nl.sbuf)
dst_4d = nl.ndarray((4096, 1, 1, 128), dtype=nl.float32, buffer=nl.sbuf)
nisa.dma_transpose(dst=dst_4d, src=src_4d, axes=(3, 1, 2, 0))
```

### PSUM to HBM — Required Pattern (NKI 0.3.0+)

PSUM cannot be directly DMA-copied to HBM. Always copy through SBUF first.

**FORBIDDEN — do NOT generate this code:**
```python
nisa.dma_copy(dst=hbm_tensor, src=psum_tensor[0:TILE, 0:N])  # FORBIDDEN: direct PSUM→HBM
```

**CORRECT — always use this pattern:**
```python
sbuf_temp = nl.ndarray((TILE, N), dtype=nl.float32, buffer=nl.sbuf)
nisa.tensor_copy(dst=sbuf_temp[0:TILE, 0:N], src=psum_tensor[0:TILE, 0:N])
nisa.dma_copy(dst=hbm_tensor, src=sbuf_temp[0:TILE, 0:N])
```

### Read-Modify-Write (Scatter-Add) — Required Pattern (NKI 0.3.0+)

For ANY scatter-add, accumulation, or read-modify-write on HBM tensors, use `nisa.dma_compute`
with `reduce_op`. Do NOT use manual load+add+store as a workaround.

**FORBIDDEN — do NOT generate this code:**
```python
nisa.dma_copy(dst=hbm_dst, src=sbuf_src, dst_rmw_op=nl.add)  # FORBIDDEN: dst_rmw_op removed
```

**CORRECT — simple scatter-add:**
```python
nisa.dma_compute(dst=hbm_dst, srcs=[sbuf_src], reduce_op=nl.add)
```

**CORRECT — accumulation loop with indirect indexing:**
```python
for k_idx in range(K):
    src_access = input_tensor.ap(...)
    if k_idx == 0:
        nisa.dma_copy(dst=reduced_sb[:, :], src=src_access)
    else:
        nisa.dma_compute(
            dst=reduced_sb[:, :],
            srcs=[src_access, reduced_sb[:, :]],
            reduce_op=nl.add,
            unique_indices=True,
        )
```

### Register Instructions — Required Pattern (NKI 0.3.0+)

Register instructions (`register_alloc`, `register_move`, `register_load`, `register_store`) are
used for **dynamic loop boundaries and while loop conditions** — they control engine sequencer
branching. They are NOT for adding constants to 2D tensors.

**FORBIDDEN — do NOT generate this code:**
```python
loop_reg = nisa.register_alloc()
nisa.register_move(loop_reg, imm=10)  # FORBIDDEN: imm= removed
```

**CORRECT — allocate register with initial value directly:**
```python
src_reg = nisa.register_alloc(x=10)
nisa.register_move(dst=loop_reg, src=src_reg)
```

**CORRECT — dynamic while loop pattern:**
```python
reg = nisa.register_alloc(5)
cond = nl.ndarray((1, 1), buffer=nl.sbuf, dtype=nl.int32)

while reg:
    # ... kernel body ...
    nisa.register_store(dst=cond, src=reg)
    nisa.tensor_scalar(dst=cond, data=cond, op0=nl.add, operand0=-1)
    nisa.register_load(dst=reg, src=cond)
```

### DGE Mode / Enum Constants — Required Pattern (NKI 0.3.0+)

Use named enums for all enum parameters. For hardware DGE mode, use the `dge_mode` parameter
with `nisa.dge_mode.hwdge`.

**FORBIDDEN — do NOT generate this code:**
```python
nisa.dma_copy(src=src_tensor, dst=dst_tensor, dge_mode=2)       # FORBIDDEN: integer enum
nisa.dma_copy(src=src_tensor, dst=dst_tensor, engine=nisa.dge)   # FORBIDDEN: wrong param name
```

**CORRECT — use dge_mode parameter with named enum:**
```python
nisa.dma_copy(dst=dst_tensor, src=src_tensor, dge_mode=nisa.dge_mode.hwdge)
```

Available `nisa.dge_mode` enum values: `nisa.dge_mode.none`, `nisa.dge_mode.swdge`, `nisa.dge_mode.hwdge`

### Dynamic Addressing — Required Pattern (NKI 0.3.0+)

Use `nisa.tensor_copy()` with `.ap()` (access pattern) and `scalar_offset` for dynamic addressing.
Do NOT use legacy dynamic-copy APIs or invent helper APIs — they do not exist.

**FORBIDDEN — do NOT generate this code:**
```python
nisa.tensor_copy_dynamic_src(dst=dst_tile, src=src_tile, offset=dyn_offset)  # FORBIDDEN: deprecated API
```

**CORRECT — use .ap() with scalar_offset:**
```python
nisa.tensor_copy(dst=dst_tile, src=src_tile.ap(scalar_offset=dyn_offset))
```
