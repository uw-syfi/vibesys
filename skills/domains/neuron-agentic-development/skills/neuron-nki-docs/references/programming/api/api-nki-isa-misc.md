# nki.isa — Miscellaneous

> **Module**: nki.isa
> **Functions**: 27

## Overview

Miscellaneous instructions.

## Functions

### nki.isa.dge_mode {#nki-isa-dge_mode}

`nki.isa.dge_mode(value)`

**Signature:**
```python
isa.dge_mode(value)
```

Descriptor Generation Engine mode.

**Attributes**

| Name | Description |
| --- | --- |
| `unknown` | Unknown DGE mode, i.e., let compiler decide the DGE mode |
| `swdge` | Software DGE |
| `hwdge` | Hardware DGE |
| `none` | Not using DGE |

---

### nki.isa.dma_compute {#nki-isa-dma_compute}

`nki.isa.dma_compute(dst, srcs, reduce_op, scales, unique_indices, oob_mode, name)`

**Engine:** DMA Engine

**Signature:**
```python
isa.dma_compute(dst, srcs, reduce_op, scales=None, unique_indices=True, oob_mode=oob_mode.error, name=None)
```

Perform math operations using compute logic inside DMA engines with element-wise scaling and reduction.

This instruction leverages the compute capabilities within DMA engines to perform scaled element-wise operations
followed by reduction across multiple source tensors. The computation follows the pattern:
`dst = reduce_op(srcs[0] * scales[0], srcs[1] * scales[1], ...)`, where each source tensor is first
multiplied by its corresponding scale factor, then all scaled results are combined using the specified
reduction operation.
Currently, only `nl.add` is supported for `reduce_op`, and
all values in `scales` must be `1.0` (or `scales` can be `None`
which defaults to all 1.0).

The DMA engines perform all computations in float32 precision internally. Input tensors are automatically
cast from their source data types to float32 before computation, and the final float32 result is cast
to the output data type in a pipelined fashion.

**Read-Modify-Write with vector_offset (scatter and gather).**

When one of the source tensors has a `vector_offset` (indirect indexing),
`dma_compute` performs read-modify-write with two modes:

**Scatter RMW**: `dst(HBM)[indices] = dst(HBM)[indices] + src(SB)`
  - `dst` is in HBM with indirect indexing
  - One source matches `dst` and has `vector_offset`
  - The other source is data in SBUF

**Gather RMW**: `dst(SB) = dst(SB) + src(HBM)[indices]`
  - `dst` is in SBUF
  - One source is data in HBM with `vector_offset`
  - The other source matches `dst`

Both modes require:
  - Exactly 2 source tensors
  - All `scales` must be `1.0` (or `None`)
  - `unique_indices` must be `True` (non-unique indices not yet supported)

The only supported DGE mode for read-modify-write (scatter/gather) is SW DGE.
For `dma_compute` without `vector_offset`, the only supported DGE mode is None (static DMA).
The compiler automatically assigns the correct DGE mode.

**Memory types.**

Both input `srcs` tensors and output `dst` tensor can be in HBM or SBUF.
Both `srcs` and `dst` tensors must have compile-time known addresses (unless using vector_offset for indirect access).

**Data types.**

All input `srcs` tensors and the output `dst` tensor can be any supported NKI data types
(see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information). The DMA engines automatically cast input data types to float32
before performing the scaled reduction computation. The float32 computation results are then cast to the
data type of `dst` in a pipelined fashion.
See nki-type-casting for more information about type casting behavior.

**Layout.**

The computation is performed element-wise across all tensors, with the reduction operation applied
across the scaled source tensors at each element position.

**Tile size.**

The element count of each tensor in `srcs` and `dst` must match exactly.
The max number of source tensors in `srcs` is 16.

- **dst** — the output tensor to store the computed results
- **srcs** — a list of input tensors to be scaled and reduced
- **reduce_op** — the reduction operation to apply (currently only `nl.add` is supported)
- **scales** — (optional) a list of scale factors corresponding to each
               tensor in `srcs`. Must be all 1.0 if provided.
               Defaults to None (equivalent to [1.0, 1.0, ...]).
- **unique_indices** — (optional) Whether scatter indices are unique.
                      Must be True when using vector_offset (non-unique
                      not yet supported). Default: True.
- **oob_mode** — (optional) Specifies how to handle out-of-bounds (oob)
                 array indices during indirect access operations. Valid
                 modes are:

    - `oob_mode.error`: (Default) Raises an error when encountering
      out-of-bounds indices.
    - `oob_mode.skip`: Silently skips any operations involving
      out-of-bounds indices.

    For example, when using indirect gather/scatter operations with
    `vector_offset`, out-of-bounds indices can occur if the index
    array contains values that exceed the dimensions of the target array.

---

### nki.isa.dma_engine {#nki-isa-dma_engine}

`nki.isa.dma_engine(value)`

**Signature:**
```python
isa.dma_engine(value)
```

DMA transfer engine.

**Attributes**

| Name | Description |
| --- | --- |
| `dma` | Shared DMA with CoreBarrier synchronization (default). |
| `gpsimd_dma` | GPSIMD's internal DMA engine for low-latency SB-to-SB swaps in LNC=2. |

---

### nki.isa.dropout {#nki-isa-dropout}

`nki.isa.dropout(dst, data, prob, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.dropout(dst, data, prob, name=None)
```

Randomly replace some elements of the input tile `data` with zeros
based on input probabilities using Vector Engine.
The probability of replacing input elements with zeros (i.e., drop probability)
is specified using the `prob` field:
- If the probability is 1.0, all elements are replaced with zeros.
- If the probability is 0.0, all elements are kept with their original values.

The `prob` field can be a scalar constant or a tile of shape `(data.shape[0], 1)`,
where each partition contains one drop probability value.
The drop probability value in each partition is applicable to the input
`data` elements from the same partition only.

Data type of the input `data` tile can be any valid NKI data types
(see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).
However, data type of `prob` has restrictions based on the data type of `data`:

- If data type of `data` is any of the integer types (e.g., int32, int16),
  `prob` data type must be float32
- If data type of data is any of the float types (e.g., float32, bfloat16),
  `prob` data can be any valid float type

The output data type `dst.dtype` must match the input data type `data.dtype`.
The `data` and `dst` tiles can have at most three free dimensions (maximum
rank 4, including the partition dimension).

