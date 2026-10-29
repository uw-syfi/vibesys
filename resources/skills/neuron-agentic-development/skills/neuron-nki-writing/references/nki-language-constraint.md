# NKI Language Constraints — Current NKI API (MANDATORY)

All NKI code MUST target the current NKI API. Legacy pre-0.3.0 patterns (historically
"Beta 1" / "Beta 2") and any pattern removed in a later release will NOT compile on a
current Neuron SDK.

**How versions are annotated in this document.** Rules are labelled with the release that
introduced the backward-incompatible change, written as `0.x.0+` — meaning "enforced from
that release onward, including the current one". A rule marked `0.4.0+` is a compilation
failure on 0.4.0 and every release after it. Rules with no version annotation are baseline
requirements that hold in every numbered release. The latest release these constraints were
validated against is **0.7.0**; when a newer release lands, add rows rather than renaming
the document.

A few rules apply only under the legacy `custom` frontend and are marked as such — see
[Frontend selection](#frontend-selection-custom-vs-standard) below for what that means.

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
    for p_start in range((P + TILE_P - 1) // TILE_P):
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

## Baseline API Requirements (all numbered releases)

These conventions predate the numbered releases (they were introduced as "Beta 2") and are
mandatory in every current version. Violating ANY is a compilation failure.

| NEVER use (legacy) | ALWAYS use |
|---|---|
| `import neuronxcc.nki` | `import nki` |
| `nl.load(tensor[...])` | `nisa.dma_copy(dst=sbuf_tile, src=tensor[0:128, 0:512])` |
| `nl.store(tensor[...], value=x)` | `nisa.dma_copy(dst=tensor[0:128, 0:512], src=sbuf_tile)` |
| `result = nisa.func(...)` | `nisa.func(dst=result, ...)` |
| `nl.mgrid[...]` or `nl.arange(...)` | `tensor[0:128, 0:512]` or `nl.ds(offset, size)` |
| `nl.par_dim(...)` to mark the partition axis, which allowed block dimensions in front of it — e.g. `nl.ndarray((8, nl.par_dim(128), 512), buffer=nl.sbuf)` | The partition dimension is simply the first axis. **If you want a grid of fixed-size tiles addressed by tile coordinate, use the layout API (`0.7.0+`)** — `nl.alloc_tiles(tile_shape=(128, 512), element_shape=(P, F), buffer=nl.sbuf, dtype=...)` for new storage, `nl.tiles(tensor, tile_shape=(128, 512))` to view an existing tensor as a tile grid, then index `grid[i, j].data`; `nl.alloc_blocks` / `nl.blocks` for a coarser transfer grain. See `/neuron-nki-docs` (Tiled & Blocked Layouts). Only when you do **not** want a tile grid, fold the block axis into the free dimensions — `nl.ndarray((128, 8, 512), buffer=nl.sbuf)`, indexed `a[0:128, i, 0:512]` — or allocate inside the loop when the blocks need not be alive at once. Do not hand-roll offset/stride arithmetic over a flat `nl.ndarray` as a substitute for the layout API |
| `mask=` on any ISA call | `min()` for boundary clamping |
| `nl.max` / `nl.min` | `nl.maximum` / `nl.minimum` |
| `negate=`, `reverse0=`, `dtype=` in reduce | Remove these parameters |
| `np.float32`, `np.add` inside kernels | `nl.float32`, `nl.add` |
| `@nki.jit` on sub-functions | Remove decorator from helpers |

**Mutable tensor annotations:** Use `import neuronxcc.nki.typing as nt` ONLY for annotating mutable output tensors in function signatures (caller allocates, kernel writes).

## Version-Gated Breaking Changes (Violating ANY is a compilation failure)

| Breaking since | NEVER use | ALWAYS use |
|---|---|---|
| `0.3.0+` | `@nki.jit(platform_target=...)` | Set `NEURON_PLATFORM_TARGET_OVERRIDE` env var instead |
| `0.3.0+` | `@nki.jit(mode=...)` | Remove `mode=`; compiler auto-detects framework from arguments |
| `0.3.0+` | `nisa.dma_copy(dst=hbm, src=psum)` | Copy PSUM→SBUF with `nisa.tensor_copy` first, then `nisa.dma_copy` from SBUF |
| `0.3.0+` | `nisa.dma_copy(..., dst_rmw_op=...)` | Use `nisa.dma_compute(dst, srcs=[...], reduce_op=...)` |
| `0.3.0+` | `nisa.dma_copy(..., unique_indices=...)` | Move `unique_indices` to `nisa.dma_compute(...)` |
| `0.3.0+` | `buffer='sbuf'`, `buffer='psum'`, `buffer='hbm'` | Use objects: `buffer=nl.sbuf`, `buffer=nl.psum`, `buffer=nl.hbm` |
| `0.3.0+` | `dge_mode=2` (integer enum constants) | Use named enums: `dge_mode=nisa.dge_mode.hwdge` |
| `0.3.0+` | `buffer=nl.hbm` for kernel output tensors | `buffer=nl.shared_hbm` for all output tensors |
| `0.3.0+` | `nisa.register_move(dst, imm=42)` | `src = nisa.register_alloc(x=42)` then `nisa.register_move(dst, src=src)` |
| `0.3.0+` | `nisa.sendrecv(..., use_gpsimd_dma=True)` | `nisa.sendrecv(..., dma_engine=nisa.dma_engine.gpsimd_dma)` |
| `0.3.0+` | deprecated dynamic-source tensor copy API | `nisa.tensor_copy()` with `.ap()` and `scalar_offset` |
| `0.3.0+` | deprecated dynamic-destination tensor copy API | `nisa.tensor_copy()` with `.ap()` and `scalar_offset` |
| `0.3.0+` | `nisa.memset(dst=int_buf, value=2.0)` | `nisa.memset(dst=int_buf, value=2)` — value dtype must match dst dtype |
| `custom` frontend only | `def kernel(X, *, flag=True):` | `def kernel(X, flag=True):` — no `*` keyword-only separator. **Only under `NKI_COMPILER_FRONTEND=custom`**; the default `standard` frontend accepts keyword-only arguments |
| `custom` frontend only | identity operators in conditionals (`is` / `is not`) | equality operators `==` / `!=` — e.g. `if x != None:`. **Only under `NKI_COMPILER_FRONTEND=custom`.** On the default `standard` frontend write idiomatic Python: `if x is not None:` |
| `0.3.0+` | list-typed default arguments for kernel collection params | tuple defaults — e.g. `stride=(1, 1)` — use tuples, not lists, for kernel arguments |
| `0.3.0+` | `num_channels=N` in collectives | Use `channel_ids=[0, 1, ...]` list in `collective_permute_implicit` |
| `0.3.0+` | `nisa.dma_copy(dst=f4, src=ui16, dge_mode=hwdge)` (mismatched types) | Use `.view()` to match types: `src=src.view(nl.float4_e2m1fn_x4)` |
| `0.3.0+` | `nisa.tensor_reduce(..., axis=1)` on 3D/4D tensors (wrong axis) | Use correct axis for actual tensor dimension (pre-0.3.0 axis handling was buggy) |
| `0.3.0+` | `nisa.dma_compute(dst, srcs, scales, reduce_op)` (legacy order) | `nisa.dma_compute(dst, srcs, reduce_op, scales=None, unique_indices=True)` |
| `0.3.0+` | `nisa.affine_select(dst, pattern, offset, ch_mul, ...)` (positional offset) | `nisa.affine_select(dst, pattern, ch_mul, on_true, on_false, offset=offset)` |
| `0.4.0+` | `nisa.dma_transpose` with mismatched src/dst ranks | `dst.shape` must match transposed `src.shape` exactly including rank |
| `0.4.0+` | `nisa.tensor_copy_dynamic_src(...)` | Removed. Use `nisa.tensor_copy()` with `.ap()` and `scalar_offset` |
| `0.4.0+` | `nisa.tensor_copy_dynamic_dst(...)` | Removed. Use `nisa.tensor_copy()` with `.ap()` and `scalar_offset` |
| `0.4.0+` | `import neuronxcc.nki` inside kernels | Now a **compilation error** (was warning). Use `import nki` |
| `0.4.0+` | `nl.tile_size.total_available_sbuf_size` | Deprecated. Use `nl.tile_size.sbuf_fmax_bytes` (per-partition) or `nl.tile_size.sbuf_size_bytes` (total) |
| `0.6.0+` (deprecated, still functional) | `nl.affine_range(N)` / `nl.sequential_range(N)` / `nl.static_range(N)` | `range(N)` — all three are deprecated aliases for `range()` with identical behaviour; see loop ranges below |
| `0.7.0+` | `for i in nl.dynamic_range(reg):` | `nl.fori_loop(lower, upper, body_fun, step=1)` — `dynamic_range` still exists but cannot work on the default `standard` frontend; see structured dynamic loops below |
| `0.7.0+` | bare `while reg:` | `nl.while_loop(init, body_fun)` — same reason; see structured dynamic loops below |
| `0.7.0+` | an SPMD launch-grid dimension that differs from the `neuronx-cc` LNC degree | Make them match, or set the kernel LNC degree with `NEURON_LOGICAL_NC_CONFIG` — a mismatch is now the compile error `NCC_EVRF066`; see LNC degree below |

### tile_size Bytes-Aware Constants (`0.4.0+`)

Properties on `nl.tile_size` for SBUF/PSUM capacity checks:

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

### `dma_transpose` Rank Matching — Required Pattern (`0.4.0+`)

`nisa.dma_transpose` enforces that `dst.shape` rank matches the transposed `src.shape` rank exactly.

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

### PSUM to HBM — Required Pattern (`0.3.0+`)

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

### Read-Modify-Write (Scatter-Add) — Required Pattern (`0.3.0+`)

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

### Register Instructions — Required Pattern (`0.3.0+`)

Register instructions (`register_alloc`, `register_move`, `register_load`, `register_store`) are
used for **dynamic loop boundaries and while loop conditions** — they control engine sequencer
branching. They are NOT for adding constants to 2D tensors, and they are NOT the way to do
arithmetic on a scalar read off a tile — for that see
[Device scalars](#device-scalars-scalar_load-vs-register_load) below.

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

#### Device scalars: `scalar_load` vs `register_load` {#device-scalars-scalar_load-vs-register_load}

Both pull a single value out of a 1x1 tile, but they are different abstractions — pick by what you
need to do with the value, not by which name you happen to recall:

| Need | Use | Why |
|---|---|---|
| Arithmetic on the value; address an HBM tensor past 4 GB via `scalar_offset` | `nisa.scalar_load(src)` → `NkiScalar`, written back with `nisa.scalar_store(dst, value)` | Returns a value supporting `+ - * // %` and all six comparisons. Accepts `nl.int32` **or `nl.int64`**, untruncated — the only way to reach past the 4 GB limit of a 32-bit byte offset. **`standard` frontend only** |
| Bound a dynamic loop; drive a `while` condition | `nisa.register_alloc()` + `nisa.register_load(dst, src)` | A mutable register handle for engine sequencer branching. 32-bit only. Works on both frontends |

`scalar_load` returns the scalar; `register_load` fills a register you allocated first. Do not
substitute `register_load` when the task is to compute with the value — it has no arithmetic and
no 64-bit form. Invented spellings such as `load_scalar` or `to_scalar` do not exist.

```python
# CORRECT — read an int64 off a tile, compute with it, use it as a wide HBM offset, store it back
buf = nl.ndarray((1, 1), dtype=nl.int64, buffer=nl.sbuf)
nisa.dma_copy(dst=buf, src=offset_src)
idx = nisa.scalar_load(buf)          # NkiScalar, dtype int64
idx = idx + 1                        # arithmetic directly on the scalar
nisa.dma_copy(dst=tile, src=big_hbm.ap(scalar_offset=idx))
nisa.scalar_store(out_buf, idx)      # out_buf must match the scalar's width
```

Two asymmetries to respect: an `nl.int64` scalar **cannot** bound an `nl.fori_loop` (the
trip-count comparison has no 64-bit form), and a `scalar_offset` on an SBUF or PSUM operand must
be `nl.int32` — a 64-bit offset there is rejected, not silently narrowed.

#### Loop ranges: use `range()` — the `nl.*_range` aliases are deprecated and identical

`nl.affine_range`, `nl.sequential_range` and `nl.static_range` are **deprecated aliases for
`range()`**. Each one's docstring carries *"This API is deprecated and will be removed in future
releases. Prefer using `range()` directly instead"*, and on the default frontend as well as in the
CPU simulator all three are implemented as `return range(start, stop, step)`.

They convey **no** unrolling or scheduling information. In particular:

| Belief | Reality |
|---|---|
| `affine_range` = parallel / full unroll | every alias fully unrolls; no variant does otherwise |
| `sequential_range` = "no unroll", serializes a loop-carried dependency | it is `range()`; it serializes nothing |
| `static_range` = "partial unroll" | it is `range()` |

So do not pick one alias over another to express intent — it has no effect. Instead:

- **Loop-carried state** (running max/sum, accumulators): keep it in SBUF/PSUM and mutate in place.
  Ordering follows from the data dependency, not from the loop function. For matmul K-accumulation
  this is exactly what `accumulate=(k_idx > 0)` does.
- **Constraining the scheduler:** use the `nl.no_reorder()` region.
- **A runtime trip count:** use `nl.fori_loop` / `nl.while_loop` (next section).

Existing kernels that use the aliases still compile; prefer `range()` in new code and do not
retrofit working kernels solely for this.

#### Frontend selection: `custom` vs `standard` {#frontend-selection-custom-vs-standard}

NKI has two frontends, selected by the `NKI_COMPILER_FRONTEND` environment variable:

| Value | What it is | Status |
|---|---|---|
| `standard` | ordinary Python — the kernel body is traced by executing it | **the default** since `0.7.0` |
| `custom` | NKI's own partial evaluator (historically called "the parser") | legacy escape hatch, deprecated |

Assume `standard` unless the user says otherwise. `custom` exists as a deprecation-window
escape hatch for kernels not yet migrated; a few rules in the table above are marked as
applying only to it. (The older boolean `NKI_EXPERIMENTAL_TRACER_FE` is deprecated and only
consulted when `NKI_COMPILER_FRONTEND` is unset.)

#### Structured dynamic loops (`0.7.0+`) — required on the default frontend

Under `standard` the kernel body is real Python, so a **runtime register has no value at
trace time**. `for i in nl.dynamic_range(...)` and bare `while reg:` therefore cannot drive
control flow there. `nl.dynamic_range` is still exported from `nki.language` — it is not
deleted — but it only functions under `NKI_COMPILER_FRONTEND=custom`. Treat both forms as
unusable and write the structured constructs `nl.fori_loop` / `nl.while_loop` instead; those
compile on **both** frontends, so migrated code works either way.

**DEPRECATED — bare `while reg:` (works only under `NKI_COMPILER_FRONTEND=custom`):**
```python
reg = nisa.register_alloc(5)
cond = nl.ndarray((1, 1), buffer=nl.sbuf, dtype=nl.int32)

while reg:  # DEPRECATED: a register has no value at trace time on the standard frontend
    # ... kernel body ...
    nisa.register_store(dst=cond, src=reg)
    nisa.tensor_scalar(dst=cond, data=cond, op0=nl.add, operand0=-1)
    nisa.register_load(dst=reg, src=cond)
```

**CORRECT — data-dependent loop with `nl.while_loop(init, body_fun)`:**
The body returns the next condition register. It is a true while (skips if `init` is zero).
```python
reg = nisa.register_alloc(5)
cond = nl.ndarray((1, 1), buffer=nl.sbuf, dtype=nl.int32)

def body(r):
    # ... kernel body ...
    nisa.register_store(dst=cond, src=r)
    nisa.tensor_scalar(dst=cond, data=cond, op0=nl.add, operand0=-1)
    nisa.register_load(dst=r, src=cond)
    return r  # next condition register

nl.while_loop(reg, body)
```

**CORRECT — counted loop with a runtime bound via `nl.fori_loop(lower, upper, body_fun, step=1)`:**
Replaces `for i in nl.dynamic_range(reg): BODY`. The body receives the iteration value as a
`VirtualRegister`; read it as a runtime offset via `.ap(scalar_offset=...)` or `nl.ds(offset, size)`.
```python
def body(i):
    nisa.dma_copy(dst=temp, src=data.ap(scalar_offset=i, indirect_dim=1))

nl.fori_loop(0, reg, body)  # replaces: for i in nl.dynamic_range(reg): ...
```

**Rules for structured dynamic loops:**
- **No loop-carried dependencies.** `fori_loop` follows Pallas semantics; keep cross-iteration
  state (running max/sum, accumulators, counters) in SBUF/HBM and mutate in place.
- **The loop variable is a frozen register.** No writes to it and no raw Python arithmetic on it;
  read it as a runtime offset via `.ap(scalar_offset=...)` or `nl.ds(offset, size)`.
- **Dynamic loops may not sit inside `nl.no_reorder()` blocks.** Compile-time loops (`range()`, or
  the deprecated `nl.*_range` aliases) are allowed there; `fori_loop` / `while_loop` are not.
- **Allocate output tensors before the loop.** The body is a separate MLIR region; a tensor made
  inside cannot be used outside. Allocate outputs before the loop (PSUM inside), write inside.
- **Use unique op names inside the body** (the body is traced once).

For the `fori_loop` / `while_loop` API signatures see `/neuron-nki-docs`
(api-nki-language-dims.md).

### DGE Mode / Enum Constants — Required Pattern (`0.3.0+`)

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

### MatMul PSUM Accumulation — Required Pattern (`0.4.0+`)

For matmul accumulation over the K (contraction) dimension, control PSUM initialization with the
`accumulate` flag on `nisa.nc_matmul`. Pass `accumulate=(k_idx > 0)` in the K-tile loop: the first
tile overwrites (initializes) PSUM, subsequent tiles accumulate. Do NOT `nisa.memset` the PSUM tile
before the loop — the `accumulate=False` first write initializes it.

**FORBIDDEN — do NOT generate this code:**
```python
# FORBIDDEN: memset PSUM to zero, then accumulate every iteration
psum = nl.ndarray((M, N), dtype=nl.float32, buffer=nl.psum)
nisa.memset(dst=psum, value=0.0)                  # FORBIDDEN: unnecessary PSUM zero-init
for k_idx in range(num_k_tiles):
    nisa.nc_matmul(dst=psum, stationary=a_tile, moving=b_tile, accumulate=True)
```

**CORRECT — use accumulate=(k_idx > 0), no memset:**
```python
psum = nl.ndarray((M, N), dtype=nl.float32, buffer=nl.psum)  # no memset needed
for k_idx in range(num_k_tiles):
    nisa.nc_matmul(dst=psum, stationary=a_tile, moving=b_tile, accumulate=(k_idx > 0))
```

### Dynamic Addressing — Required Pattern (`0.3.0+`)

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

### LNC degree must match — Required Pattern (`0.7.0+`)

The kernel's LNC degree and the `neuronx-cc` LNC degree **must match**; a mismatch is a
compile-time error, `NCC_EVRF066`. There are two ways to set the kernel's degree:

- `NEURON_LOGICAL_NC_CONFIG` — the **preferred** way to set the kernel LNC degree;
- the launch-grid bracket syntax (`my_kernel[2](...)`) — still supported, and it **takes
  precedence** when both are specified.

An SPMD launch grid is no longer required to launch an LNC2 kernel. With or without one, the
kernel is specialized across **all physical NeuronCores (PNCs)** in the LNC. When a launch
grid IS given, its dimension must equal the LNC degree — never pass a mismatched dimension.

For LNC concepts and the launch model, consult `/neuron-nki-docs` (LNC Overview).

---

## Deprecation Notices (forward-looking — not yet enforced)

These are NOT compile failures in the current release. They are documented so kernels are
written in a forward-compatible way; do not retrofit working code solely for these unless
asked. When one of these becomes enforced, move it into the version-gated table above with
the `0.x.0+` release that enforces it.

### Default LNC degree will become 2 on trn2+ (a future release)

A future release flips the default LNC degree to **2** on trn2 and later, including the CPU
simulator. NKI warns today when the degree is left unspecified. To keep the current
single-core behaviour across that change, set `NEURON_LOGICAL_NC_CONFIG=1` explicitly rather
than relying on the default.

### The `custom` frontend will be removed

`NKI_COMPILER_FRONTEND=custom` is a deprecation-window escape hatch. Write kernels that
compile on `standard` (the default) — notably the structured dynamic loops above — so no
migration is needed when `custom` goes away.
