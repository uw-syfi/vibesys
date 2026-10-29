# nki.language — Miscellaneous

> **Module**: nki.language
> **Functions**: 18

## Overview

Miscellaneous instructions.

## Functions

### nki.language.all {#nki-language-all}

`nki.language.all(x, axis, dtype)`

**Signature:**
```python
language.all(x, axis, dtype=None)
```

Whether all elements along the specified axis (or axes) evaluate to True.

((Similar to [numpy.all](https://numpy.org/doc/stable/reference/generated/numpy.all.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — a tile.
- **axis** — int or tuple/list of ints. The axis (or axes) along which to operate;
    must be free dimensions, not partition dimension (0); can only be the
    last contiguous dim(s) of the tile: [1], [1,2], [1,2,3], [1,2,3,4].
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
**Returns:** a tile with the logical AND reduction along the provided axis.

---

### nki.language.broadcast_to {#nki-language-broadcast_to}

`nki.language.broadcast_to(x, shape, dtype)`

**Signature:**
```python
language.broadcast_to(x, shape, dtype=None)
```

Broadcast a tile to a new shape following numpy broadcasting rules.

((Similar to [numpy.broadcast_to](https://numpy.org/doc/stable/reference/generated/numpy.broadcast_to.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

If `x.shape` is already the same as `shape`, returns `x` unchanged
(or a dtype-cast copy if `dtype` differs).

- **x** — the source tile in SBUF or PSUM.
- **shape** — the target shape. Must have the same rank as `x`.
    Each dimension must either match or be broadcast from size 1.
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
**Returns:** a tile with the target shape containing broadcast values from `x`.

---

### nki.language.dropout {#nki-language-dropout}

`nki.language.dropout(x, rate, dtype)`

**Signature:**
```python
language.dropout(x, rate, dtype=None)
```

Randomly zeroes some of the elements of the input tile given a probability rate.

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — a tile.
- **rate** — the probability of zeroing each element. Can be a scalar constant
    or a tile of shape `(x.shape[0], 1)` for per-partition drop probabilities.
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
**Returns:** a tile with randomly zeroed elements of `x`.

---

### nki.language.expand_dims {#nki-language-expand_dims}

`nki.language.expand_dims(x, axis)`

**Signature:**
```python
language.expand_dims(x, axis)
```

Expand the shape of a tile.

((Similar to [numpy.expand_dims](https://numpy.org/doc/stable/reference/generated/numpy.expand_dims.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

Insert a new axis that will appear at the axis position in the expanded tile shape.

- **x** — a tile.
- **axis** — position in the expanded axes where the new axis is placed.
**Returns:** a tile with view of input data with the number of dimensions increased.

---

### nki.language.matmul {#nki-language-matmul}

`nki.language.matmul(x, y, transpose_x)`

**Engine:** Tensor Engine

**Signature:**
```python
language.matmul(x, y, transpose_x=False)
```

x @ y matrix multiplication of x and y.

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — a tile on SBUF (partition dimension <= 128, free dimension <= 128),
    x's free dimension must match y's partition dimension.
- **y** — a tile on SBUF (partition dimension <= 128, free dimension <= 512).
- **transpose_x** — defaults to False. If True, x is treated as already transposed.
    If False, an additional transpose will be inserted to make x's partition
    dimension the contract dimension of the matmul to align with the Tensor Engine.
**Returns:** x @ y or x.T @ y if transpose_x=True.

Examples:

```python
import nki.isa as nisa
import nki.language as nl

# nki.language.matmul -- identity.T @ ones = ones
x = nl.shared_identity_matrix(n=128, dtype=nl.float32)
y = nl.full((128, 128), 1.0, dtype=nl.float32, buffer=nl.sbuf)
result_psum = nl.matmul(x, y, transpose_x=True)
result = nl.ndarray((128, 128), dtype=nl.float32, buffer=nl.sbuf)
nisa.tensor_copy(result, result_psum)
expected = nl.full((128, 128), 1.0, dtype=nl.float32,
                   buffer=nl.sbuf)
assert nl.equal(result, expected)
```

---

### nki.language.max {#nki-language-max}

`nki.language.max(x, axis, dtype, keepdims)`

**Signature:**
```python
language.max(x, axis, dtype=None, keepdims=False)
```

Maximum of elements along the specified axis (or axes) of the input.

((Similar to [numpy.max](https://numpy.org/doc/stable/reference/generated/numpy.max.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — a tile.
- **axis** — int or tuple/list of ints. The axis (or axes) along which to operate;
    must be free dimensions, not partition dimension (0); can only be the
    last contiguous dim(s) of the tile: [1], [1,2], [1,2,3], [1,2,3,4].
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
- **keepdims** — if True, the reduced axes are kept as size-one dimensions.
**Returns:** a tile with the maximum along the provided axis.

---

### nki.language.mean {#nki-language-mean}

`nki.language.mean(x, axis, dtype, keepdims)`

**Signature:**
```python
language.mean(x, axis, dtype=None, keepdims=False)
```

Arithmetic mean along the specified axis (or axes) of the input.

((Similar to [numpy.mean](https://numpy.org/doc/stable/reference/generated/numpy.mean.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — a tile.
- **axis** — int or tuple/list of ints. Free dimensions can only be the last
    contiguous dim(s) of the tile: [1], [1,2], [1,2,3], [1,2,3,4]. The partition
    dimension (0) is also supported on its own, reducing across partitions; it
    cannot be mixed with free dimensions in one call.
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
- **keepdims** — if True, the reduced axes are kept as size-one dimensions. It
    has no effect for `axis=0`: dimension 0 is structurally the partition
    dimension of a tile, so it is always retained as size one rather than dropped.
**Returns:** a tile with the average of elements along the provided axis. Float32
    intermediate values are used for the computation.

---

### nki.language.min {#nki-language-min}

`nki.language.min(x, axis, dtype, keepdims)`

**Signature:**
```python
language.min(x, axis, dtype=None, keepdims=False)
```

Minimum of elements along the specified axis (or axes) of the input.

((Similar to [numpy.min](https://numpy.org/doc/stable/reference/generated/numpy.min.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — a tile.
- **axis** — int or tuple/list of ints. The axis (or axes) along which to operate;
    must be free dimensions, not partition dimension (0); can only be the
    last contiguous dim(s) of the tile: [1], [1,2], [1,2,3], [1,2,3,4].
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
- **keepdims** — if True, the reduced axes are kept as size-one dimensions.
**Returns:** a tile with the minimum along the provided axis.

---

### nki.language.prod {#nki-language-prod}

`nki.language.prod(x, axis, dtype, keepdims)`

**Signature:**
```python
language.prod(x, axis, dtype=None, keepdims=False)
```

Product of elements along the specified axis (or axes) of the input.

((Similar to [numpy.prod](https://numpy.org/doc/stable/reference/generated/numpy.prod.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — a tile.
- **axis** — int or tuple/list of ints. The axis (or axes) along which to operate;
    must be free dimensions, not partition dimension (0); can only be the
    last contiguous dim(s) of the tile: [1], [1,2], [1,2,3], [1,2,3,4].
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
- **keepdims** — if True, the reduced axes are kept as size-one dimensions.
**Returns:** a tile with the product along the provided axis.

---

### nki.language.profiler_region_close {#nki-language-profiler_region_close}

`nki.language.profiler_region_close()`

**Signature:**
```python
language.profiler_region_close()
```

Mark the end of the most recently opened annotation region.

Must be paired with a preceding :py`profiler_region_open` call.
Failing to close a region will result in a compilation error.

---

### nki.language.profiler_region_open {#nki-language-profiler_region_open}

`nki.language.profiler_region_open(label, color)`

**Engine:** DMA Engine

**Signature:**
```python
language.profiler_region_open(label, color='')
```

Mark the start of a named annotation region.

Annotates all instructions between :py`profiler_region_open` and
:py`profiler_region_close` with the given label. These annotations
appear in Neuron Explorer as named regions on the execution timeline.

This API has no execution time cost — it only adds debug metadata during
compilation.

All instructions within a profiler region are associated with that region,
even if the compiler instruction scheduler reorders them to execute before
an instruction that appears in program order prior to
`profiler_region_open()`. The start of a region in the timeline is set
to the execution time of the first instruction associated with the region,
and therefore may cover instructions that were not associated with the
region.

Nested regions are supported: opening a region inside another region
associates instructions with both the inner and outer labels.

Opening a region without closing it is a compilation error.

- **label** — name of the annotation region. Must not contain `,`, `@`,
    `#`, or `|` characters.
- **color** — optional CSS color string (e.g., `"#4CAF50"`) for the region
    in Neuron Explorer.

Examples:

```python
import nki.language as nl

# nki.language.profiler_region_open -- annotate regions for Neuron Explorer
nl.profiler_region_open("load_phase", color="#4CAF50")
tile = nl.ndarray((128, 128), dtype=nl.float16, buffer=nl.sbuf)
nisa.dma_copy(tile, a[0:128, 0:128])
nl.profiler_region_close()

nl.profiler_region_open("compute_phase", color="#2196F3")
dst = nl.ndarray((128, 128), dtype=nl.float16, buffer=nl.sbuf)
nisa.tensor_tensor(dst, tile, tile, op=nl.add)
nl.profiler_region_close()
```

---

### nki.language.rms_norm {#nki-language-rms_norm}

`nki.language.rms_norm(x, w, axis, n, epsilon, dtype, compute_dtype)`

**Signature:**
```python
language.rms_norm(x, w, axis, n, epsilon=1e-06, dtype=None, compute_dtype=None)
```

Apply Root Mean Square Layer Normalization.

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — input tile.
- **w** — weight tile.
- **axis** — axis along which to compute the root mean square (rms) value.
- **n** — total number of values to calculate rms.
- **epsilon** — epsilon value used by rms calculation to avoid divide-by-zero.
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
- **compute_dtype** — (optional) dtype for the internal computation.
**Returns:** `x / RMS(x) * w`

Examples:

```python
import nki.language as nl

# nki.language.rms_norm -- normalize with unit weights
x = nl.full((128, 512), 2.0, dtype=nl.float32, buffer=nl.sbuf)
w = nl.full((128, 512), 1.0, dtype=nl.float32, buffer=nl.sbuf)
result = nl.rms_norm(x, w, axis=1, n=512)
```

---

### nki.language.softmax {#nki-language-softmax}

`nki.language.softmax(x, axis, dtype)`

**Signature:**
```python
language.softmax(x, axis=-1, dtype=None)
```

Softmax activation function on the input, element-wise.

((Similar to [torch.nn.functional.softmax](https://pytorch.org/docs/stable/generated/torch.nn.functional.softmax.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — a tile.
- **axis** — int or tuple/list of ints. The axis (or axes) along which to operate; must be free dimensions, not partition dimension (0); can only be the last contiguous dim(s) of the tile: `[1], [1,2], [1,2,3], [1,2,3,4]`
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
**Returns:** a tile that has softmax of `x`.

Examples:

```python
import nki.language as nl

# nki.language.softmax -- uniform input produces uniform output
a = nl.full((128, 512), 1.0, dtype=nl.float32, buffer=nl.sbuf)
result = nl.softmax(a, axis=1)
```

---

### nki.language.sum {#nki-language-sum}

`nki.language.sum(x, axis, dtype, keepdims)`

**Signature:**
```python
language.sum(x, axis, dtype=None, keepdims=False)
```

Sum of elements along the specified axis (or axes) of the input.

((Similar to [numpy.sum](https://numpy.org/doc/stable/reference/generated/numpy.sum.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — a tile.
- **axis** — int or tuple/list of ints. The axis (or axes) along which to operate;
    must be free dimensions, not partition dimension (0); can only be the
    last contiguous dim(s) of the tile: [1], [1,2], [1,2,3], [1,2,3,4].
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
- **keepdims** — if True, the reduced axes are kept as size-one dimensions.
**Returns:** a tile with the sum along the provided axis.

---

### nki.language.transpose {#nki-language-transpose}

`nki.language.transpose(x, dtype)`

**Signature:**
```python
language.transpose(x, dtype=None)
```

Transposes a 2D tile between its partition and free dimension.

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — 2D input tile.
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
**Returns:** a tile that has the values of the input tile with its partition and free
    dimensions swapped.

Examples:

```python
import nki.isa as nisa
import nki.language as nl

# nki.language.transpose -- transpose of identity is identity
x = nl.shared_identity_matrix(n=128, dtype=nl.float32)
result_psum = nl.transpose(x)
result = nl.ndarray((128, 128), dtype=nl.float32, buffer=nl.sbuf)
nisa.tensor_copy(result, result_psum)
assert nl.equal(result, x)
```

---

### nki.language.var {#nki-language-var}

`nki.language.var(x, axis, dtype, keepdims)`

**Signature:**
```python
language.var(x, axis, dtype=None, keepdims=False)
```

Variance along the specified axis (or axes) of the input.

((Similar to [numpy.var](https://numpy.org/doc/stable/reference/generated/numpy.var.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — a tile.
- **axis** — int or tuple/list of ints. The axis (or axes) along which to operate;
    must be free dimensions, not partition dimension (0); can only be the
    last contiguous dim(s) of the tile: [1], [1,2], [1,2,3], [1,2,3,4].
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
- **keepdims** — currently ignored; result always has keepdims=True shape.
**Returns:** a tile with the variance of the elements along the provided axis.

---

### nki.language.where {#nki-language-where}

`nki.language.where(condition, x, y, dtype)`

**Signature:**
```python
language.where(condition, x, y, dtype=None)
```

Return elements chosen from x or y depending on condition.

((Similar to [numpy.where](https://numpy.org/doc/stable/reference/generated/numpy.where.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **condition** — condition tile with float values (1.0 for True, 0.0 for False).
- **x** — tensor from which to take elements where condition is True.
- **y** — tensor from which to take elements where condition is False.
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
**Returns:** tensor with elements from x or y based on condition.

Examples:

```python
import nki.language as nl

# nki.language.where -- select 10.0 where condition is 1, else 0.0
cond = nl.full((128, 512), 1.0, dtype=nl.float32,
               buffer=nl.sbuf)
x = nl.full((128, 512), 10.0, dtype=nl.float32,
            buffer=nl.sbuf)
y = nl.full((128, 512), 0.0, dtype=nl.float32,
            buffer=nl.sbuf)
result = nl.where(cond, x, y)
expected = nl.full((128, 512), 10.0, dtype=nl.float32,
                   buffer=nl.sbuf)
assert nl.equal(result, expected)

# nki.language.where -- select 5.0 where condition is 0
cond = nl.full((128, 512), 0.0, dtype=nl.float32,
               buffer=nl.sbuf)
x = nl.full((128, 512), 10.0, dtype=nl.float32,
            buffer=nl.sbuf)
y = nl.full((128, 512), 5.0, dtype=nl.float32,
            buffer=nl.sbuf)
result = nl.where(cond, x, y)
expected = nl.full((128, 512), 5.0, dtype=nl.float32,
                   buffer=nl.sbuf)
assert nl.equal(result, expected)
```

---

### nki.language.zeros_like {#nki-language-zeros_like}

`nki.language.zeros_like(x, dtype, buffer, name)`

**Signature:**
```python
language.zeros_like(x, dtype=None, buffer=None, name='')
```

Create a new tensor of zeros with the same shape and type as a given tensor.

((Similar to [numpy.zeros_like](https://numpy.org/doc/stable/reference/generated/numpy.zeros_like.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **x** — the tensor.
- **dtype** — the data type of the tensor.
- **buffer** — the specific buffer (ie, sbuf, psum, hbm), defaults to sbuf.
- **name** — the name of the tensor, used in scheduling.
**Returns:** a new `NkiTensor` of zeros with the same shape as `x`.

---

