# nki.language — Dimensions & Indexing

> **Module**: nki.language
> **Functions**: 11

## Overview

Dimensions & Indexing instructions.

## Functions

### nki.language.affine_range {#nki-language-affine_range}

`nki.language.affine_range(start, stop, step)`

**Signature:**
```python
language.affine_range(start, stop=None, step=1)
```

Create a sequence for fully unrolled loop iteration.

Create a sequence of numbers for use as loop iterators in NKI, resulting in
a fully unrolled loop. This function is an alias for Python's `range()`
function. Prefer using `range()` directly instead.

> **Warning:**
>
> This API is deprecated and will be removed in future releases.
>

- **start** — start value (or stop if `stop` is None).
- **stop** — stop value (exclusive).
- **step** — step size.
**Returns:** an iterator yielding integer values from start to stop.

Examples:

```python
import nki.language as nl

# nki.language.affine_range
for i in nl.affine_range(input_tensor.shape[1] // 512):
    offset = i * 512
    tile = nl.load(input_tensor[0:128, offset:offset+512])
    result = nl.multiply(tile, tile)
    nl.store(out_tensor[0:128, offset:offset+512], result)
```

---

### nki.language.ds {#nki-language-ds}

`nki.language.ds(start, size)`

**Signature:**
```python
language.ds(start, size)
```

A `size`-element window starting at `start`.

Used inside an index expression: `t[nl.ds(start, size), :]` is equivalent
to `t[start:start + size, :]` for a compile-time `start`. `size` must
be a compile-time integer (the access pattern needs a static element count);
`start` may be an `int` or a runtime index (see
`NkiTensor.select`). A runtime `start` is only spelled this way,
because a slice needs both bounds while the kernel is built.

```python
t = nl.ndarray((128, 64), dtype=nl.float32, buffer=nl.sbuf)
t[:, nl.ds(8, 16)]        # static: columns 8..23

idx = nl.ndarray((1, 1), dtype=nl.uint32, buffer=nl.sbuf)
t[:, nl.ds(idx, 16)]      # runtime: 16 columns starting at idx

```

With a runtime `start` the window slides at execution time and the
resulting view is indirect (see `NkiTensor.is_indirect`).

- **start** — window start — compile-time `int` or a runtime
    `NkiTensor` / `VirtualRegister` / `NkiScalar`
- **size** — window length, must be a compile-time integer
**Returns:** a `DynamicSlice` to use inside an index expression

---

### nki.language.dynamic_range {#nki-language-dynamic_range}

`nki.language.dynamic_range(start, stop, step)`

**Signature:**
```python
language.dynamic_range(start, stop=None, step=1)
```

Create a sequence for **dynamic** loop iteration.

Create a sequence of numbers for use as **dynamic** loop iterators in NKI.
The loop runs on device with dynamic bounds.

- **start** — start value (or stop if `stop` is None), can be VirtualRegister.
- **stop** — stop value (exclusive), can be VirtualRegister.
- **step** — step size, must be a compile-time positive integer (not VirtualRegister).
**Returns:** an iterator yielding integer values from start to stop.

Examples:

```python
import nki.language as nl

# nki.language.dynamic_range -- dynamic iteration with runtime bounds
for _ in nl.dynamic_range(1):
    tile = nl.load(input_tensor[0:128, 0:512])
    result = nl.multiply(tile, tile)
    nl.store(out_tensor[0:128, 0:512], result)
```

---

### nki.language.fori_loop {#nki-language-fori_loop}

`nki.language.fori_loop(lower, upper, body_fun, step)`

**Engine:** DMA Engine

**Signature:**
```python
language.fori_loop(lower, upper, body_fun, step=1)
```

Structured for loop with dynamic bounds.

Executes `body_fun(i)` for each iteration value from `lower` to
`upper` (exclusive) with the given `step`. The body is a callable that
receives the current iteration value.

The loop is roughly equivalent to the following Python code:

```python
for i in range(lower, upper, step):
    body_fun(i)

```

The body receives the current iteration value `i` as a VirtualRegister or NkiScalar,
passed by value. Read it inside the body (for example, as a `scalar_offset`
into a tensor). The loop controls iteration via `lower`/`upper`/`step`,
so writing to `i` has no effect on the loop; carry any other loop state
through SBUF/HBM.

