# nki.isa — Utility & Gather

> **Module**: nki.isa
> **Functions**: 3

## Overview

Utility & Gather instructions.

## Functions

### nki.isa.affine_select {#nki-isa-affine_select}

`nki.isa.affine_select(dst, pattern, channel_multiplier, on_true_tile, on_false_value, cmp_op, offset, name)`

**Engine:** GpSimd Engine

**Signature:**
```python
isa.affine_select(dst, pattern, channel_multiplier, on_true_tile, on_false_value, cmp_op=equal, offset=0, name=None)
```

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
See nki-type-casting for more information about type casting behavior.
The `on_false_value` must be float32, regardless of the input/output tile data types.

**Layout.**

The partition dimension determines the number of active channels for parallel pattern generation and selection.
The input tile `on_true_tile`, the calculated boolean predicate tile, and the returned output tile
must have the same partition dimension size and.

**Tile size.**

- The partition dimension size of `dst` and `on_true_tile` must be the same and must not exceed 128.
- The number of elements per partition of `dst` and `on_true_tile` must not
  exceed the physical size of each SBUF partition.
- The total number of elements in `pattern` must match the number of elements
  per partition in the `dst` and `on_true_tile` tiles.
- `dst` and `on_true_tile` can have at most two free dimensions (maximum
  rank 3, including the partition dimension).

- **dst** — the output tile in SBUF to store the selected values
- **pattern** — a list of [step, num] to describe up to 4D tensor sizes and strides for affine expression generation
- **offset** — an int32 offset value to be added to every generated affine value
- **channel_multiplier** — an int32 multiplier to be applied to the channel (partition) ID
- **on_true_tile** — an input tile for selection with a `True` predicate value
- **on_false_value** — a scalar value for selection with a `False` predicate value
- **cmp_op** — comparison operator to use for predicate evaluation (default: nl.equal)

---

### nki.isa.iota {#nki-isa-iota}

`nki.isa.iota(dst, pattern, offset, channel_multiplier, name)`

**Engine:** GpSimd Engine

**Signature:**
```python
isa.iota(dst, pattern, offset=0, channel_multiplier=0, name=None)
```

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
before writing to the output tile. The output data type is determined by the `dst` tile's
data type.

**Layout.**

The partition dimension determines the number of active channels for parallel pattern generation.

**Tile size.**

The partition dimension size of `dst` must not exceed 128. The number of
elements per partition of `dst` must not exceed the physical size of each SBUF partition.
The total number of elements in `pattern` must match the number of elements per partition in the `dst` tile.
`dst` can have at most four free dimensions (maximum rank 5, including the
partition dimension).

- **dst** — the output tile in SBUF to store the generated pattern
- **pattern** — a list of [step, num] to describe up to 4D tensor sizes and strides
- **offset** — an int32 offset value to be added to every generated value
- **channel_multiplier** — an int32 multiplier to be applied to the channel (parition) ID

---

### nki.isa.nc_n_gather {#nki-isa-nc_n_gather}

`nki.isa.nc_n_gather(dst, data, indices, name)`

**Engine:** GpSimd Engine

**Signature:**
```python
isa.nc_n_gather(dst, data, indices, name=None)
```

Gather elements from `data` according to `indices` using GpSimd Engine.

This instruction performs a gather operation where elements are selected from the input `data` tile
based on flattened indices specified in the `indices` tile. The free dimensions of `data` are
treated as if they were flattened into a single dimension for indexing purposes, while the partition
dimension defines the parallel compute boundary.

The gather operation works independently within each partition. For each partition, the free dimensions
of `data` are conceptually flattened, and elements are gathered according to the corresponding
flattened indices from the same partition in `indices`. If you need to gather elements across partitions
(within groups of partitions), consider using nisa.local_gather.

The `n` in `nc_n_gather` indicates that this instruction corresponds to `n` groups of instructions
in the underlying ISA, where `n = ceil(elems_per_partition / 512)`.

Alternatively, we could gather elements by calling nisa.dma_copy with an
indirect access pattern derived from `indices`. However, this is less efficient than `nc_n_gather`,
which uses GpSimd Engine to perform local data movement within SBUF, without using DMA engines.

**Memory types.**

All input and output tiles (`data`, `indices`, and `dst`) must be in SBUF.
GpSimd Engine cannot access PSUM (see [NeuronCore-v2 Compute Engines](../../architecture/trainium_inferentia2_arch.md#arch-sec-neuron-core-engines) for details).

**Data types.**

The input `data` tile can be any valid NKI data type (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).
The output `dst` tile must have the same data type as `data`.
The `indices` tile must be uint32.

**Layout.**

The partition dimension of `data`, `indices`, and `dst` must be the same.
Within each partition, the free dimensions of `data` are flattened for indexing.
The free dimensions of `indices` determine the shape of the output `dst`.

**Tile size.**

The partition dimension size of `data`, `indices`, and `dst` must be the same and must not exceed 128.
The number of elements per partition in `dst` must match the number of elements per partition in `indices`.
The indices' values must be within the range `[0, data.size / data.shape[0])`.
The `data` and `dst` tiles can have at most four free dimensions (maximum
rank 5, including the partition dimension).

- **dst** — output tile containing the gathered elements
- **data** — the input tile to gather elements from
- **indices** — the indices tile (uint32) specifying which elements to gather

---