- **dst** — an output tile of the dropout result
- **data** — the input tile
- **prob** — a scalar or a tile of shape `(data.shape[0], 1)` to indicate the
             probability of replacing elements with zeros

---

### nki.isa.engine {#nki-isa-engine}

`nki.isa.engine(value)`

**Signature:**
```python
isa.engine(value)
```

Neuron Device engines.

**Attributes**

| Name | Description |
| --- | --- |
| `tensor` | Tensor Engine |
| `vector` | Vector Engine |
| `scalar` | Scalar Engine |
| `gpsimd` | GpSIMD Engine |
| `dma` | DMA Engine |
| `sync` | Sync Engine |
| `unknown` | Unknown Engine |

---

### nki.isa.exponential {#nki-isa-exponential}

`nki.isa.exponential(dst, src, max_value, reduce_res, reduce_cmd, reduce_init, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.exponential(dst, src, max_value=0.0, reduce_res=None, reduce_cmd=reduce_cmd.idle, reduce_init=0.0, name=None)
```

Apply exponential function to each element after subtracting a max_value using Vector Engine.

> **Note:**
> Available only on NeuronCore-v4 and newer.
>

This instruction computes `exp(src - max_value)` for each element. The instruction can
optionally maintain a running sum of the exponential values using shared internal reduction
registers in the Vector Engine.

The exponential operation is performed as:

```
dst[i] = exp(src[i] - max_value)

```

When accumulation is enabled through `reduce_cmd`, the instruction also computes:

```
reduce_res[i] = sum(dst[i])

```

The Vector Engine performs the computation in float32 precision internally and can
output results in various data types as specified by the `dst` dtype field.
See nki-type-casting for more information about type casting behavior.

**Constraints**

- Supported engines: Vector.
- `src`, `dst` must have the same number of elements in the partition dimension.
- `src`, `dst` must have the same number of elements in the free dimensions.
- `src` and `dst` can have at most three free dimensions (maximum rank 4,
  including the partition dimension).
- `reduce_init` should be unset or set to `0.0` when `reduce_cmd` is not `load_reduce`.

**Tensor indirection.**

`dst` and `src` support tensor indirection (gather/scatter) by passing a view
created with `.indirect(index)`. `max_value`, `reduce_res`, and
`reduce_init` do **not** support tensor indirection. Runs on the Vector engine.
(`exponential` itself is only available on NeuronCore-v4 and later.)

When operands are manually allocated, their base partitions must satisfy:

- the `index` of every `.indirect()` view starts on a quadrant boundary
  (a multiple of 32);
- if `src` uses `.indirect()`, `src` starts on the same partition as
  its `index`;
- if `dst` uses `.indirect()` and `src` is in SBUF, `dst`'s `index`
  starts on the same partition as `src`;
- if `dst` uses `.indirect()` and `src` is in PSUM and uses
  `.indirect()`, `dst`'s `index` starts on the same partition as
  `src`'s `index`.

- **dst** — The output tile with exponential function applied. Supported buffers: SBUF, PSUM. Supported dtypes: float8_e4m3, float8_e5m2, float16, bfloat16, float32, tfloat32, int8, int16, int32, uint8, uint16.
- **src** — The input tile to apply exponential function on. Supported buffers: SBUF, PSUM. Supported dtypes: float8_e4m3, float8_e5m2, float16, bfloat16, float32, int8, int16, int32, uint8, uint16, uint32.
- **max_value** — The maximum value to subtract from each element before applying exponential (for numerical stability). Can be a scalar or vector of shape `(src.shape[0], 1)`. Supported dtypes: float32.
- **reduce_res** — Optional tile to store reduction results (sum of exponentials). Must have shape `(src.shape[0], 1)`. Supported buffers: SBUF, PSUM. Supported dtypes: float8_e4m3, float8_e5m2, float16, bfloat16, float32, tfloat32.

    Pass `None` to keep the reduction result in the Vector Engine's internal
    accumulator without writing it out. This is useful when chaining multiple
    calls that reduce into the same accumulator — only the final call needs to
    pass a tile to retrieve the accumulated result.
- **reduce_cmd** — Control the state of reduction registers for accumulating exponential results. Supported: `idle`, `reset_reduce`, `reduce`, `load_reduce`.
- **reduce_init** — Initial value for reduction when using `reduce_cmd.load_reduce`. Supported dtypes: float32.

**Accumulator behavior:**

The Vector Engine maintains internal accumulator registers that can be controlled via the `reduce_cmd` parameter:

- `reduce_cmd.reset_reduce`: Reset accumulators to 0, then accumulate the current results.
- `reduce_cmd.reduce`: Continue accumulating without resetting (useful for multi-step reductions).
- `reduce_cmd.load_reduce`: Load the values from `reduce_init` into the accumulator, then accumulate the current result on top of it.
- `reduce_cmd.idle`: (default) No accumulation performed, accumulator state unknown.

> **Note:**
> Even when `reduce_cmd` is set to `idle`, the accumulator state may still be modified.
> Always use `reset_reduce` after any Vector Engine operation that ran with `idle` mode to ensure
> consistent behavior.
>
> **Note:**
> The accumulator registers are shared across Vector Engine accumulation instructions including
> `~nki.isa.exponential`,
> `~nki.isa.range_select`,
> `~nki.isa.scalar_tensor_tensor`,
> `~nki.isa.select_reduce`,
> `~nki.isa.tensor_scalar_cumulative`, and
> `~nki.isa.tensor_scalar_reduce`.
>

**Behavior**

```python
# Initialize reduction if requested
if reduce_cmd == reduce_cmd.reset_reduce:
    accumulator = 0
elif reduce_cmd == reduce_cmd.load_reduce:
    accumulator = reduce_init
elif reduce_cmd == reduce_cmd.idle:
    accumulator = undefined  # Not used

# Process each element
for i in range(num_elements):
    dst[i] = exp(src[i] - max_value)

    # Update reduction if active
    if reduce_cmd != reduce_cmd.idle:
        accumulator += dst[i]
```

---

### nki.isa.matmul_perf_mode {#nki-isa-matmul_perf_mode}

`nki.isa.matmul_perf_mode(value)`

**Signature:**
```python
isa.matmul_perf_mode(value)
```

Performance mode for matmul.