- **lower** — start value (int, VirtualRegister, or NkiScalar).
- **upper** — end value (exclusive) (int, VirtualRegister, or NkiScalar).
- **body_fun** — function `(i) -> None` called each iteration with the
                current iteration value. `i` is a `NkiScalar` unless
                either `lower` or `upper` is a  `VirtualRegister`,
                then it will be a `VirtualRegister`
- **step** — step size, must be a compile-time positive integer.
**Returns:** None. Side effects in `body_fun` persist after the loop.

A bound must be 32-bit: the loop compiles into a compare-and-branch that has
no 64-bit form, so an `nl.int64` scalar cannot bound it. A 64-bit scalar can
still be computed inside the body and used as an offset there.

Examples:

```python
import nki.isa as nisa
import nki.language as nl

# nki.language.fori_loop -- counted loop with a runtime (dynamic) upper bound
ub_sb = nl.ndarray((1, 1), dtype=nl.int32, buffer=nl.sbuf)
nisa.dma_copy(dst=ub_sb, src=ub_input)
ub_reg = nisa.register_alloc()
nisa.register_load(ub_reg, ub_sb)

zeros = nl.zeros((1, N), dtype=nl.float32, buffer=nl.sbuf)
nisa.dma_copy(dst=output, src=zeros)
temp = nl.ndarray((1, 1), dtype=nl.float32, buffer=nl.sbuf)

def body(i):
    # `i` is the loop induction register; read it as a dynamic offset
    nisa.dma_copy(
        dst=temp,
        src=data.ap([[1, 1], [1, 1]], scalar_offset=i, indirect_dim=1),
    )
    nisa.dma_copy(
        dst=output.ap([[1, 1], [1, 1]], scalar_offset=i, indirect_dim=1),
        src=temp,
    )

nl.fori_loop(0, ub_reg, body)
```

---

### nki.language.num_programs {#nki-language-num_programs}

`nki.language.num_programs(axes)`

**Signature:**
```python
language.num_programs(axes=0)
```

Number of SPMD programs along the given axes in the launch grid.

- **axes** — the axes of the launch grid. If not provided, returns the total
    number of programs along the entire launch grid.
**Returns:** the number of SPMD programs along `axes` in the launch grid.

---

### nki.language.program_id {#nki-language-program_id}

`nki.language.program_id(axis)`

**Signature:**
```python
language.program_id(axis=0)
```

Index of the current SPMD program along the given axis in the launch grid.

- **axis** — the axis of the launch grid.
**Returns:** the program id along `axis`.

---

### nki.language.program_ndim {#nki-language-program_ndim}

`nki.language.program_ndim()`

**Signature:**
```python
language.program_ndim()
```

Number of dimensions in the SPMD launch grid.

**Returns:** the number of dimensions in the launch grid, i.e. the number of axes. 0 if no grid.

---

### nki.language.sequential_range {#nki-language-sequential_range}

`nki.language.sequential_range(start, stop, step)`

**Signature:**
```python
language.sequential_range(start, stop=None, step=1)
```

Create a sequence for fully unrolled loop iteration.

Create a sequence of numbers for use as loop iterators in NKI, resulting in
a fully unrolled loop. This function is an alias for Python's `range()`
function. Prefer using `range()` directly instead.

> **Warning:**
>
> This API is deprecated and will be removed in future releases.
>

- **start** — start value (or stop if `stop` is None).
- **stop** — stop value (exclusive).
- **step** — step size.
**Returns:** an iterator yielding integer values from start to stop.

Examples:

```python
import nki.language as nl

# nki.language.sequential_range
for i in nl.sequential_range(input_tensor.shape[1] // 512):
    offset = i * 512
    tile = nl.load(input_tensor[0:128, offset:offset+512])
    result = nl.multiply(tile, tile)
    nl.store(out_tensor[0:128, offset:offset+512], result)
```

---

### nki.language.static_range {#nki-language-static_range}

`nki.language.static_range(start, stop, step)`

**Signature:**
```python
language.static_range(start, stop=None, step=1)
```

Create a sequence for fully unrolled loop iteration.

Create a sequence of numbers for use as loop iterators in NKI, resulting in
a fully unrolled loop. This function is an alias for Python's `range()`
function. Prefer using `range()` directly instead.

> **Warning:**
>
> This API is deprecated and will be removed in future releases.
>

