# nki.tensor — Tensor Views (NkiTensor methods)

> **Module**: nki.tensor
> **Methods**: 17

## Overview

View methods on `NkiTensor`. Each returns a new `NkiTensor` view that shares the same underlying storage without copying data.

## Methods

### NkiTensor.ap {#nki-tensor-ap}

`NkiTensor.ap(pattern, offset, scalar_offset, vector_offset, indirect_dim, dtype)`

**Signature:**
```python
NkiTensor.ap(pattern, offset=None, scalar_offset=None, vector_offset=None, indirect_dim=0, dtype=None)
```

Low-level access pattern override (escape hatch).

Replaces shape and strides with an explicit
`[[stride, count], ...]` pattern addressing the underlying
storage directly. Analogous to `torch.as_strided`.

Each `stride` and `count` slot may be a Python `int` or dynamic
value only known at runtime — `nki.isa.VirtualRegister`. Note
that when any slot is dynamic, `scalar_offset` /
`vector_offset` / non-zero `indirect_dim` are not supported.

```python
sb = nl.ndarray((128, 32), dtype=nl.float32, buffer=nl.sbuf)
# Explicit 2D pattern: partition stride=32, free stride=1
sb.ap(pattern=[[32, 128], [1, 32]])

# With indirect access and dtype reinterpret:
sb.ap(pattern=[[64, 128], [1, 16]], dtype=nl.bfloat16,
      scalar_offset=idx, indirect_dim=1)

# Register-bound stride (dynamic access pattern):
stride0 = nisa.register_alloc(...)
count1 = nisa.register_alloc(...)
sb.ap(pattern=[[stride0, 128], [1, count1]])

```

- **pattern** — list of `[stride, count]` pairs defining the access
    pattern. Each entry may be `int` or `VirtualRegister`.
- **offset** — element offset added to the view's base storage offset.
    When `None` (default), inherits the current view's storage offset
    unchanged. Pass an explicit integer to compose with the base offset
    (e.g. offset=0 keeps the base, offset=N shifts by N additional elements).
- **scalar_offset** — dynamic scalar index for indirect access, as a
    1x1 index tensor or an `~nki.isa.NkiScalar`. Must be
    32-bit when `self` is in SBUF or PSUM; a 64-bit offset is
    only valid on an HBM operand, where it reaches past 4GB
- **vector_offset** — per-partition index tensor for indirect access
- **indirect_dim** — dimension in `self.shape` whose stride scales
    the indirect scalar/vector offset (default 0)