**Attributes**

| Name | Description |
| --- | --- |
| `none` | Default mode, no performance optimization |
| `double_row` | Double FP8 mode, 2x matmul throughput by packing two FP8 weight/ifmap element pairs |

---

### nki.isa.max8 {#nki-isa-max8}

`nki.isa.max8(dst, src, name)`

**Signature:**
```python
isa.max8(dst, src, name=None)
```

Find the 8 largest values in each partition of the source tile.

This instruction reads the input elements, converts them to fp32 internally, and outputs
the 8 largest values in descending order for each partition. Outputs are converted to
`dst.dtype` automatically.
See nki-type-casting for more information about type casting behavior.

The source tile can have at most four free dimensions (maximum rank 5, including
the partition dimension). The output tile can have at most two free dimensions
(maximum rank 3), although it always contains exactly 8 elements per partition.
The number of elements read per partition must be between 8 and 16,384 inclusive.
The output will always contain exactly 8 elements per partition.
The source and output must have the same partition dimension size:

- source: [par_dim, ...]
- output: [par_dim, 8]

- **dst** — a 2D tile containing the 8 largest values per partition in descending order with shape [par_dim, 8]
- **src** — the source tile to find maximum values from

---

### nki.isa.nonzero_with_count {#nki-isa-nonzero_with_count}

`nki.isa.nonzero_with_count(dst, src, index_offset, padding_val, name)`

**Engine:** GpSimd Engine

**Signature:**
```python
isa.nonzero_with_count(dst, src, index_offset=0, padding_val=-1, name=None)
```

Find indices of nonzero elements in an input tensor and their total count using GpSimd Engine.

> **Note:**
>
> Available only on NeuronCore-v3 and newer.
>

NOTE: this instruction only operates on partitions [0, 16, 32, ..., 112] of the input tile
and writes to partitions [0, 16, 32, ..., 112] of the destination tile. The data in other
partitions of the destination tile are not modified, including the last 'extra' slot for count.

This behavior is due to the physical connectivity of GpSimd engine. Each of the eight GpSimd cores
connects to 16 contiguous SBUF partitions (e.g., core[0] connects to partitions[0:16]).
In nonzero_with_count, each GpSimd core reads from and writes to its 0-th partition only.

This instruction takes an input array and produces an output array containing the indices of all
nonzero elements, followed by padding values, and ending with the count of nonzero elements found.

The output tensor has one more element in the free dimension than the input tensor:

- **First N elements**: 0-indexed positions of nonzero elements, offset by `index_offset`
- **Next T-N elements**: Filled with `padding_val`
- **Last element**: Count `N` of nonzero elements found

The `index_offset` parameter is useful when processing arrays in tiles, allowing
indices to be relative to the original array position rather than the tile.

Example for one partition of the tensor:

```
Input array (T=8): [0, 1, 1, 0, 0, 1, 0, 0]
index_offset = 16
padding_val = -1

Output (T+1=9): [17, 18, 21, -1, -1, -1, -1, -1, 3]

Where:

- 17, 18, 21 are the indices (1, 2, 5) plus offset 16
- -1 is the padding value for unused slots
- 3 is the count of nonzero elements

```

**Constraints**

- Supported arch versions: NeuronCore-v3+.
- Supported engines: GpSimd.
- Parameters `src`, `dst` must have the same number of elements in the partition dimension.
- Destination tensor must have exactly 1 more element than the source tensor in the free dimension.
- Only accesses the 0-th partition for each GpSimd core (i.e., [0, 16, 32, ..., 112]).
- `src` must be in SBUF with dtype float32 or int32.
- `dst` must be in SBUF with dtype int32.
- `index_offset` and `padding_val` must be int32.
- `src` and `dst` can have at most three free dimensions (maximum rank 4,
  including the partition dimension).

- **src** — Input tensor to find nonzero indices from. Only partitions [0, 16, 32, ..., 112] are read from. Supported buffers: SBUF. Supported dtypes: float32, int32.
- **dst** — Output tensor containing nonzero indices, padding, and count. Only partitions [0, 16, 32, ..., 112] are written to. It must have one extra element than src in the free dimension. Supported buffers: SBUF. Supported dtypes: int32.
- **index_offset** — Offset to add to the found indices (useful for tiled processing). Supported dtypes: int32.
- **padding_val** — Value to use for padding unused output elements. Supported dtypes: int32.

**Behavior**

```python
# Find all nonzero elements in input
nonzero_indices = []
for i in range(len(input_array)):
    if input_array[i] != 0:
        nonzero_indices.append(i + index_offset)

# Build output array
output = []
# Add found indices
for idx in nonzero_indices:
    output.append(idx)
# Add padding for remaining slots
for _ in range(len(input_array) - len(nonzero_indices)):
    output.append(padding_val)
# Add count as last element
output.append(len(nonzero_indices))

```

**Example**

```python
def nonzero_with_count_kernel(in_tensor):
    in_shape = in_tensor.shape
    assert len(in_tensor.shape) == 2, "expected 2D tensor"

    in_tile = nl.ndarray(in_shape, dtype=in_tensor.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=in_tile, src=in_tensor)

    out_tile = nl.ndarray((in_shape[0], in_shape[1] + 1), dtype=nl.int32, buffer=nl.sbuf)
    nisa.nonzero_with_count(dst=out_tile, src=in_tile, index_offset=0, padding_val=-1)

    out_tensor = nl.ndarray(out_tile.shape, dtype=out_tile.dtype, buffer=nl.hbm)
    nisa.dma_copy(dst=out_tensor, src=out_tile)

    return out_tensor
```

---

### nki.isa.oob_mode {#nki-isa-oob_mode}

`nki.isa.oob_mode(value)`

**Signature:**
```python
isa.oob_mode(value)
```

Out-of-bounds access mode.

**Attributes**

| Name | Description |
| --- | --- |
| `error` | Raise a runtime error when an out-of-bounds access is detected. |
| `skip` | Silently skip the runtime out-of-bounds access. |

---

### nki.isa.quantize_mx {#nki-isa-quantize_mx}

`nki.isa.quantize_mx(dst, src, dst_scale, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.quantize_mx(dst, src, dst_scale, name=None)
```

Quantize FP16/BF16 data to MXFP8 tensors (both data and scales) using Vector Engine.

