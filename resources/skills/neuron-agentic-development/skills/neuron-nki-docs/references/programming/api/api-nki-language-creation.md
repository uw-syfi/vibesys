# nki.language — Tensor Creation

> **Module**: nki.language
> **Functions**: 10

## Overview

Tensor Creation instructions.

## Functions

### nki.language.device_print {#nki-language-device_print}

`nki.language.device_print(print_prefix, tensor)`

**Signature:**
```python
language.device_print(print_prefix, tensor)
```

Print a message with a string prefix followed by the value of a tile.

During kernel execution on hardware, the Neuron Runtime (NRT) exports device-printed tensors
via the NRT debug stream API. By default, setting the environment variable
`NEURON_RT_DEBUG_OUTPUT_DIR` to a directory path enables the default stream consumer,
which dumps tensor data to that directory. The output is organized as:
`<output_dir>/<print_prefix>/core_<logical_core_id>/<iteration>/`.

In CPU simulation, this prints immediately to stdout.

- **print_prefix** — prefix of the print message. Evaluated at trace time; must be a constant string.
- **tensor** — tensor to print out. Can be in SBUF or HBM.

---

### nki.language.gather_flattened {#nki-language-gather_flattened}

`nki.language.gather_flattened(data, indices, axis, dtype)`

**Signature:**
```python
language.gather_flattened(data, indices, axis=0, dtype=None)
```

Gather elements from data tensor using indices after flattening.

This instruction gathers elements from the data tensor using integer indices
provided in the indices tensor. For each element in the indices tensor, it
retrieves the corresponding value from the data tensor using the index value
to select from the free dimension of data.

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **data** — input tensor to gather from.
- **indices** — indices to gather.
- **axis** — axis along which to gather.
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
**Returns:** gathered tensor.

Examples:

```python
import nki.language as nl

# nki.language.gather_flattened -- gather elements by index
data = nl.load(data_tensor[0:128, 0:512])
indices = nl.load(indices_tensor[0:128, 0:512])
result = nl.gather_flattened(data, indices)
nl.store(actual_tensor[0:128, 0:512], result)
```

---

### nki.language.load {#nki-language-load}

`nki.language.load(src, dtype)`

**Signature:**
```python
language.load(src, dtype=None)
```

Load a tensor from device memory (HBM) into on-chip memory (SBUF).

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **src** — HBM tensor to load the data from.
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
**Returns:** a new tile on SBUF with values from `src`.

---

### nki.language.load_transpose2d {#nki-language-load_transpose2d}

`nki.language.load_transpose2d(src, dtype)`

**Signature:**
```python
language.load_transpose2d(src, dtype=None)
```

Load a tensor from device memory (HBM) and 2D-transpose the data before storing into on-chip memory (SBUF).

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **src** — HBM tensor to load the data from.
- **dtype** — (optional) data type to cast the output type to (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information); if not specified, it will default to be the same as the data type of the input tile.
**Returns:** a new tile on SBUF with values from `src` 2D-transposed.

---

### nki.language.ndarray {#nki-language-ndarray}

`nki.language.ndarray(shape, dtype, buffer, name, address)`

**Signature:**
```python
language.ndarray(shape, dtype, buffer=sbuf, name='', address=None)
```

Create a new tensor of given shape and dtype on the specified buffer.

- **shape** — the shape of the tensor.
- **dtype** — the data type of the tensor.
- **buffer** — the specific buffer (ie, sbuf, psum, hbm), defaults to sbuf.
- **name** — the name of the tensor, used in scheduling.
- **address** — optional memory address. For SBUF/PSUM this is a 2-tuple
    `(partition_offset, free_offset)`; for HBM it is a 1-tuple
    `(free_offset,)`.
**Returns:** a new `NkiTensor` allocated on the buffer.

---

### nki.language.ones {#nki-language-ones}

`nki.language.ones(shape, dtype, buffer, name)`

**Signature:**
```python
language.ones(shape, dtype, buffer=sbuf, name='')
```

Create a new tensor of given shape and dtype on the specified buffer, filled with ones.

((Similar to [numpy.ones](https://numpy.org/doc/stable/reference/generated/numpy.ones.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **shape** — the shape of the tensor.
- **dtype** — the data type of the tensor.
- **buffer** — the specific buffer (ie, sbuf, psum, hbm), defaults to sbuf.
- **name** — the name of the tensor, used in scheduling.
**Returns:** a new `NkiTensor` allocated on the buffer.

---

### nki.language.rand {#nki-language-rand}

`nki.language.rand(shape, dtype, buffer, name)`

**Signature:**
```python
language.rand(shape, dtype, buffer=sbuf, name='')
```

Create a new tensor of given shape and dtype on the specified buffer, filled with random values.

Values are sampled from a uniform distribution between 0 and 1.

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **shape** — the shape of the tensor.
- **dtype** — the data type of the tensor (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).
- **buffer** — the specific buffer (ie, sbuf, psum, hbm), defaults to sbuf.
- **name** — the name of the tensor, used in scheduling.
**Returns:** a new `NkiTensor` allocated on the buffer with random values.

Examples:

```python
import nki.language as nl

# nki.language.rand -- generate random values in [0, 1)
a = nl.rand((128, 512), dtype=nl.float32)
```

---

### nki.language.shared_constant {#nki-language-shared_constant}

`nki.language.shared_constant(constant)`

**Signature:**
```python
language.shared_constant(constant)
```

Create a tensor in shared HBM initialized with constant data.

The constant is embedded in the compiled binary and loaded to HBM
at model load time. With LNC=2, both cores share the same constant;
the data must not diverge across cores.

Supported element types: float32, float16, bfloat16, int32, int16,
int8, uint32, uint16, uint8, float8_e4m3fn, float8_e5m2,
float8_e8m0fnu.
Packed types (float8_e4m3fn_x4, float8_e5m2_x4, float4_e2m1fn_x4)
and tfloat32 are supported at the MLIR level but not yet tested
end-to-end on hardware.

- **constant** — the constant data. Can be a numpy array or a file path
    to a `.npy` file.
**Returns:** an NkiTensor in shared_hbm containing the constant data.

---

### nki.language.store {#nki-language-store}

`nki.language.store(dst, value)`

**Signature:**
```python
language.store(dst, value)
```

Store into a tensor on device memory (HBM) from on-chip memory (SBUF).

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **dst** — HBM tensor to store the data into.
- **value** — an SBUF tile that contains the values to store.

---

### nki.language.zeros {#nki-language-zeros}

`nki.language.zeros(shape, dtype, buffer, name)`

**Signature:**
```python
language.zeros(shape, dtype, buffer=sbuf, name='')
```

Create a new tensor of given shape and dtype on the specified buffer, filled with zeros.

((Similar to [numpy.zeros](https://numpy.org/doc/stable/reference/generated/numpy.zeros.html)))

> **Warning:**
>
> This API is experimental and may change in future releases.
>

- **shape** — the shape of the tensor.
- **dtype** — the data type of the tensor.
- **buffer** — the specific buffer (ie, sbuf, psum, hbm), defaults to sbuf.
- **name** — the name of the tensor, used in scheduling.
**Returns:** a new `NkiTensor` allocated on the buffer.

---