- **dtype** — reinterpret storage as this dtype (default: tensor's dtype)
**Returns:** new `NkiTensor` with the explicit access pattern

---

### NkiTensor.broadcast {#nki-tensor-broadcast}

`NkiTensor.broadcast(dim, size)`

**Signature:**
```python
NkiTensor.broadcast(dim, size)
```

Expand a size-1 dimension to `size` by repeating elements.

The dimension must have size 1 before broadcasting. No data is copied.

```python
t = nl.ndarray((128, 1, 64), dtype=nl.float32, buffer=nl.sbuf)
t.broadcast(1, 8)  # shape becomes (128, 8, 64)

```

**Constraints.**

- `t.shape[dim]` must be 1
- On-chip tensors: `dim` must not be 0 (partition dim)

- **dim** — dimension to broadcast
- **size** — new size for the dimension
**Returns:** new `NkiTensor` view with the broadcasted dimension

---

### NkiTensor.expand_dim {#nki-tensor-expand_dim}

`NkiTensor.expand_dim(dim)`

**Signature:**
```python
NkiTensor.expand_dim(dim)
```

Insert a new dimension of size 1 at position `dim`.

```python
t = nl.ndarray((128, 64), dtype=nl.float32, buffer=nl.sbuf)
t.expand_dim(1)  # shape becomes (128, 1, 64)

```

**Constraints.**

- On-chip tensors: `dim` must not be 0
- After `vector_select`: `dim` must not be 0 (cannot insert before
  the indirect partition dim; see `is_indirect`)

- **dim** — position at which to insert the new dimension
**Returns:** new `NkiTensor` view with an additional size-1 dimension

---

### NkiTensor.flatten_dims {#nki-tensor-flatten_dims}

`NkiTensor.flatten_dims(start_dim, end_dim)`

**Signature:**
```python
NkiTensor.flatten_dims(start_dim, end_dim)
```

Merge a contiguous range of dimensions into one.

Dimensions `start_dim` through `end_dim` (inclusive) are merged into
a single dimension. The dimensions must already be contiguous in memory
(no `permute` or non-contiguous slicing across them) so the merged view
is itself a valid view of storage.

```python
t = nl.ndarray((128, 2, 3, 4), dtype=nl.float32, buffer=nl.sbuf)
t.flatten_dims(1, 2)  # shape becomes (128, 6, 4)

```

**Constraints.**

- Dimensions `start_dim..end_dim` must be contiguous in memory
- On-chip tensors: `start_dim` must be > 0
- After `vector_select`: `start_dim` must be > 0

- **start_dim** — first dimension to merge (inclusive)
- **end_dim** — last dimension to merge (inclusive)
**Returns:** new `NkiTensor` view with the merged dimension

---

### NkiTensor.get_pattern {#nki-tensor-get_pattern}

`NkiTensor.get_pattern()`

**Signature:**
```python
NkiTensor.get_pattern()
```

Return the view's access pattern as `[[stride, count], ...]`.

Useful as a starting point when constructing a new `.ap()` that
keeps most of the current layout intact. The returned pattern
pairs each of the view's dimensions with its current stride, in
the same order as `shape` / `strides`.

---

### NkiTensor.indirect {#nki-tensor-indirect}

`NkiTensor.indirect(index, num_elem)`

**Signature:**
```python
NkiTensor.indirect(index, num_elem=None)
```

Create an indirect tensor view for Tensor Indirection (TI).

Available on NeuronCore-v4 (Trn3) and later.

TI allows reading or writing a column of data at given free-dimension
offsets across contiguous partition dimensions. Can be used as input
(gather) or output (scatter) in nisa operations.

Offsets are stored in a snake pattern across partition groups: offset i
comes from `index[i % G, i // G]` where G is the group size (16 for
vector/scalar/gpsimd engines, 32 for tensor engine).

- **index** — SBUF tensor containing free-dimension offsets, shape `(P, K)`
    where `P == self.shape[0]`.
- **num_elem** — number of offsets to use. Defaults to `index.size`.
**Returns:** new `NkiTensor` view with TI attached. Output shape is
    `(P, num_elem)`.

---

### NkiTensor.is_contiguous {#nki-tensor-is_contiguous}

`NkiTensor.is_contiguous()`

**Signature:**
```python
NkiTensor.is_contiguous()
```

Return True if the view covers storage contiguously (row-major order).

Computed from the current strides: each non-size-1 dimension's stride
must equal the product of the shape sizes of inner dimensions.

For on-chip tensors (SBUF, PSUM), the partition dim (dim 0) is skipped:
partitions are physically independent memory banks, so the partition
stride does not represent physical contiguity. Contiguity is evaluated
per-partition over the free dims only.

---

### NkiTensor.is_indirect {#nki-tensor-is_indirect}

`NkiTensor.is_indirect()`

**Signature:**
```python
NkiTensor.is_indirect()
```

Return True if this view already uses indirect addressing.

Indirect addressing is produced by dynamic `select`,
`vector_select`, or `ap` with `scalar_offset` /
`vector_offset`. Indirect views cannot be re-indirected, and the
dimension that participates in the indirection cannot be further
sliced or selected — use this query to guard against those chains.

---

### NkiTensor.permute {#nki-tensor-permute}

`NkiTensor.permute(dims)`

**Signature:**
```python
NkiTensor.permute(dims)
```

Reorder tensor dimensions.

Returns a new view with dimensions rearranged according to `dims`.
No data is copied.

```python
t = nl.ndarray((128, 4, 8), dtype=nl.float32, buffer=nl.sbuf)
t.permute((0, 2, 1))  # shape becomes (128, 8, 4)

```

**Constraints.**

- `dims` must be a permutation of `range(t.ndim)`
- On-chip tensors: `dims[0]` must be 0 (partition dim stays outermost)
- After `vector_select`: `dims[0]` must be 0 (indirect partition dim
  stays outermost; see `is_indirect`)

- **dims** — tuple of dimension indices in the desired order
**Returns:** new `NkiTensor` view with reordered dimensions

---

### NkiTensor.rearrange {#nki-tensor-rearrange}

`NkiTensor.rearrange(src_pattern, dst_pattern, fixed_sizes)`

**Signature:**
```python
NkiTensor.rearrange(src_pattern, dst_pattern, fixed_sizes=None)
```

Rearrange tensor dimensions using einops-style patterns.

Combines splitting, reordering, and merging dimensions into a single
named operation. Patterns are tuples of strings (dimension names) or
tuples of strings (grouped dimensions that are split or merged).

```python
t = nl.ndarray((128, 24), dtype=nl.float32, buffer=nl.sbuf)
# Split dim 1 into (h, w), then reorder to (b, w, h):
t.rearrange(('b', ('h', 'w')), ('b', 'w', 'h'), {'h': 4})
# Result shape: (128, 6, 4)

```

- **src_pattern** — source dimension pattern (tuple of str or tuple-of-str)
- **dst_pattern** — destination dimension pattern (same dimension names)
- **fixed_sizes** — dict mapping dimension names to known sizes (for -1 inference)
**Returns:** new `NkiTensor` view with rearranged dimensions

---

### NkiTensor.reshape {#nki-tensor-reshape}

`NkiTensor.reshape(shape)`

**Signature:**
```python
NkiTensor.reshape(shape)
```

Reshape the tensor to a new shape without copying data.

The total number of elements must remain the same. Fails if the current
memory layout is incompatible with the requested shape (e.g. after a
non-contiguous slice or permute).

```python
t = nl.ndarray((128, 4, 6), dtype=nl.float32, buffer=nl.sbuf)
t.reshape((128, 24))       # merge last two dims
t.reshape((128, 2, 12))    # split differently

```

**Constraints.**

- `prod(shape) == prod(t.shape)`
- On-chip tensors: `shape[0]` must equal `t.shape[0]` (partition dim preserved)
- After `vector_select`: `shape[0]` must equal `t.shape[0]` (indirect
  partition dim preserved)
- Fails if the current layout is incompatible with the requested shape

- **shape** — tuple of new dimension sizes
**Returns:** new `NkiTensor` view with the requested shape

---

### NkiTensor.reshape_dim {#nki-tensor-reshape_dim}

`NkiTensor.reshape_dim(dim, shape)`

**Signature:**
```python
NkiTensor.reshape_dim(dim, shape)
```

Split a single dimension into multiple dimensions.

The product of `shape` must equal `t.shape[dim]`. One element of
`shape` may be -1, in which case its value is inferred.

```python
t = nl.ndarray((128, 24), dtype=nl.float32, buffer=nl.sbuf)
t.reshape_dim(1, (4, 6))   # shape becomes (128, 4, 6)
t.reshape_dim(1, (4, -1))  # same result, 6 is inferred

```

**Constraints.**

- `prod(shape) == t.shape[dim]`
- On-chip tensors: `dim` must not be 0 (unless `shape` is trivial, e.g., `(128,)`)
- After `vector_select`: `dim` must not be 0

- **dim** — dimension to split
- **shape** — tuple of sizes for the new dimensions (may contain one -1)
**Returns:** new `NkiTensor` view with the dimension split

---

### NkiTensor.select {#nki-tensor-select}

`NkiTensor.select(dim, index)`

**Signature:**
```python
NkiTensor.select(dim, index)
```

Select a single element along a dimension, removing it.

The index is either a compile-time `int` (equivalent to
`t[:, index, :]` when `dim=1`) or a runtime value resolved on
device: an `NkiTensor` 1-element tile, a `VirtualRegister`, or an
`NkiScalar`.

```python
t = nl.ndarray((128, 8, 64), dtype=nl.float32, buffer=nl.sbuf)
t.select(1, 3)          # static: shape becomes (128, 64)

# Dynamic select (HBM tensor, index resolved at runtime):
idx = nl.ndarray((1, 1), dtype=nl.uint32, buffer=nl.sbuf)
hbm_t = nl.ndarray((4, 128, 8), dtype=nl.float32, buffer=nl.shared_hbm)
hbm_t.select(0, idx)    # shape becomes (128, 8)

# Same thing driven by a register:
reg = nisa.register_alloc()
nisa.register_load(reg, idx)
hbm_t.select(0, reg)

```

**Constraints.**

- Static: `0 <= index < t.shape[dim]`; runtime indices are not
  bounds-checked, and out-of-range is undefined behaviour
- One runtime index per view (no chaining) — see `is_indirect`
- Runtime on-chip: `dim` must not be 0 (partition dim)
- Static selection cannot target a dimension already indirected

- **dim** — dimension to select from
- **index** — compile-time `int`, or a runtime `NkiTensor` /
    `VirtualRegister` / `NkiScalar`
**Returns:** new `NkiTensor` view with the dimension removed

---

### NkiTensor.slice {#nki-tensor-slice}

`NkiTensor.slice(dim, start, end, step)`

**Signature:**
```python
NkiTensor.slice(dim, start, end, step=1)
```

Slice along a single dimension.

Returns a view selecting elements from `start` to `end` (exclusive)
with the given `step`. Equivalent to `t[:, start:end:step, :]` when
`dim=1`.

```python
t = nl.ndarray((128, 64), dtype=nl.float32, buffer=nl.sbuf)
t.slice(1, 8, 24, 1)   # shape becomes (128, 16)
t.slice(1, 0, 64, 2)   # shape becomes (128, 32)

```

**Constraints.**

- `0 <= start < end <= t.shape[dim]`
- `step >= 1`
- On an indirect view (see `is_indirect`), cannot slice a
  dimension that participates in the indirection.

- **dim** — dimension to slice
- **start** — start index (inclusive)
- **end** — end index (exclusive)
- **step** — step size (default 1)
**Returns:** new `NkiTensor` view with the sliced dimension

---

### NkiTensor.squeeze_dim {#nki-tensor-squeeze_dim}

`NkiTensor.squeeze_dim(dim)`

**Signature:**
```python
NkiTensor.squeeze_dim(dim)
```

Remove a dimension of size 1.

```python
t = nl.ndarray((128, 1, 64), dtype=nl.float32, buffer=nl.sbuf)
t.squeeze_dim(1)  # shape becomes (128, 64)

```

**Constraints.**

- `t.shape[dim]` must be 1
- On-chip tensors: `dim` must not be 0
- After `vector_select`: `dim` must not be 0

- **dim** — dimension to remove (must have size 1)
**Returns:** new `NkiTensor` view with the dimension removed

---

### NkiTensor.vector_select {#nki-tensor-vector_select}

`NkiTensor.vector_select(dim, vector_offset)`

**Signature:**
```python
NkiTensor.vector_select(dim, vector_offset)
```

Per-partition indirect addressing using a vector of offsets.

Each partition uses its own index from `vector_offset` as the base
address for the selected dimension. The output partition count is
`vector_offset.shape[0]`.

This is used for gather-style operations where different partitions
read from different locations in the source tensor.

```python
hbm_t = nl.ndarray((64, 128, 8), dtype=nl.float32, buffer=nl.shared_hbm)
offsets = nl.ndarray((128, 1), dtype=nl.int32, buffer=nl.sbuf)
# Each of 128 partitions reads from a different row of hbm_t
hbm_t.vector_select(0, offsets)  # shape becomes (128, 128, 8)

```

**Constraints.**

- `dim` must be 0
- Only supported on HBM tensors
- Only one dynamic select per tensor (cannot combine with a prior
  dynamic `select` or `vector_select`); check
  `is_indirect` to guard
- The result is an indirect view: the selected dimension cannot be
  further sliced or selected.

- **dim** — dimension to apply indirect addressing (must be 0)
- **vector_offset** — SBUF tensor with per-partition indices, shape `(num_partitions, 1)`
**Returns:** new `NkiTensor` view with dim 0 size set to `vector_offset.shape[0]`

---

### NkiTensor.view {#nki-tensor-view}

`NkiTensor.view(dtype)`

**Signature:**
```python
NkiTensor.view(dtype)
```

Reinterpret the tensor's data as a different dtype.

No data is copied. Equivalent to `numpy.ndarray.view(dtype)` or
C++ `reinterpret_cast`. The last dimension's size is scaled by the
ratio of dtype sizes: reinterpreting a float32 tensor as uint8
multiplies the last-dim count by 4; reinterpreting uint8 as float32
divides it by 4.

```python
t = nl.ndarray((128, 64), dtype=nl.float32, buffer=nl.sbuf)
t.view(nl.uint8)     # shape becomes (128, 256), 4x expansion
t.view(nl.int32)     # shape stays (128, 64), same-size reinterpret

u = nl.ndarray((128, 256), dtype=nl.uint8, buffer=nl.sbuf)
u.view(nl.float32)   # shape becomes (128, 64), 4x contraction

```

**Constraints.**

- The last dimension must be contiguous in memory
- For contraction (larger dtype): last-dim size must be divisible by the ratio
- Not supported after dynamic / vector select

- **dtype** — target NKI dtype to reinterpret as
**Returns:** new `NkiTensor` view with the adjusted dtype and shape

---