> **Note:**
>
> Available only on NeuronCore-v4 and newer.
>

The resulting `dst` and `dst_scale` tensors use the MXFP8 element and scale data types as defined in the
[OCP Microscaling standard](https://www.opencompute.org/documents/ocp-microscaling-formats-mx-v1-0-spec-final-pdf).
This instruction calculates the required scales for each group of 32 values in `src`, divides them by the calculated scale,
and casts to the target MXFP8 element data type.

The scale calculation differs from the sample conversion algorithm described in the OCP
specification: this instruction uses a block scale that is two times larger, reserving
additional range for rounding the largest values in each group without saturation. This
remains OCP MX-compliant.

The output layout is suitable for direct consumption by the
`nisa.nc_matmul_mx` API running on Tensor Engine.

**Memory types.**

All input `src` and output tiles (`dst` and `dst_scale`) must be in SBUF.

**Data types.**

The input `src` tile must be float16 or bfloat16. The output `dst` tile must be float8_e5m2_x4 or
float8_e4m3fn_x4 (4-packed FP8 data types). The `dst_scale` tile must be float8_e8m0fnu or uint8
(prefer `float8_e8m0fnu`: OCP MX standard; uint8 accepted for backward compatibility).

The 4-packed data types (float8_e5m2_x4/float8_e4m3fn_x4) are 32-bit data types that pack four 8-bit
float8_e5m2/float8_e4m3fn values.

**Layout.**

The quantization operates on groups of 32 elements from the input `src` tile, where each group consists of
8 partitions × 4 elements per partition. For each 32-element group, the instruction produces:

- Quantized FP8 data in `dst`
- One shared scale value in `dst_scale` per group

**Tile size.**

- The partition dimension size of `src` must be a multiple of 32 and must not exceed 128.
- The free dimension size of `src` must be a multiple of 4 and must not exceed the physical size of each SBUF
  partition.
- The `dst` tile has the same partition dimension size as `src` but a free dimension size
  that is 1/4 of `src` free dimension size due to the special 4-packed FP8 data types.
- `src` can have at most three free dimensions (maximum rank 4). `dst` and
  `dst_scale` must each be contiguous in memory (multiple free dimensions are
  allowed if they collapse to a single contiguous run). These ranks include the
  partition dimension.

**Scale calculation.**

For a group :math:`V` of 32 values, let

.. math::

   a_{\max} = \max_{V_i \in V} |V_i|

Let :math:`E_{\max}` be the maximum unbiased exponent of the destination
element data type: 8 for `float8_e4m3fn` and 15 for `float8_e5m2`.

The block scale :math:`X` is calculated as

.. math::

   X =
   2^{
     \left\lfloor \log_2(a_{\max}) \right\rfloor
     - (E_{\max} - 1)
   }

For an all-zero group, where :math:`a_{\max} = 0`, the block scale is set to
:math:`X = 2^{-127}`, the minimum value representable by `float8_e8m0fnu`.

- **dst** — the quantized MXFP8 output tile
- **src** — the input FP16/BF16 tile to be quantized
- **dst_scale** — the MXFP8 output scale tile (float8_e8m0fnu or uint8)

---

### nki.isa.rand2 {#nki-isa-rand2}

`nki.isa.rand2(dst, min, max, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.rand2(dst, min, max, name=None)
```

Generate pseudo random numbers with uniform distribution using Vector Engine.

> **Note:**
>
> Available only on NeuronCore-v4 and newer.
>

This instruction generates pseudo random numbers and stores them into SBUF/PSUM.
The generated values follow a uniform distribution within the specified [min, max] range.

Key features:

- Uses XORWOW PRNG algorithm for high-quality random number generation
- Generates FP32 random values with uniform distribution
- Supports output conversion to various data types

**Memory types.**

The output `dst` tile can be in SBUF or PSUM.

**Data types.**

The output `dst` tile can be any of: float8_e4m3, float8_e5m2, float16, bfloat16, float32,
tfloat32, int8, int16, int32, uint8, uint16, or uint32.

**Tile size.**

The partition dimension size of `dst` must not exceed 128. The number of
elements per partition of `dst` must not exceed the physical size of each SBUF/PSUM partition.

**Constraints.**

- Supported arch versions: NeuronCore-v4+.
- Supported engines: Vector.
- min < max for valid range.
- `dst` can have at most three free dimensions (maximum rank 4, including
  the partition dimension).

- **dst** — the destination tensor to write random values to
- **min** — minimum value for uniform distribution range (FP32), can be a scalar or vector value
- **max** — maximum value for uniform distribution range (FP32), can be a scalar or vector value

---

### nki.isa.rand_get_state {#nki-isa-rand_get_state}

`nki.isa.rand_get_state(dst, engine, name)`

**Engine:** GpSimd Engine

**Signature:**
```python
isa.rand_get_state(dst, engine=engine.gpsimd, name=None)
```

Store the current pseudo random number generator (PRNG) states from the engine.

This instruction stores the current PRNG states cached inside the engine to SBUF/PSUM.
Each partition in the output tensor holds the PRNG states for the corresponding compute lane
inside the engine.

**Memory types.**

The output `dst` tile must be in SBUF (NeuronCore-v3) or SBUF/PSUM (NeuronCore-v4+).

**Data types.**

The output `dst` tile must be uint32.

**Tile size.**

- dst element count for XORWOW must be 6 elements (GpSimd) or 24 elements (Vector).

**Constraints.**

- Supported arch versions: NeuronCore-v3+.
- Supported engines: NeuronCore-v3: GpSimd. NeuronCore-v4+: GpSimd, Vector.
- Since GpSimd Engine cannot access PSUM, `dst` must be in SBUF when using GpSimd Engine.

- **dst** — the destination tensor to store PRNG state values; must be a 2D uint32 tensor
- **engine** — specify which engine to use: `nki.isa.engine.gpsimd` (default)
               or `nki.isa.engine.vector` (NeuronCore-v4+)

---

### nki.isa.rand_set_state {#nki-isa-rand_set_state}

`nki.isa.rand_set_state(src_seeds, engine, name)`

**Engine:** GpSimd Engine

**Signature:**
```python
isa.rand_set_state(src_seeds, engine=engine.gpsimd, name=None)
```

Seed the pseudo random number generator (PRNG) inside the engine.

This instruction initializes the PRNG state for future random number generation operations.
Each partition in the source tensor seeds the PRNG states for the corresponding compute lane
inside the engine.

The PRNG state is cached inside the engine as a persistent state during the rest of NEFF
execution. However, the state cannot survive TPB resets or Runtime reload.

**Memory types.**

The input `src_seeds` tile must be in SBUF.

**Data types.**

The input `src_seeds` tile must be uint32.

**Tile size.**

- src_seeds element count for XORWOW must be 6 elements (GpSimd) or 24 elements (Vector).

**Constraints.**

- Supported arch versions: NeuronCore-v3+.
- Supported engines: NeuronCore-v3: GpSimd. NeuronCore-v4+: GpSimd, Vector.
- `src_seeds` must be in SBUF.

- **src_seeds** — the source tensor containing seed values for the PRNG; must be a 2D uint32 tensor
                  with the partition dimension representing the compute lanes and the free dimension
                  containing the seed values
- **engine** — specify which engine to use: `nki.isa.engine.gpsimd` (default)
               or `nki.isa.engine.vector` (NeuronCore-v4+)

---

### nki.isa.range_select {#nki-isa-range_select}

`nki.isa.range_select(dst, on_true_tile, comp_op0, comp_op1, bound0, bound1, reduce_cmd, reduce_res, reduce_op, range_start, on_false_value, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.range_select(dst, on_true_tile, comp_op0, comp_op1, bound0, bound1, reduce_cmd=reduce_cmd.reset_reduce, reduce_res=None, reduce_op=maximum, range_start=0, on_false_value=..., name=None)
```

Select elements from `on_true_tile` based on comparison with bounds using Vector Engine.

> **Note:**
>
> Available only on NeuronCore-v3 and newer.
>

For each element in `on_true_tile`, compares its free dimension index + `range_start` against `bound0` and `bound1`
using the specified comparison operators (`comp_op0` and `comp_op1`). If both comparisons
evaluate to True, copies the element to the output; otherwise uses  `on_false_value`.

Additionally performs a reduction operation specified by `reduce_op` on the results,
storing the reduction result in `reduce_res`.

**Note on numerical stability:**

In self-attention, we often have this instruction sequence: `range_select` (VectorE) -> `reduce_res` -> `activation` (ScalarE).
When `range_select` outputs a full row of `fill_value`, caution is needed to avoid NaN in the
activation instruction that subtracts the output of `range_select` by `reduce_res` (max value):

- If `dst.dtype` and `reduce_res.dtype` are both FP32, we should not hit any NaN issue
  since `FP32_MIN - FP32_MIN = 0`. Exponentiation on 0 is stable (1.0 exactly).

- If `dst.dtype` is FP16/BF16/FP8, the fill_value in the output tile will become `-INF`
  since HW performs a downcast from FP32_MIN to a smaller dtype.
  In this case, you must make sure `reduce_res.dtype` is FP32 to avoid NaN in `activation`.
  NaN can be avoided because `activation` always upcasts input tiles to FP32 to perform math operations: `-INF - FP32_MIN = -INF`.
  Exponentiation on `-INF` is stable (0.0 exactly).

**Constraints:**

The comparison operators must be one of:

- nl.equal
- nl.less
- nl.less_equal
- nl.greater
- nl.greater_equal

Partition dim sizes must match across `on_true_tile`, `bound0`, and `bound1`:

- `bound0` and `bound1` must have one element per partition
- `on_true_tile` must be one of the FP dtypes, and `bound0/bound1` must be FP32 types.
- `on_true_tile` and `dst` can have at most two free dimensions (maximum rank 3,
  including the partition dimension).

The comparison with `bound0`, `bound1`, and free dimension index is done in FP32.
Make sure `range_start` + free dimension index is within 2^24 range.

> **Note:**
> The accumulator registers are shared across Vector Engine accumulation instructions including
> `~nki.isa.exponential`,
> `~nki.isa.range_select`,
> `~nki.isa.scalar_tensor_tensor`,
> `~nki.isa.select_reduce`,
> `~nki.isa.tensor_scalar_cumulative`, and
> `~nki.isa.tensor_scalar_reduce`.
>

**Numpy equivalent:**

```python
indices = np.zeros_like(on_true_tile, dtype=np.float32)
indices[:] = range_start + np.arange(on_true_tile[0].size)

mask = comp_op0(indices, bound0) & comp_op1(indices, bound1)
select_out_tile = np.where(mask, on_true_tile, on_false_value)
reduce_tile = reduce_op(select_out_tile, axis=1, keepdims=True)

```

- **dst** — output tile with selected elements
- **on_true_tile** — input tile containing elements to select from
- **on_false_value** — constant value to use when selection condition is False.
  Due to hardware constraints, this must be `FP32_MIN` (`-3.4028235e+38`).
  See the numerical stability note above for guidance on output dtype selection.
- **comp_op0** — first comparison operator
- **comp_op1** — second comparison operator
- **bound0** — tile with one element per partition for first comparison
- **bound1** — tile with one element per partition for second comparison
- **reduce_op** — reduction operator to apply on across the selected output. Currently only `nl.maximum` is supported.
- **reduce_cmd** — controls the state of the Vector Engine accumulator registers.
  Defaults to `reduce_cmd.reset_reduce`.
- **reduce_res** — optional tile to store reduction results.

  Pass `None` to keep the reduction result in the Vector Engine's internal
  accumulator without writing it out. This is useful when chaining multiple
  calls that reduce into the same accumulator — only the final call needs to
  pass a tile to retrieve the accumulated result.
- **range_start** — starting base offset for index array for the free dimension of `on_true_tile`.
    Defaults to 0, and must be a compile-time integer.

---

### nki.isa.reciprocal {#nki-isa-reciprocal}

`nki.isa.reciprocal(dst, data, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.reciprocal(dst, data, name=None)
```

Compute element-wise reciprocal (1.0/x) of the input `data` tile using Vector Engine.

**Memory types.**

Both the input `data` and output `dst` tiles can be in SBUF or PSUM.

**Data types.**

The input `data` tile can be any valid NKI data type (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).
The Vector Engine automatically casts the input data type to float32 and performs the reciprocal
computation in float32 math. The float32 results are cast to the data type of `dst`.
See nki-type-casting for more information about type casting behavior.

**Layout.**

The partition dimension of the input `data` is considered the parallel compute dimension.

**Tile size.**

The partition dimension size of input `data` and output `dst` tiles must be the same
and must not exceed 128. The number of elements per partition of `dst` must match
that of `data` and must not exceed the physical size of each SBUF partition.
Both tiles can have at most four free dimensions (maximum rank 5, including
the partition dimension).

- **dst** — the output tile
- **data** — the input tile

---

### nki.isa.reduce_cmd {#nki-isa-reduce_cmd}

`nki.isa.reduce_cmd(value)`

**Signature:**
```python
isa.reduce_cmd(value)
```

Engine register reduce commands.

**Attributes**

| Name | Description |
| --- | --- |
| `idle` | Not using the accumulator registers |
| `reset` | Resets the accumulator registers to its initial state |
| `reduce` | Keeps accumulating over the current value of the accumulator registers |
| `reset_reduce` | Resets the accumulator registers then immediately accumulate the results of the current instruction into the accumulators |
| `load_reduce` | Loads a value into the accumulator registers, then accumulate the results of the current instruction into the accumulators |

---

### nki.isa.register_alloc {#nki-isa-register_alloc}

`nki.isa.register_alloc(x)`

**Engine:** GpSimd Engine

**Signature:**
```python
isa.register_alloc(x=None)
```

Allocate a virtual register and optionally initialize it with a value.

Each engine sequencer (Tensor/Scalar/Vector/GpSimd/Sync Engine) within a NeuronCore maintains its own set of
physical registers for scalar operations (64x 32-bit registers per engine sequencer in NeuronCore v2-v4).
This API conceptually allocates a register within a virtual register space.
Users do not need to explicitly free a register through nisa APIs. The NKI compiler
handles physical register allocation (and deallocation) across the appropriate engine sequencers
based on the dynamic program flow.

NKI provides the following APIs to manipulate allocated registers:

- `nisa.register_move`: Move a constant integer or another register's value into a register
- `nisa.register_load`: Load a scalar (32-bit) value from HBM/SBUF into a register
- `nisa.register_store`: Store register contents to HBM/SBUF

In the current NKI release, these registers are primarily used to specify dynamic loop boundaries and
while loop conditions. The NKI compiler compiles such dynamic looping constructs to branching instructions
executed by engine sequencers. For additional details, see `nl.dynamic_range`. For more information
on engine sequencer and its capabilities, see
[Trainium/Inferentia2 architecture guide](../../architecture/trainium_inferentia2_arch.md).

- **x** — optional initialization value. Can be one of:

          - `None` (default): allocate an uninitialized register
          - `int`: allocate a register initialized with this immediate integer value

Example:

Three ways to allocate a register initialized to zero:

```python
# Approach 1: Using an immediate value
reg1 = nisa.register_alloc(0)

# Approach 2: Two-step with register_load
zero_tensor = nl.zeros([1, 1], dtype=nl.int32, buffer=nl.sbuf)
reg2 = nisa.register_alloc(None)
nisa.register_load(reg2, zero_tensor)
```

---

### nki.isa.register_load {#nki-isa-register_load}

`nki.isa.register_load(dst, src)`

**Signature:**
```python
isa.register_load(dst, src)
```

Load a scalar value from memory (HBM or SBUF) into a virtual register.

This instruction reads a single scalar value (up to 32-bit) from a memory location (HBM or SBUF)
and stores it in the specified virtual register. The source must be a NKI tensor with exactly
one element (shape [1] or [1, 1]). This enables dynamic loading of values computed at
runtime into registers for use in control flow operations.

The virtual register system allows the NKI compiler to allocate physical registers across
different engine sequencers as needed. See `nisa.register_alloc` for more details on
virtual register allocation.

`src` can be any valid NKI data type (see [Supported Data Types](nki.api.shared.md#nki-dtype)) except the
packed MX x4 types (`nl.float8_e5m2_x4`, `nl.float8_e4m3fn_x4`,
`nl.float4_e2m1fn_x4`), which are only defined for `nisa.quantize_mx`
and `nisa.nc_matmul_mx`.

- **dst** — the destination virtual register (allocated via `nisa.register_alloc`)
- **src** — the source tensor containing a single scalar value to load

Example:

```python
# Load a computed value into a register
computed_bound = nl.ones([1], dtype=nl.int32, buffer=nl.sbuf)  # bound of 1 in SBUF
loop_reg = nisa.register_alloc()
nisa.register_load(loop_reg, computed_bound)
```

---

### nki.isa.register_move {#nki-isa-register_move}

`nki.isa.register_move(dst, src)`

**Signature:**
```python
isa.register_move(dst, src)
```

Move a value into a virtual register.

This instruction loads a value into the specified virtual register. The source can be
either a compile-time constant integer or another virtual register.

The virtual register system allows the NKI compiler to allocate physical registers across
different engine sequencers as needed. See `nisa.register_alloc` for more details on
virtual register allocation.

This instruction operates on virtual registers only and does not access SBUF, PSUM, or HBM.

- **dst** — the destination virtual register (allocated via `nisa.register_alloc`)
- **src** — source value - either a compile-time constant integer or a VirtualRegister

Example:

```python
# Allocate a register and initialize it with a constant
loop_count = nisa.register_alloc()
nisa.register_move(loop_count, 10)  # Set register to 10

# Copy from another register
reg2 = nisa.register_alloc()
nisa.register_move(reg2, loop_count)  # Copy value from loop_count
```

---

### nki.isa.register_store {#nki-isa-register_store}

`nki.isa.register_store(dst, src)`

**Signature:**
```python
isa.register_store(dst, src)
```

Store the value from a virtual register into memory (HBM/SBUF).

This instruction writes the scalar value (up to 32-bit) stored in a virtual register to a memory location
(HBM or SBUF). The destination must be a tensor with exactly one element (shape [1] or [1, 1]).
This enables saving register values back to memory for later use or for output purposes.

The virtual register system allows the NKI compiler to allocate physical registers across
different engine sequencers as needed. See `nisa.register_alloc` for more details on
virtual register allocation.

`dst` can be any valid NKI data type (see [Supported Data Types](nki.api.shared.md#nki-dtype)) except the
packed MX x4 types (`nl.float8_e5m2_x4`, `nl.float8_e4m3fn_x4`,
`nl.float4_e2m1fn_x4`), which are only defined for `nisa.quantize_mx`
and `nisa.nc_matmul_mx`.

- **dst** — the destination tensor with a single element to store the register value
- **src** — the source virtual register (allocated via `nisa.register_alloc`)

Example:

```python
# Store a register value back to memory
counter_reg = nisa.register_alloc(0)
# ... perform operations that modify counter_reg ...
result_tensor = nl.ndarray([1], dtype=nl.int32, buffer=nl.sbuf)
nisa.register_store(result_tensor, counter_reg)
```

---

### nki.isa.rng {#nki-isa-rng}

`nki.isa.rng(dst, engine, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.rng(dst, engine=engine.vector, name=None)
```

Generate pseudo random numbers using the Vector or GpSimd Engine.

This instruction generates 32 random bits per element and writes them to the
destination tensor. Depending on the size of the dtype, the instruction truncates
each 32-bit random value to the specified data type, taking the least significant bits.

Example use case:
To generate random FP32 numbers between 0.0 and 1.0, follow the Rng instruction
with a normalization instruction (e.g., write 16 random bits as UINT16, then
divide by (2^16-1) to get a random FP32 number between 0.0 and 1.0).

**Memory types.**

The output `dst` tile can be in SBUF or PSUM.

**Data types.**

The output `dst` tile must be an integer type: int8, int16, int32, uint8, uint16, or uint32.

**Tile size.**

The partition dimension size of `dst` must not exceed 128. The number of
elements per partition of `dst` must not exceed the physical size of each SBUF/PSUM partition.
`dst` can have at most four free dimensions (maximum rank 5, including the
partition dimension).

**Constraints.**

- Supported arch versions: NeuronCore-v2+.
- Supported engines: NeuronCore-v2: Vector. NeuronCore-v3+: GpSimd, Vector.
- Since GpSimd Engine cannot access PSUM, `dst` must be in SBUF when using GpSimd Engine.

- **dst** — the destination tensor to write random values to
- **engine** — specify which engine to use: `nki.isa.engine.vector` (default)
               or `nki.isa.engine.gpsimd` (NeuronCore-v3+)

---

### nki.isa.scalar_load {#nki-isa-scalar_load}

`nki.isa.scalar_load(src)`

**Engine:** DMA Engine

**Signature:**
```python
isa.scalar_load(src)
```

Load a scalar value from a 1x1 tensor.

Returns an `~nki.isa.NkiScalar`, whose `dtype` matches `src` and
fixes the width of every value derived from it. See that class for which
operations each width supports, and nki-scalar for a walkthrough.

The source tensor must use `nl.int32` or `nl.int64` dtype. Unsigned
dtypes are not accepted; use the signed dtype of the same width.

Only the tracer frontend supports this. The parser frontend rejects the
call.

Unlike the compute instructions, `scalar_load` accepts `nl.int64`, and
the value is not truncated. An int64 scalar can be written back with
`nki.isa.scalar_store`, and can be passed as `scalar_offset` to
address an HBM tensor beyond the 4GB reach of a 32-bit byte offset. It
cannot bound an `nki.language.fori_loop`, because the loop's
trip-count comparison has no 64-bit form.

A scalar of either dtype can be used as a `scalar_offset` argument to
access-pattern methods for dynamic addressing, with one restriction: on an
SBUF or PSUM operand the offset must be `nl.int32`. On-chip buffers are
megabytes, so a 64-bit offset there is rejected rather than silently
narrowed.

Args:
    src: A 1x1 tensor in SBUF or HBM with dtype `nl.int32` or `nl.int64`.

Returns:
    NkiScalar: The loaded scalar, with `dtype` matching `src`.

Example::

    buf = nl.ndarray((1, 1), dtype=nl.int32, buffer=nl.sbuf)
    nisa.dma_copy(dst=buf, src=src)
    idx = nisa.scalar_load(buf)
    idx = idx + 1  # arithmetic on the scalar

---

### nki.isa.scalar_store {#nki-isa-scalar_store}

`nki.isa.scalar_store(dst, value)`

**Signature:**
```python
isa.scalar_store(dst, value)
```

Store a scalar value to a 1x1 tensor in memory.

Like `nki.isa.scalar_load`, this accepts `nl.int64` as well as
`nl.int32`, and a 64-bit value is written back without truncation.

`dst` must have the same width as `value`. A 32-bit scalar cannot be
stored into an `nl.int64` tile, nor a 64-bit scalar into an `nl.int32`
one; store at the scalar's own width and convert in memory if needed.

Only the tracer frontend supports this, matching
`nki.isa.scalar_load`.

`value` must be an `~nki.isa.NkiScalar`. A Python `int` is not
accepted: it stored nothing on the tracer, where the instruction needs a
register, and only appeared to work under `nki.simulate`. Use
`nki.isa.memset` to write a compile-time constant into a tile.

Args:
    dst: A 1x1 tensor in SBUF or HBM with dtype `nl.int32` or `nl.int64`,
        matching the width of `value`.
    value: An `~nki.isa.NkiScalar` to store.

Example::

    out_buf = nl.ndarray((1, 1), dtype=nl.int32, buffer=nl.sbuf)
    nisa.scalar_store(out_buf, result)

---

### nki.isa.select_reduce {#nki-isa-select_reduce}

`nki.isa.select_reduce(dst, predicate, on_true, on_false, reduce_res, reduce_cmd, reduce_op, reverse_pred, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.select_reduce(dst, predicate, on_true, on_false, reduce_res=None, reduce_cmd=reduce_cmd.idle, reduce_op=maximum, reverse_pred=False, name=None)
```

Selectively copy elements from either `on_true` or `on_false` to the destination tile
based on a `predicate` using Vector Engine, with optional reduction (max).

The operation can be expressed in NumPy as:

```python
# Select:
predicate = ~predicate if reverse_pred else predicate
result = np.where(predicate, on_true, on_false)

# With Reduce:
reduction_result = np.max(result, axis=1, keepdims=True)

```

**Memory constraints:**

- Both `on_true` and `predicate` are permitted to be in SBUF
- Either `on_true` or `predicate` may be in PSUM, but not both simultaneously
- The destination `dst` can be in either SBUF or PSUM

**Shape and data type constraints:**

- `on_true`, `dst`, and `predicate` must have identical shapes (same number of partitions and elements per partition)
- `on_true` can be any supported dtype except `tfloat32`, `int32`, `uint32`
- `on_false` dtype must be `float32` if `on_false` is a scalar.
- `on_false` has to be either scalar or vector of shape `(on_true.shape[0], 1)`
- `predicate` dtype can be any supported integer type `int8`, `uint8`, `int16`, `uint16`
- `reduce_res` must be a vector of shape `(on_true.shape[0], 1)`
- `reduce_res` dtype must of float type
- `reduce_op` only supports `max`
- `on_true`, `predicate`, and `dst` can have at most two free dimensions
  (maximum rank 3, including the partition dimension)

**Behavior:**

- Where predicate is True: The corresponding elements from `on_true` are copied to `dst`
- Where predicate is False: The corresponding elements from `on_false` are copied to `dst`
- When reduction is enabled, the max value from each partition of the `result` is computed and stored in `reduce_res`

**Accumulator behavior:**

The Vector Engine maintains internal accumulator registers that can be controlled via the `reduce_cmd` parameter:

- `nisa.reduce_cmd.reset_reduce`: Reset accumulators to -inf, then accumulate the current results
- `nisa.reduce_cmd.reduce`: Continue accumulating without resetting (useful for multi-step reductions)
- `nisa.reduce_cmd.idle`: No accumulation performed (default)

> **Note:**
> Even when `reduce_cmd` is set to `idle`, the accumulator state may still be modified.
> Always use `reset_reduce` after any operations that ran with `idle` mode to ensure
> consistent behavior.
>
> **Note:**
> The accumulator registers are shared across Vector Engine accumulation instructions including
> `~nki.isa.exponential`,
> `~nki.isa.range_select`,
> `~nki.isa.scalar_tensor_tensor`,
> `~nki.isa.select_reduce`,
> `~nki.isa.tensor_scalar_cumulative`, and
> `~nki.isa.tensor_scalar_reduce`.
>

- **dst** — The destination tile to write the selected values to
- **predicate** — Tile that determines which value to select (on_true or on_false)
- **on_true** — Tile to select from when predicate is True
- **on_false** — Value to use when predicate is False, can be a scalar value or a vector tile of `(on_true.shape[0], 1)`
- **reduce_res** — (optional) Tile to store reduction results, must have shape `(on_true.shape[0], 1)`

    Pass `None` to keep the reduction result in the Vector Engine's internal
    accumulator without writing it out. This is useful when chaining multiple
    calls that reduce into the same accumulator — only the final call needs to
    pass a tile to retrieve the accumulated result.
- **reduce_cmd** — (optional) Control accumulator behavior using `nisa.reduce_cmd` values, defaults to idle
- **reduce_op** — (optional) Reduction operator to apply (only `nl.maximum` is supported)
- **reverse_pred** — (optional) Reverse the meaning of the predicate condition, defaults to False

---

### nki.isa.sequence_bounds {#nki-isa-sequence_bounds}

`nki.isa.sequence_bounds(dst, segment_ids, name)`

**Engine:** GpSimd Engine

**Signature:**
```python
isa.sequence_bounds(dst, segment_ids, name=None)
```

Compute the sequence bounds for a given set of segment IDs using GpSIMD Engine.

Given a tile of segment IDs, this function identifies where each segment begins and ends.
For each element, it returns a pair of values: [start_index, end_index] indicating
the boundaries of the segment that element belongs to. All segment IDs must be non-negative
integers. Padding elements (with segment ID of zero) receive special boundary
values: a start index of n and an end index of (-1), where n is the length
of `segment_ids`.

The output tile contains two values per input element: the start index (first column)
and end index (second column) of each segment. The partition dimension must always be 1.
For example, with input shape (1, 512), the output shape becomes (1, 2, 512), where
the additional dimension holds the start and end indices for each element.

Both the input tile (`segment_ids`) and output tile (`dst`) must have data type `nl.float32` or `nl.int32`.
The input tile (`segment_ids`) can have at most two free dimensions (maximum rank 3).
The output tile (`dst`) can have at most three free dimensions (maximum rank 4).

**NumPy equivalent:**

```python
def compute_sequence_bounds(sequence):
  n = len(sequence)

  min_bounds = np.zeros(n, dtype=sequence.dtype)
  max_bounds = np.zeros(n, dtype=sequence.dtype)

  min_bound_pad = n
  max_bound_pad = -1

  min_bounds[0] = 0 if sequence[0] != 0 else min_bound_pad
  for i in range(1, n):
    if sequence[i] == 0:
      min_bounds[i] = min_bound_pad
    elif sequence[i] == sequence[i - 1]:
      min_bounds[i] = min_bounds[i - 1]
    else:
      min_bounds[i] = i

  max_bounds[-1] = n if sequence[-1] != 0 else max_bound_pad
  for i in range(n - 2, -1, -1):
    if sequence[i] == 0:
      max_bounds[i] = max_bound_pad
    elif sequence[i] == sequence[i + 1]:
      max_bounds[i] = max_bounds[i + 1]
    else:
      max_bounds[i] = i + 1

  return np.vstack((min_bounds, max_bounds))

b = (
  np.apply_along_axis(
    compute_sequence_bounds, axis=1, arr=reshaped_segment_ids
  )
  .reshape(m, 2, n)
  .astype(np.float32)
)

```

- **dst** — tile containing the sequence bounds.
- **segment_ids** — tile containing the segment IDs. Elements with ID=0 are treated as padding.

---

### nki.isa.set_rng_seed {#nki-isa-set_rng_seed}

`nki.isa.set_rng_seed(src_seeds, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.set_rng_seed(src_seeds, name=None)
```

Seed the pseudo random number generator (PRNG) inside the Vector Engine.

The PRNG state is cached inside the engine as a persistent state during the rest of NEFF
execution. However, the state cannot survive TPB resets or Runtime reload.

Using the same seed will generate the same sequence of random numbers when used
together with the `nisa.rng()` on the Vector Engine.

**Memory types.**

The input `src_seeds` must be in SBUF or PSUM.

**Data types.**

The input `src_seeds` must be a 32-bit value.

**Tile size.**

The input `src_seeds` must be a [1,1] tensor.

- **src_seeds** — a [1,1] tensor on SBUF or PSUM with a 32-bit value to be used as the seed

---

