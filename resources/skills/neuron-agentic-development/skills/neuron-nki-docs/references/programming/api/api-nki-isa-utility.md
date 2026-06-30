# NKI ISA - Utility Functions

> **Module**: nki.isa
> **Total Functions**: 6

## Overview

Utility and helper ISA functions.

## Functions

### nki.isa.affine_select {#nki-isa-affine_select}

# nki.isa.affine_select

nki.isa.affine_select

nki.isa.affine_select(*dst*, *pattern*, *offset*, *channel_multiplier*, *on_true_tile*, *on_false_value*, *cmp_op=<function equal>*, *name=None*)[[source]](../../../_modules/nki/isa.html#affine_select)
Select elements between an input tile `on_true_tile` and a scalar value `on_false_value`
according to a boolean predicate tile using GpSimd Engine.

The predicate tile is calculated on-the-fly in the engine by evaluating an affine expression element-by-element.
The affine expression is defined by a `pattern`, `offset`, and `channel_multiplier`, similar to `nisa.iota`.
The `pattern` field is a list of lists in the form of
`[[step_w, num_w], [step_z, num_z], [step_y, num_y], [step_x, num_x]]`. When fewer than 4D `pattern`
is provided, NKI compiler automatically pads remaining dimensions with size of 1.

Given a 4D pattern (padded if needed), the instruction generates a predicate using the following pseudo code:


```python
num_partitions = dst.shape[0]
[[step_w, num_w], [step_z, num_z], [step_y, num_y], [step_x, num_x]] = pattern

for channel_id in range(num_partitions):
  for w in range(num_w):
    for z in range(num_z):
      for y in range(num_y):
        for x in range(num_x):
          affine_value = offset + (channel_id * channel_multiplier) +
                        (w * step_w) + (z * step_z) + (y * step_y) + (x * step_x)

          predicate = cmp_op(affine_value, 0)  # Compare with 0 using cmp_op

          if predicate:
              dst[channel_id, w, z, y, x] = on_true_tile[channel_id, w, z, y, x]
          else:
              dst[channel_id, w, z, y, x] = on_false_value
```


The above pseudo code assumes `dst` has the same size in every dimension `x/y/z/w` for simplicity. However,
the instruction allows any sizes in the free dimension, as long as the number of elements per partition in `dst`
matches the product: `num_w * num_z * num_y * num_x`.

A common use case for `affine_select` is to apply a causal mask on the attention
scores for transformer decoder models.

**Memory types.**

The output `dst` tile must be in SBUF. The input `on_true_tile` must also be in SBUF.

**Data types.**

The input `on_true_tile` and output `dst` tile can be any valid NKI data type
(see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information). If the data type of `on_true_tile` differs from
that of `dst`, the input elements in `on_true_tile`, if selected, are first cast to FP32
before converting to the output data type in `dst`.
The `on_false_value` must be float32, regardless of the input/output tile data types.

**Layout.**

The partition dimension determines the number of active channels for parallel pattern generation and selection.
The input tile `on_true_tile`, the calculated boolean predicate tile, and the returned output tile
must have the same partition dimension size and.

**Tile size.**

* The partition dimension size of `dst` and `on_true_tile` must be the same and must not exceed 128.

* The number of elements per partition of `dst` and `on_true_tile` must not
exceed the physical size of each SBUF partition.

* The total number of elements in `pattern` must match the number of elements
per partition in the `dst` and `on_true_tile` tiles.

Parameters:

* **dst** – the output tile in SBUF to store the selected values

* **pattern** – a list of [step, num] to describe up to 4D tensor sizes and strides for affine expression generation

* **offset** – an int32 offset value to be added to every generated affine value

* **channel_multiplier** – an int32 multiplier to be applied to the channel (partition) ID

* **on_true_tile** – an input tile for selection with a `True` predicate value

* **on_false_value** – a scalar value for selection with a `False` predicate value

* **cmp_op** – comparison operator to use for predicate evaluation (default: nl.equal)

---

### nki.isa.iota {#nki-isa-iota}

# nki.isa.iota

nki.isa.iota

nki.isa.iota(*dst*, *pattern*, *offset*, *channel_multiplier=0*, *name=None*)[[source]](../../../_modules/nki/isa.html#iota)
Generate a constant literal pattern into SBUF using GpSimd Engine.

The pattern is defined by an int32 `offset`, a tensor access pattern of up to 4D `pattern` and
an int32 `channel_multiplier`. The `pattern` field is a list of lists in the form of
`[[step_w, num_w], [step_z, num_z], [step_y, num_y], [step_x, num_x]]`. When fewer than 4D `pattern`
is provided, NKI compiler automatically pads remaining dimensions with size of 1.

Given a 4D pattern (padded if needed), the instruction generates a stream of values using the following pseudo code:


```python
num_partitions = dst.shape[0]
[[step_w, num_w], [step_z, num_z], [step_y, num_y], [step_x, num_x]] = pattern

for channel_id in range(num_partitions):
    for w in range(num_w):
        for z in range(num_z):
            for y in range(num_y):
                for x in range(num_x):
                    value = offset + (channel_id * channel_multiplier) +
                            (w * step_w) + (z * step_z) + (y * step_y) + (x * step_x)

                    dst[channel_id, w, z, y, x] = value
```


The above pseudo code assumes `dst` has the same size in every dimension `x/y/z/w` for simplicity. However,
the instruction allows any sizes in the free dimension, as long as the number of elements per partition in `dst`
matches the product: `num_w * num_z * num_y * num_x`.

**Memory types.**

The output `dst` tile must be in SBUF.

**Data types.**

The generated values are computed in 32-bit integer arithmetic. The GpSimd Engine can cast
these integer results to any valid NKI data type (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information)
before writing to the output tile. The output data type is determined by the `dst` tile’s
data type.

**Layout.**

The partition dimension determines the number of active channels for parallel pattern generation.

**Tile size.**

The partition dimension size of `dst` must not exceed 128. The number of
elements per partition of `dst` must not exceed the physical size of each SBUF partition.
The total number of elements in `pattern` must match the number of elements per partition in the `dst` tile.

Parameters:

* **dst** – the output tile in SBUF to store the generated pattern

* **pattern** – a list of [step, num] to describe up to 4D tensor sizes and strides

* **offset** – an int32 offset value to be added to every generated value

* **channel_multiplier** – an int32 multiplier to be applied to the channel (parition) ID

---

### nki.isa.max8 {#nki-isa-max8}

# nki.isa.max8

nki.isa.max8

nki.isa.max8(*dst*, *src*, *name=None*)[[source]](../../../_modules/nki/isa.html#max8)
Find the 8 largest values in each partition of the source tile.

This instruction reads the input elements, converts them to fp32 internally, and outputs
the 8 largest values in descending order for each partition. Outputs are converted to
`dst.dtype` automatically.

The source tile can be up to 5-dimensional, while the output tile is always 2-dimensional.
The number of elements read per partition must be between 8 and 16,384 inclusive.
The output will always contain exactly 8 elements per partition.
The source and output must have the same partition dimension size:

* source: [par_dim, …]

* output: [par_dim, 8]

Parameters:

* **dst** – a 2D tile containing the 8 largest values per partition in descending order with shape [par_dim, 8]

* **src** – the source tile to find maximum values from

---

### nki.isa.range_select {#nki-isa-range_select}

# nki.isa.range_select

nki.isa.range_select

nki.isa.range_select(*dst*, *on_true_tile*, *comp_op0*, *comp_op1*, *bound0*, *bound1*, *reduce_cmd=reduce_cmd.idle*, *reduce_res=None*, *reduce_op=<function maximum>*, *range_start=0.0*, *on_false_value=0.0*, *name=None*)[[source]](../../../_modules/nki/isa.html#range_select)
Select elements from `on_true_tile` based on comparison with bounds using Vector Engine.

> **Note**
>
> Note
> 
> 
> Available only on NeuronCore-v3 and newer.

For each element in `on_true_tile`, compares its free dimension index + `range_start` against `bound0` and `bound1`
using the specified comparison operators (`comp_op0` and `comp_op1`). If both comparisons
evaluate to True, copies the element to the output; otherwise uses `on_false_value`.

Additionally performs a reduction operation specified by `reduce_op` on the results,
storing the reduction result in `reduce_res`.

**Note on numerical stability:**

In self-attention, we often have this instruction sequence: `range_select` (VectorE) -> `reduce_res` -> `activation` (ScalarE).
When `range_select` outputs a full row of `fill_value`, caution is needed to avoid NaN in the
activation instruction that subtracts the output of `range_select` by `reduce_res` (max value):

* If `dst.dtype` and `reduce_res.dtype` are both FP32, we should not hit any NaN issue
since `FP32_MIN - FP32_MIN = 0`. Exponentiation on 0 is stable (1.0 exactly).

* If `dst.dtype` is FP16/BF16/FP8, the fill_value in the output tile will become `-INF`
since HW performs a downcast from FP32_MIN to a smaller dtype.
In this case, you must make sure `reduce_res.dtype` is FP32 to avoid NaN in `activation`.
NaN can be avoided because `activation` always upcasts input tiles to FP32 to perform math operations: `-INF - FP32_MIN = -INF`.
Exponentiation on `-INF` is stable (0.0 exactly).

**Constraints:**

The comparison operators must be one of:

* nl.equal

* nl.less

* nl.less_equal

* nl.greater

* nl.greater_equal

Partition dim sizes must match across `on_true_tile`, `bound0`, and `bound1`:

* `bound0` and `bound1` must have one element per partition

* `on_true_tile` must be one of the FP dtypes, and `bound0/bound1` must be FP32 types.

The comparison with `bound0`, `bound1`, and free dimension index is done in FP32.
Make sure `range_start` + free dimension index is within 2^24 range.

**Numpy equivalent:**


```python
indices = np.zeros_like(on_true_tile, dtype=np.float32)
indices[:] = range_start + np.arange(on_true_tile[0].size)

mask = comp_op0(indices, bound0) & comp_op1(indices, bound1)
select_out_tile = np.where(mask, on_true_tile, on_false_value)
reduce_tile = reduce_op(select_out_tile, axis=1, keepdims=True)
```


Parameters:

* **dst** – output tile with selected elements

* **on_true_tile** – input tile containing elements to select from

* **on_false_value** – constant value to use when selection condition is False.
Due to HW constraints, this must be FP32_MIN FP32 bit pattern

* **comp_op0** – first comparison operator

* **comp_op1** – second comparison operator

* **bound0** – tile with one element per partition for first comparison

* **bound1** – tile with one element per partition for second comparison

* **reduce_op** – reduction operator to apply on across the selected output. Currently only `nl.maximum` is supported.

* **reduce_res** – optional tile to store reduction results.

* **range_start** – starting base offset for index array for the free dimension of `on_true_tile`.
Defaults to 0, and must be a compile-time integer.

---

### nki.isa.select_reduce {#nki-isa-select_reduce}

# nki.isa.select_reduce

nki.isa.select_reduce

nki.isa.select_reduce(*dst*, *predicate*, *on_true*, *on_false*, *reduce_res=None*, *reduce_cmd=reduce_cmd.idle*, *reduce_op=<function maximum>*, *reverse_pred=False*, *name=None*)[[source]](../../../_modules/nki/isa.html#select_reduce)
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

* Both `on_true` and `predicate` are permitted to be in SBUF

* Either `on_true` or `predicate` may be in PSUM, but not both simultaneously

* The destination `dst` can be in either SBUF or PSUM

**Shape and data type constraints:**

* `on_true`, `dst`, and `predicate` must have identical shapes (same number of partitions and elements per partition)

* `on_true` can be any supported dtype except `tfloat32`, `int32`, `uint32`

* `on_false` dtype must be `float32` if `on_false` is a scalar.

* `on_false` has to be either scalar or vector of shape `(on_true.shape[0], 1)`

* `predicate` dtype can be any supported integer type `int8`, `uint8`, `int16`, `uint16`

* `reduce_res` must be a vector of shape `(on_true.shape[0], 1)`

* `reduce_res` dtype must of float type

* `reduce_op` only supports `max`

**Behavior:**

* Where predicate is True: The corresponding elements from `on_true` are copied to `dst`

* Where predicate is False: The corresponding elements from `on_false` are copied to `dst`

* When reduction is enabled, the max value from each partition of the `result` is computed and stored in `reduce_res`

**Accumulator behavior:**

The Vector Engine maintains internal accumulator registers that can be controlled via the `reduce_cmd` parameter:

* `nisa.reduce_cmd.reset_reduce`: Reset accumulators to -inf, then accumulate the current results

* `nisa.reduce_cmd.reduce`: Continue accumulating without resetting (useful for multi-step reductions)

* `nisa.reduce_cmd.idle`: No accumulation performed (default)

> **Note**
>
> Note
> 
> 
> Even when `reduce_cmd` is set to `idle`, the accumulator state may still be modified.
> Always use `reset_reduce` after any operations that ran with `idle` mode to ensure
> consistent behavior.

> **Note**
>
> Note
> 
> 
> The accumulator registers are shared for other Vector Engine accumulation instructions such [nki.isa.range_select](nki.isa.range_select.md)

Parameters:

* **dst** – The destination tile to write the selected values to

* **predicate** – Tile that determines which value to select (on_true or on_false)

* **on_true** – Tile to select from when predicate is True

* **on_false** – Value to use when predicate is False, can be a scalar value or a vector tile of `(on_true.shape[0], 1)`

* **reduce_res** – (optional) Tile to store reduction results, must have shape `(on_true.shape[0], 1)`

* **reduce_cmd** – (optional) Control accumulator behavior using `nisa.reduce_cmd` values, defaults to idle

* **reduce_op** – (optional) Reduction operator to apply (only `nl.maximum` is supported)

* **reverse_pred** – (optional) Reverse the meaning of the predicate condition, defaults to False

---

### nki.isa.sequence_bounds {#nki-isa-sequence_bounds}

# nki.isa.sequence_bounds

nki.isa.sequence_bounds

nki.isa.sequence_bounds(*dst*, *segment_ids*, *name=None*)[[source]](../../../_modules/nki/isa.html#sequence_bounds)
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


Parameters:

* **dst** – tile containing the sequence bounds.

* **segment_ids** – tile containing the segment IDs. Elements with ID=0 are treated as padding.

---