- **start** — start value (or stop if `stop` is None).
- **stop** — stop value (exclusive).
- **step** — step size.
**Returns:** an iterator yielding integer values from start to stop.

Examples:

```python
import nki.language as nl

# nki.language.static_range -- fully unrolled iteration over tiles
for i in nl.static_range(input_tensor.shape[1] // 512):
    offset = i * 512
    tile = nl.load(input_tensor[0:128, offset:offset+512])
    result = nl.multiply(tile, tile)
    nl.store(out_tensor[0:128, offset:offset+512], result)
```

---

### nki.language.tile_size {#nki-language-tile_size}

`nki.language.tile_size()`

**Signature:**
```python
language.tile_size
```

Hardware tile size constants (pmax, psum_bank_fmax, gemm_stationary_fmax, etc.).

**Attributes**

| Name | Description |
| --- | --- |
| `pmax` | Maximum partition dimension of a tile. |
| `psum_fmax` | Maximum free dimension per PSUM bank per partition, in FP32 elements. |
| `psum_bank_fmax` | Maximum free dimension per PSUM bank per partition, in FP32 elements. |
| `psum_bank_fmax_bytes` | Maximum free dimension per PSUM bank per partition, in bytes. |
| `gemm_stationary_fmax` | Maximum free dimension of the stationary operand of General Matrix Multiplication on Tensor Engine |
| `gemm_moving_fmax` | Maximum free dimension of the moving operand of General Matrix Multiplication on Tensor Engine |
| `bn_stats_fmax` | Maximum free dimension of BN_STATS |
| `psum_min_align` | Minimum byte alignment requirement for PSUM free dimension address |
| `sbuf_min_align` | Minimum byte alignment requirement for SBUF free dimension address |
| `psum_num_banks` | Number of usable PSUM banks per partition |
| `sbuf_size_bytes` | Total SBUF capacity in bytes (all partitions combined). |
| `sbuf_fmax` | Maximum free dimension of a tile on SBUF buffer, in FP32 elements. |
| `sbuf_fmax_bytes` | Maximum free dimension of a tile on SBUF buffer, in bytes. |
| `total_available_sbuf_size` | Usable SBUF free dimension per partition, in bytes. |

---

### nki.language.while_loop {#nki-language-while_loop}

`nki.language.while_loop(init, body_fun)`

**Engine:** DMA Engine

**Signature:**
```python
language.while_loop(init, body_fun)
```

Structured while loop with a register condition.

Loops while the condition register is nonzero, checking the condition before
each iteration (a true `while`, not `do-while`: if `init` is zero the
body never runs).

The loop is roughly equivalent to the following Python code:

```python
r = init
while r != 0:
    r = body_fun(r)

```

The body receives the current condition value `r` as a VirtualRegister,
passed by value. Read it inside the body (for example, materialize it with
`register_store` and use it as a `scalar_offset`). The loop's next
condition is the register the body **returns**; carry any other loop state
through SBUF/HBM.

- **init** — Initial condition register (VirtualRegister).
- **body_fun** — function `(r: VirtualRegister) -> VirtualRegister` called
                each iteration with the current condition value; returns the
                next condition register.
**Returns:** None. Side effects in `body_fun` persist after the loop.

Examples:

```python
import nki.isa as nisa
import nki.language as nl

# nki.language.while_loop -- flag-driven loop with a register condition
val = nl.ndarray((1, 1), dtype=nl.float32, buffer=nl.sbuf)
nisa.dma_copy(dst=val, src=data)
acc = nl.zeros((1, 1), dtype=nl.float32, buffer=nl.sbuf)

count_sb = nl.ndarray((1, 1), dtype=nl.int32, buffer=nl.sbuf)
nisa.dma_copy(dst=count_sb, src=count_input)
one_sb = nl.ndarray((1, 1), dtype=nl.int32, buffer=nl.sbuf)
nisa.memset(dst=one_sb, value=1)

reg = nisa.register_alloc()
nisa.register_load(reg, count_sb)

def body(r):
    nisa.tensor_tensor(dst=acc, data1=acc, data2=val, op=nl.add)
    nisa.tensor_tensor(dst=count_sb, data1=count_sb, data2=one_sb, op=nl.subtract)
    nisa.register_load(r, count_sb)
    return r

nl.while_loop(reg, body)
```

---

