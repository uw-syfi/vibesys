# NKI ISA - Tensor Engine

> **Module**: nki.isa
> **Total Functions**: 21

## Overview

Tensor Engine instructions for matrix operations.

## Functions

### nki.isa.get_nc_version {#nki-isa-get_nc_version}

# nki.isa.get_nc_version

nki.isa.get_nc_version

nki.isa.get_nc_version()[[source]](../../../_modules/nki/isa.html#get_nc_version)
Returns the `nc_version` of the current target context.

---

### nki.isa.nc_find_index8 {#nki-isa-nc_find_index8}

# nki.isa.nc_find_index8

nki.isa.nc_find_index8

nki.isa.nc_find_index8(*dst*, *data*, *vals*, *name=None*)[[source]](../../../_modules/nki/isa.html#nc_find_index8)
Find indices of the 8 given vals in each partition of the data tensor.

This instruction first loads the 8 values,
then loads the data tensor and outputs the indices (starting at 0) of the first
occurrence of each value in the data tensor, for each partition.

The data tensor can be up to 5-dimensional, while the vals tensor must be up
to 3-dimensional. The data tensor must have between 8 and 16,384 elements per
partition. The vals tensor must have exactly 8 elements per partition.
The output will contain exactly 8 elements per partition and will be uint16 or
uint32 type. Default output type is uint32.

Behavior is undefined if vals tensor contains values that are not in
the data tensor.

If provided, a mask is applied only to the data tensor.

Parameters:

* **dst** – a 2D tile containing indices (uint16 or uint32) of the 8 values in each partition with shape [par_dim, 8]

* **data** – the data tensor to find indices from

* **vals** – tensor containing the 8 values per partition whose indices will be found

---

### nki.isa.nc_match_replace8 {#nki-isa-nc_match_replace8}

# nki.isa.nc_match_replace8

nki.isa.nc_match_replace8

nki.isa.nc_match_replace8(*dst*, *data*, *vals*, *imm*, *dst_idx=None*, *name=None*)[[source]](../../../_modules/nki/isa.html#nc_match_replace8)
Replace first occurrence of each value in `vals` with `imm` in `data`
using the Vector engine and return the replaced tensor. If `dst_idx`
tile is provided, the indices of the matched values are written to `dst_idx`.

This instruction reads the input `data`, replaces the first occurrence of each
of the given values (from `vals` tensor) with the specified immediate constant and,
optionally, output indices of matched values to `dst_idx`. When performing the operation,
the free dimensions of both `data` and `vals` are flattened. However, these dimensions
are preserved in the replaced output tensor and in `dst_idx` respectively. The partition
dimension defines the parallelization boundary. Match, replace, and index
generation operations execute independently within each partition.

The `data` tensor can be up to 5-dimensional, while the `vals` tensor can be up
to 3-dimensional. The `vals` tensor must have exactly 8 elements per partition.
The data tensor must have no more than 16,384 elements per partition.
The replaced output will have the same shape as the input data tensor. `data` and `vals`
must have the same number of partitions. Both input tensors can come from SBUF
or PSUM.

Behavior is undefined if vals tensor contains values that are not in the data
tensor.

If provided, a mask is applied to the data tensor.

**NumPy equivalent:**


```python
# Let's assume we work with NumPy, and ``data``, ``vals`` are 2-dimensional arrays
# (with shape[0] being the partition axis) and imm is a constant float32 value.

import numpy as np

# Get original shapes
data_shape = data.shape
vals_shape = vals.shape

# Reshape to 2D while preserving first dimension
data_2d = data.reshape(data_shape[0], -1)
vals_2d = vals.reshape(vals_shape[0], -1)

# Initialize output array for indices
indices = np.zeros(vals_2d.shape, dtype=np.uint32)

for i in range(data_2d.shape[0]):
  for j in range(vals_2d.shape[1]):
    val = vals_2d[i, j]
    # Find first occurrence of val in data_2d[i, :]
    matches = np.where(data_2d[i, :] == val)[0]
    if matches.size > 0:
      indices[i, j] = matches[0]  # Take first match
      data_2d[i, matches[0]] = imm

output = data_2d.reshape(data.shape)
indices = indices.reshape(vals.shape) # Computed only if ``dst_idx`` is specified
```


Parameters:

* **dst** – the modified data tensor

* **data** – the data tensor to modify

* **dst_idx** – (optional) the destination tile to write flattened indices of matched values

* **vals** – tensor containing the 8 values per partition to replace

* **imm** – float32 constant to replace matched values with

---

### nki.isa.nc_matmul {#nki-isa-nc_matmul}

# nki.isa.nc_matmul

nki.isa.nc_matmul

nki.isa.nc_matmul(*dst*, *stationary*, *moving*, *is_stationary_onezero=False*, *is_moving_onezero=False*, *is_transpose=False*, *tile_position=()*, *tile_size=()*, *perf_mode=matmul_perf_mode.none*, *accumulate=None*, *name=None*)[[source]](../../../_modules/nki/isa.html#nc_matmul)
Compute `dst = stationary.T &#64; moving` matrix multiplication using Tensor Engine.

The figure below illustrates how to map a matrix multiplication from a mathematical definition
to `nisa.nc_matmul` on Tensor Engine. For more detailed discussion of Tensor Engine capabilities, see
[Trainium arch guide](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/nki/arch/trainium_inferentia2_arch.html).


> **Figure: matmul**
>
> A comprehensive diagram comparing the mathematical view of matrix multiplication with the NeuronCore Tensor Engine implementation view, showing how matrices map to stationary (Tensor Engine), moving (SBUF), and output (PSUM) components.
>
> This diagram is divided into two parts by a vertical dashed line, illustrating how mathematical matrix multiplication maps to NeuronCore hardware.
>
> Part (a) "Mathematical View" (left side) shows standard matrix multiplication:
> - A blue matrix "y" at the top with dimensions N (width) by K (height)
> - A green matrix "x" at the bottom left with dimensions K (width) by M (height)
> - A purple matrix "output" at the bottom right with dimensions N (width) by M (height)
> - The matrices are arranged to show x * y = output multiplication
>
> Part (b) "Tensor Engine View" (right side) shows the hardware mapping:
> - A green matrix labeled "stationary (Tensor Engine)" with dimensions M (stationary_fsize) width by K height - this matrix is loaded into the Tensor Engine and held stationary
> - A blue matrix labeled "moving (SBUF)" with dimensions N (moving_fsize) width by K (rhs_psize) height - this matrix streams from the State Buffer
> - A purple matrix labeled "output (PSUM)" with dimensions N (moving_fsize) width by M (stationary_fsize) height - partial sums accumulate here
> - Arrows show the data flow: stationary and moving matrices feed into the computation, producing output in PSUM
> - A "Copy" arrow shows the PSUM output being copied to a final "output (SBUF)" tensor with dimensions N width by M height, stored in State Buffer
>
> Dimension annotations include:
> - M (stationary_fsize): Free dimension size of stationary matrix
> - N (moving_fsize): Free dimension size of moving matrix  
> - K (lhs_psize, rhs_psize): Contraction dimension
> - PSUM P-dim and SBUF P-dim labels indicate partition dimension orientations
>
> **Key Elements:**
> - **Mathematical View (a)**: Standard matrix multiplication x * y = output
> - **Tensor Engine View (b)**: Hardware-mapped implementation
> - **stationary (Tensor Engine)**: Green matrix held in Tensor Engine
> - **moving (SBUF)**: Blue matrix streamed from State Buffer
> - **output (PSUM)**: Purple partial sum accumulator
> - **output (SBUF)**: Final output copied to State Buffer
> - **M, N, K dimensions**: Matrix dimension labels
> - **Copy arrow**: Data movement from PSUM to SBUF


Fig. 95 MxKxN Matrix Multiplication Visualization.

**Performance mode.**

On NeuronCore-v2, performance mode is not supported.
On NeuronCore-v3 and NeuronCore-v4, Tensor Engine supports FP8 double performance mode, enabled by setting
performance mode to `double_row`.
See [Trainium2 arch guide](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/nki/arch/trainium2_arch.html)
for more details.
`double_row` performance mode cannot be combined with Tensor Engine column tiling mode (details below).

**Tiling mode.**
NeuronCore Tensor Engine is built upon a systolic array with 128 rows and 128 columns of processing elements (PEs).
Tensor Engine supports both row and column tiling modes, which allow multiple `nc_matmul` instructions with
a stationary tile size smaller than [128, 128] to run in parallel to improve hardware utilization.
Row tiling mode slices the 128 PE rows into 2x 64 row
tiles (NeuronCore-v2 or newer), or 4x 32 row tiles (NeuronCore-v3 or newer). Column tiling mode slices
the 128 PE columns in the same fashion. The row and column tile sizes can be set independently in the
`tile_size` field as a tuple `(row_size, column_size)`. The stationary tile size must not exceed the chosen
`tile_size`.

In addition, a given `nc_matmul` can also pick the exact row and column tile within the 128x128 systolic
array, by specifying the starting row and starting column in `tile_position` as a
tuple `(start_row, start_column)`. The `start_row` must be a multiple of `row_size` specified in `tile_size`
and must not exceed 128. Similarly, the `start_column` must be a multiple of `column_size` and must not exceed 128.

For example, setting `tile_position` to (64, 0) and `tile_size` to (64, 128) means using the bottom half
of the systolic array.

Note, `tile_position` and `tile_size` must both be set to enable tiling mode. If they are not set,
the default is to use the full systolic array, which is equivalent to `tile_position=(0, 0)`
and `tile_size=(128, 128)`. The values in `tile_position` and `tile_size` tuples can be
integers or affine expressions.

**Transpose mode.**

Tensor Engine can transpose a tile in SBUF by loading it as a stationary tile and using an identity matrix
as the moving tile.
Starting NeuronCore-v3, turning on transpose mode by setting `is_transpose=True` enables bit-accurate
data transpose, which can transpose tensors with NaN/Inf values properly.
See [Trainium2 arch guide](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/nki/arch/trainium2_arch.html)
for more details.

On NeuronCore-v2, Tensor Engine does not support transpose mode natively. However, setting `is_transpose=True`
ensures neuron-profile identifies this instruction as a transpose for performance metric accounting purposes.

**Memory types.**

The `nc_matmul` instruction *must* read inputs from SBUF and
write outputs to PSUM. Therefore, the `stationary` and `moving` must be SBUF tiles, and `dst` tile
must be a PSUM tile.

The `psum_accumulate_flag` controls whether the matmul result data should overwrite or accumulate on top of
the `dst` PSUM tile. Multiple `nisa.nc_matmul` instructions accumulating into the same PSUM tile
can form an accumulation group before the PSUM tile content is evicted back to SBUF. The encoding of
`psum_accumulate_flag` is as follows:

* bit[0] of `psum_accumulate_flag`: if set, indicates this `nisa.nc_matmul` call is the first instruction
in the accumulation group. The matmul result should overwrite the existing content in the `dst` PSUM tile.

* bit[1] of `psum_accumulate_flag`: if set, indicates this `nisa.nc_matmul` call is the last instruction
in the accumulation group. The matmul result should accumulate to the existing content in the `dst` PSUM tile.

* bit[2] of `psum_accumulate_flag`: if set, indicates this `nisa.nc_matmul` call is the first instruction
in the accumulation group. However, the matmul result should accumulate to the existing content in the `dst` PSUM tile.

`nisa.nc_matmul` calls that are not the first or last instruction of an accumulation group should not set any bit:
`psum_accumulate_flag=0`.

**Data types.**

The input `stationary` and `moving` tiles can be one of these supported data types:
`float8_e4m3/float8_e5m2/bfloat16/float16/tfloat32/float32`. The `stationary` and `moving` tiles
can have different data types, with one exception: if one of the input tiles is `tfloat32/float32`,
the other tile must also be `tfloat32/float32`.
On NeuronCore-v3 and NeuronCore-v4, when performance mode is `double_row`, `stationary` and `moving` tiles
must be one of `float8_e4m3` or `float8_e5m2`, but the two input tiles can have different float8 formats.

The accumulation precision internal to Tensor Engine is float32.
The `dst` tile must be a float32 tile in NeuronCore-v2 and NeuronCore-v3. Starting NeuronCore-v4,
`dst` can either be a float32 or bfloat16 tile.

**Layout.**

If performance mode is off, the contraction dimension of the matmul must be along the partition dimension in
both `stationary` and `moving` tiles.

If performance mode is `double_row`, the contraction dimension of the matmul is split between the partition dimension
and the first free dimension after the partition dimension in both `stationary` and `moving` tiles.
The first free dimension must be 2. For example, to perform a matmul of `[1, 256]&#64;[256, 3]=[1, 3]`, the stationary
tile is of shape `[128, 2, 1]`, while the moving tile is of shape `[128, 2, 3]`.

Regardless of performance mode, the free dimension of the `stationary` tile matches the partition
dimension of the output `dst` tile in size, while the free dimension of the `moving` tile
matches the free dimension of the `dst` tile in size.

**Tile size.**

The partition dimension sizes of the `stationary` and `moving` tiles must be identical. They must not
exceed 128 when tiling mode is off or `row_size` specified in `tile_size` when tiling mode is on.
The free dimension size of `stationary` must not exceed 128 when tiling mode is off or `column_size`
in `tile_size` when tiling mode is on.

On NeuronCore-v2 and -v3, the free dimension size of `moving` tile must not exceed 512, matching the maximum
number of float32 elements per PSUM bank. Starting NeuronCore-v4, the free dimension size of `moving` tile
can go up to 4096 for float32 `dst` or 8192 for bfloat16 `dst`, matching the size of 8x PSUM banks
(the entire PSUM).

Explicit tiling is required when the high-level matmul operation exceeds the tile size limits of `nc_matmul`.

Parameters:

* **dst** – the matmul output

* **stationary** – the stationary operand

* **moving** – the moving operand

* **is_stationary_onezero** – hints to the compiler whether the `stationary` operand is a tile with ones/zeros only;
setting this field explicitly could lead to 2x better performance
if `stationary` tile is in float32; the field has no impact for non-float32 `stationary`

* **is_moving_onezero** – hints to the compiler whether the `moving` operand is a tile with ones/zeros only;
setting this field explicitly could lead to 2x better performance
if `moving` tile is in float32; the field has no impact for non-float32 `moving`

* **is_transpose** – controls Tensor Engine transpose mode on/off starting NeuronCore-v3

* **tile_position** – a 2D tuple (start_row, start_column) to control starting row in Tensor Engine tiling mode; start_column must be 0

* **tile_size** – a 2D tuple (row_size, column_size) to control row tile size in Tensor Engine tiling mode; column_size must be 128

* **perf_mode** – controls Tensor Engine FP8 double performance mode on/off starting NeuronCore-v3: `matmul_perf_mode.none` (default) disables double FP8 mode; `matmul_perf_mode.double_row` enables double FP8 mode which achieves 2x matmul throughput by packing two FP8 weight/ifmap element pairs and computing two multiplications in parallel per cycle; cannot be combined with column tiling mode. See the [Trainium2 arch guide](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/nki/arch/trainium2_arch.html) for more information.

---

### nki.isa.nc_matmul_mx {#nki-isa-nc_matmul_mx}

# nki.isa.nc_matmul_mx

nki.isa.nc_matmul_mx

nki.isa.nc_matmul_mx(*dst*, *stationary*, *moving*, *stationary_scale*, *moving_scale*, *tile_position=None*, *tile_size=None*, *accumulate=None*, *name=None*)[[source]](../../../_modules/nki/isa.html#nc_matmul_mx)
Compute matrix multiplication of MXFP8/MXFP4 quantized matrices with integrated dequantization using Tensor Engine.

> **Note**
>
> Note
> 
> 
> Available only on NeuronCore-v4 and newer.

The NeuronCore-v4 Tensor Engine supports matrix multiplication of MXFP8/MXFP4 quantized matrices as defined in the
[OCP Microscaling standard](https://www.opencompute.org/documents/ocp-microscaling-formats-mx-v1-0-spec-final-pdf).
This instruction performs matrix multiplication between quantized `stationary` and `moving` matrices while
applying dequantization scales during computation. The micro-scaling group size is 32 elements in groups of 8 partitions x 4 elements per partition of both `stationary` and `moving` tensors.

See the NKI-specific [Trainium3 architecture guide](../../architecture/trainium3_arch.md) for more details.

**Tiling Mode.**

NeuronCore Tensor Engine is built upon a systolic array with 128 rows and 128 columns of processing elements (PEs).
For `nc_matmul_mx`, Tensor Engine supports only row tiling mode, which allows multiple `nc_matmul_mx` instructions with
a stationary partition dimension size smaller than 128 to run in parallel to improve hardware utilization.
Row tiling mode slices the 128 PE rows into 2x 64 row tiles or 4x 32 row tiles.

The row tile size can be set in the `tile_size` field as a tuple `(row_size, column_size)`,
where `column_size` must be 128.
The stationary tile size must not exceed the chosen `tile_size`.

A given `nc_matmul_mx` can pick the exact row tile within the 128x128 systolic array by specifying the starting row
in `tile_position` as a tuple `(start_row, start_column)`, where `start_column` must be 0.
The `start_row` must be a multiple of `row_size` specified in `tile_size` and must not exceed 128.

For example, setting `tile_position` to (64, 0) and `tile_size` to (64, 128) means using the bottom half
of the systolic array.

Note, `tile_position` and `tile_size` must both be set to enable tiling mode. If they are not set,
the default is to use the full systolic array, which is equivalent to `tile_position=(0, 0)`
and `tile_size=(128, 128)`. The values in `tile_position` and `tile_size` tuples can be
integers or affine expressions.

**Memory types.**

The `nc_matmul_mx` instruction must read inputs from SBUF and write outputs to PSUM. Therefore, the
`stationary`, `moving`, `stationary_scale`, and `moving_scale` must be SBUF tiles, and `dst`
tile must be a PSUM tile.

The `psum_accumulate_flag` controls whether the matmul result data should overwrite or accumulate on top of
the `dst` PSUM tile. Multiple `nisa.nc_matmul_mx` instructions accumulating into the same PSUM tile
can form an accumulation group before the PSUM tile content is evicted back to SBUF. The

* bit[0] of `psum_accumulate_flag`: if set, indicates this `nisa.nc_matmul_mx` call is the first instruction
in the accumulation group. The matmul result should overwrite the existing content in the `dst` PSUM tile.

* bit[1] of `psum_accumulate_flag`: if set, indicates this `nisa.nc_matmul_mx` call is the last instruction
in the accumulation group. The matmul result should accumulate to the existing content in the `dst` PSUM tile.

* bit[2] of `psum_accumulate_flag`: if set, indicates this `nisa.nc_matmul_mx` call is the first instruction
in the accumulation group. However, the matmul result should accumulate to the existing content in the `dst` PSUM tile.

`nisa.nc_matmul_mx` calls that are not the first or last instruction of an accumulation group should not set any bit:
`psum_accumulate_flag=0`.

**Data types.**

The input `stationary` and `moving` tiles must be float8_e5m2_x4, float8_e4m3fn_x4, or float4_e2m1fn_x4
(4-packed quantized data types). The `stationary_scale` and `moving_scale` tiles must be uint8.
The `dst` tile can be float32 or bfloat16.

The 4-packed data types (float8_e5m2_x4/float8_e4m3fn_x4/float4_e2m1fn_x4) pack multiple quantized values
into single elements. These packed data types are required because 4 microscaling quantized data values
share 1 scale value and must operate together as a compact group.

**Layout.**

The contraction dimension of the matrix multiplication is along the partition dimension of `stationary`
and `moving` tensors and also the x4 dimension within each packed data type element
(float8_e5m2_x4, float8_e4m3fn_x4, or float4_e2m1fn_x4).

The free dimension of the `stationary` tile matches the partition
dimension of the output `dst` tile in size, while the free dimension of the `moving` tile
matches the free dimension of the `dst` tile in size.

The scale tensors follow a special layout requirement. See more details in `nisa.quantize_mx` API doc.

*Tile size*

* The partition dimension size of `stationary` and `moving` must be identical and be a multiple of 32,
not exceeding 128.

* The free dimension size of `stationary` must be even and not exceed 128.

* The free dimension size of `moving` must not exceed 512 when `dst` is in float32 or 1024 when `dst` is in bfloat16.

* The scale tensors have partition dimensions that depend on whether the data tensors span multiple quadrants.
See more details in `nisa.quantize_mx` API doc.

Parameters:

* **dst** – the matrix multiplication output (PSUM tile)

* **stationary** – the stationary quantized matrix (SBUF tile)

* **moving** – the moving quantized matrix (SBUF tile)

* **stationary_scale** – the dequantization scales for stationary matrix (SBUF tile)

* **moving_scale** – the dequantization scales for moving matrix (SBUF tile)

* **tile_position** – a 2D tuple (start_row, start_column) to control starting row and column in Tensor Engine tiling mode

* **tile_size** – a 2D tuple (row_size, column_size) to control row and column tile sizes in Tensor Engine tiling mode

* **psum_accumulate_flag** – controls PSUM near-memory accumulation in the `dst` tile

---

### nki.isa.nc_n_gather {#nki-isa-nc_n_gather}

# nki.isa.nc_n_gather

nki.isa.nc_n_gather

nki.isa.nc_n_gather(*dst*, *data*, *indices*, *name=None*)[[source]](../../../_modules/nki/isa.html#nc_n_gather)
Gather elements from `data` according to `indices` using GpSimd Engine.

This instruction performs a gather operation where elements are selected from the input `data` tile
based on flattened indices specified in the `indices` tile. The free dimensions of `data` are
treated as if they were flattened into a single dimension for indexing purposes, while the partition
dimension defines the parallel compute boundary.

The gather operation works independently within each partition. For each partition, the free dimensions
of `data` are conceptually flattened, and elements are gathered according to the corresponding
flattened indices from the same partition in `indices`.

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
The indices values must be within the valid range [0, data.size / data.shape[0]).

Parameters:

* **dst** – output tile containing the gathered elements

* **data** – the input tile to gather elements from

* **indices** – the indices tile (uint32) specifying which elements to gather

---

### nki.isa.nc_stream_shuffle {#nki-isa-nc_stream_shuffle}

# nki.isa.nc_stream_shuffle

nki.isa.nc_stream_shuffle

nki.isa.nc_stream_shuffle(*dst*, *src*, *shuffle_mask*, *name=None*)[[source]](../../../_modules/nki/isa.html#nc_stream_shuffle)
Apply cross-partition data movement within a quadrant of 32 partitions from source tile
`src` to destination tile `dst` using Vector Engine.

Both source and destination tiles can be in either SBUF or PSUM, and passed in by reference as arguments.
In-place shuffle is allowed, i.e., `dst` same as `src`. `shuffle_mask` is a 32-element list. Each mask
element must be in data type int or affine expression. `shuffle_mask[i]` indicates which input partition the
output partition [i] copies from within each 32-partition quadrant. The special value `shuffle_mask[i]=255`
means the output tensor in partition [i] will be unmodified. `nc_stream_shuffle` can be applied to multiple
of quadrants. In the case with more than one quadrant, the shuffle is applied to each quadrant independently,
and the same `shuffle_mask` is used for each quadrant. For more information about the cross-partition data movement,
see [Cross-partition Data Movement](../../architecture/trainium_inferentia2_arch.md#arch-guide-cross-partition-data-movement).

This API has 3 constraints on `src` and `dst`:

* `dst` must have same data type as `src`.

* `dst` must have the same number of elements per partition as `src`.

* The access start partition of `src` (`src_start_partition`), does not have to match or be in the same quadrant
as that of `dst` (`dst_start_partition`). However, `src_start_partition`/`dst_start_partition` needs to follow
some special hardware rules with the number of active partitions `num_active_partitions`.
`num_active_partitions = ceil(max(src_num_partitions, dst_num_partitions)/32) * 32`, where `src_num_partitions` and
`dst_num_partitions` refer to the number of partitions the `src` and `dst` tensors access respectively.
`src_start_partition`/`dst_start_partition` is constrained based on the value of `num_active_partitions`:

* If `num_active_partitions` is 96/128, `src_start_partition`/`dst_start_partition` must be 0.

* If `num_active_partitions` is 64, `src_start_partition`/`dst_start_partition` must be 0/64.

* If `num_active_partitions` is 32, `src_start_partition`/`dst_start_partition` must be 0/32/64/96.

Parameters:

* **dst** – the destination tile

* **src** – the source tile

* **shuffle_mask** – a 32-element list that specifies the shuffle source and destination partition

---

### nki.isa.nc_transpose {#nki-isa-nc_transpose}

# nki.isa.nc_transpose

nki.isa.nc_transpose

nki.isa.nc_transpose(*dst*, *data*, *engine=engine.unknown*, *name=None*)[[source]](../../../_modules/nki/isa.html#nc_transpose)
Perform a 2D transpose between the partition axis and the free axis of input `data` using Tensor or Vector Engine.

If the `data` tile has more than one free axis, this API implicitly flattens all free axes into one axis
and then performs a 2D transpose.

2D transpose on Tensor Engine is implemented by performing a matrix multiplication between `data` as the
stationary tensor and an identity matrix as the moving tensor. This is equivalent to calling `nisa.nc_matmul`
directly with `is_transpose=True`. See [architecture guide](../../architecture/trainium_inferentia2_arch.md#arch-sec-tensor-engine-alternative-use)
for more information. On NeuronCore-v2, Tensor Engine transpose is not bit-accurate if the input `data`
contains NaN/Inf.
You may consider replacing NaN/Inf with regular floats (float_max/float_min/zeros) in the input matrix.
Starting NeuronCore-v3, all Tensor Engine transpose is bit-accurate.

**Memory types.**

Tensor Engine `nc_transpose` must read the input tile from SBUF and write the transposed result to PSUM.
Vector Engine `nc_transpose` can read/write from/to either SBUF or PSUM.

**Data types.**

The input `data` tile can be any valid NKI data type (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).
The output `dst` tile must have the same data type as that of `data`.

**Layout.**
The partition dimension of `data` tile becomes the free dimension of the `dst` tile.
Similarly, the free dimension of the `data` tile becomes the partition dimension of the `dst` tile.

**Tile size.**
Tensor Engine `nc_transpose` can handle an input tile of shape [128, 128] or smaller, while Vector
Engine can handle shape [32, 32] or smaller.
If no `engine` is specified, Neuron Compiler will automatically select an engine
based on the input shape.

Parameters:

* **dst** – the transpose output

* **data** – the input tile to be transposed

* **engine** – specify which engine to use for transpose: `nki.isa.tensor_engine` or `nki.isa.vector_engine`;
by default, the best engine will be selected for the given input tile shape

---

### nki.isa.nc_version {#nki-isa-nc_version}

# nki.isa.nc_version

nki.isa.nc_version

*class *nki.isa.nc_version(*value*)[[source]](../../../_modules/nki/isa.html#nc_version)
NeuronCore version

__init__()

Attributes


| gen2 | Trn1/Inf2 target |
| --- | --- |
| gen3 | Trn2 target |
| gen4 | Trn3 target |

---

### nki.isa.scalar_tensor_tensor {#nki-isa-scalar_tensor_tensor}

# nki.isa.scalar_tensor_tensor

nki.isa.scalar_tensor_tensor

nki.isa.scalar_tensor_tensor(*dst*, *data*, *op0*, *operand0*, *op1*, *operand1*, *reverse0=False*, *reverse1=False*, *name=None*)[[source]](../../../_modules/nki/isa.html#scalar_tensor_tensor)
Apply two math operators in sequence using Vector Engine: `(data <op0> operand0) <op1> operand1`.

This instruction is equivalent to running two operations back-to-back:
1. `temp_result = tensor_scalar(data, op0, operand0)` - broadcast `operand0` and apply `op0`
2. `dst = tensor_tensor(temp_result, op1, operand1)` - element-wise operation with `operand1`

The `operand0` can be either a compile-time
constant scalar for broadcast across all elements of `data` or
a tile of shape `(data.shape[0], 1)` for broadcast along the free dimension.
The `operand1` tile must have the same shape as `data` for element-wise operation.

The scalar broadcasting in the first operation is performed at no additional performance cost,
making this instruction have approximately the same latency as a regular `tensor_tensor` instruction.

Both `op0` and `op1` must be arithmetic operators (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators).
Bitvec operators are not supported. When the operators are non-commutative (e.g., subtract),
operand ordering can be reversed using `reverse0` and `reverse1` flags.

**Memory types.**

The input `data` tile can be an SBUF or PSUM tile. The `operand0` can be an SBUF or PSUM tile
or a compile-time constant scalar. The `operand1` must be an SBUF or PSUM tile.
However, `data` and `operand1` cannot both reside in PSUM. The output `dst` tile can be
written to either SBUF or PSUM.

**Data types.**

All input tiles can be any supported NKI data type (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).
The Vector Engine automatically casts input data types to float32 and performs all computations
in float32 math. The float32 results are cast to the data type of output `dst`.

**Layout.**

The parallel computation dimension of `nisa.scalar_tensor_tensor` is along the partition dimension.

**Tile size.**

The partition dimension size of input `data`, `operand1`, and output `dst` tiles must be
the same and must not exceed 128. The total number of elements per partition of input `data`, `operand1`,
and output `dst` tiles must be the same and must not exceed the
physical size of each SBUF partition.
If operand0 is not a scalar, the partition dimension size of `operand0` must be the same as that of `data`
and the number of elements per partition of `operand0` must be 1.

Parameters:

* **dst** – the output tile

* **data** – the input tile

* **op0** – the first math operator used with operand0 (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators)

* **operand0** – a scalar constant or a tile of shape `(data.shape[0], 1)`, where data.shape[0]
is the partition axis size of the input `data` tile

* **reverse0** – reverse ordering of inputs to `op0`; if false, `operand0` is the rhs of `op0`;
if true, `operand0` is the lhs of `op0`

* **op1** – the second math operator used with operand1 (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators)

* **operand1** – a tile with the same size as `data` for element-wise operation

* **reverse1** – reverse ordering of inputs to `op1`; if false, `operand1` is the rhs of `op1`;
if true, `operand1` is the lhs of `op1`

---

### nki.isa.tensor_copy {#nki-isa-tensor_copy}

# nki.isa.tensor_copy

nki.isa.tensor_copy

nki.isa.tensor_copy(*dst*, *src*, *engine=engine.unknown*, *name=None*)[[source]](../../../_modules/nki/isa.html#tensor_copy)
Create a copy of `src` tile within NeuronCore on-chip SRAMs using Vector, Scalar or GpSimd Engine.

The output tile has the same partition axis size and also the same number of elements per partition
as the input tile `src`.

All three compute engines, Vector, Scalar and GpSimd Engine can perform tensor copy. However, their copy behavior
is slightly different across engines:

* Scalar Engine on NeuronCore-v2 performs copy by first casting the input tile to FP32 internally and then casting from
FP32 to `dst.dtype`. Users should be cautious with assigning this instruction to Scalar Engine when the input data
type cannot be precisely cast to FP32 (e.g., INT32).

* Both GpSimd and Vector Engine can operate in two modes: (1) bit-accurate copy when input and output data types are
the same or (2) intermediate FP32 cast when input and output data types differ, similar to Scalar Engine.

In addition, since GpSimd Engine cannot access PSUM in NeuronCore, Scalar or Vector Engine must be chosen when the input or
output tile is in PSUM (see [NeuronCore-v2 Compute Engines](../../architecture/trainium_inferentia2_arch.md#arch-sec-neuron-core-engines) for details). By default, this API returns
a tile in SBUF, unless the returned value is assigned to a pre-declared PSUM tile.

Parameters:

* **dst** – a tile with the same content and partition axis size as the `src` tile.

* **src** – the source of copy, must be a tile in SBUF or PSUM.

* **engine** – (optional) the engine to use for the operation: nki.isa.vector_engine, nki.isa.scalar_engine,
nki.isa.gpsimd_engine or nki.isa.unknown_engine (default, compiler selects best engine based on engine workload).

---

### nki.isa.tensor_copy_dynamic_dst {#nki-isa-tensor_copy_dynamic_dst}

# nki.isa.tensor_copy_dynamic_dst

nki.isa.tensor_copy_dynamic_dst

nki.isa.tensor_copy_dynamic_dst(*dst*, *src*, *engine=engine.unknown*, *name=None*)[[source]](../../../_modules/nki/isa.html#tensor_copy_dynamic_dst)
Create a copy of `src` tile within NeuronCore on-chip SRAMs using Vector or Scalar or GpSimd Engine,
with `dst` located at a dynamic offset within each partition.

Both source and destination tiles can be in either SBUF or PSUM.

The source and destination tiles must also have the same number of partitions and the same number of elements
per partition.

The dynamic offset must be a scalar value resided in SBUF. If you have a list of dynamic offsets
for scattering tiles in SBUF/PSUM, you may loop over each offset and call `tensor_copy_dynamic_dst`
once per offset.

Parameters:

* **dst** – the destination of copy, must be a tile in SBUF of PSUM that is dynamically indexed within each dimension.

* **src** – the source of copy, must be a tile in SBUF or PSUM.

* **engine** – (optional) the engine to use for the operation: nki.isa.vector_engine, nki.isa.gpsimd_engine,
nki.isa.scalar_engine or nki.isa.unknown_engine (default, let compiler select best engine).

---

### nki.isa.tensor_copy_dynamic_src {#nki-isa-tensor_copy_dynamic_src}

# nki.isa.tensor_copy_dynamic_src

nki.isa.tensor_copy_dynamic_src

nki.isa.tensor_copy_dynamic_src(*dst*, *src*, *engine=engine.unknown*, *name=None*)[[source]](../../../_modules/nki/isa.html#tensor_copy_dynamic_src)
Create a copy of `src` tile within NeuronCore on-chip SRAMs using Vector or Scalar or GpSimd Engine,
with `src` located at a dynamic offset within each partition.

Both source and destination tiles can be in either SBUF or PSUM. By default, this API returns
a tile in SBUF, unless the returned value is assigned to a pre-declared PSUM tile.

The source and destination tiles must also have the same number of partitions and the same number of elements
per partition.

The dynamic offset must be a scalar value resided in SBUF. If you have a list of dynamic offsets
for gathering tiles in SBUF/PSUM, you may loop over each offset and call `tensor_copy_dynamic_src`
once per offset.

Parameters:

* **src** – the source of copy, must be a tile in SBUF or PSUM that is dynamically indexed within each partition.

* **engine** – (optional) the engine to use for the operation: nki.isa.vector_engine, nki.isa.gpsimd_engine,
nki.isa.scalar_engine or nki.isa.unknown_engine (default, let compiler select best engine).

* **return** – the modified destination of copy.

---

### nki.isa.tensor_copy_predicated {#nki-isa-tensor_copy_predicated}

# nki.isa.tensor_copy_predicated

nki.isa.tensor_copy_predicated

nki.isa.tensor_copy_predicated(*dst*, *src*, *predicate*, *reverse_pred=False*, *name=None*)[[source]](../../../_modules/nki/isa.html#tensor_copy_predicated)
Conditionally copy elements from the `src` tile to the destination tile on SBUF / PSUM
based on a `predicate` using Vector Engine.

This instruction provides low-level control over conditional data movement on NeuronCores,
optimized for scenarios where only selective copying of elements is needed. Either `src` or
`predicate` may be in PSUM, but not both simultaneously. Both `src` and `predicate` are permitted to be in SBUF.

Shape and data type constraints:

* `src` (if it is a tensor), `dst`, and `predicate` must occupy the same number of partitions and same number of elements per partition.

* `predicate` must be of type `uint8`, `uint16`, or `uint32`.

* `src` and `dst` must share the same data type.

**Behavior:**

* Where predicate is True: The corresponding elements from src are copied to dst tile. If src is a scalar, the scalar is copied to the dst tile.

* Where predicate is False: The corresponding values in dst tile are unmodified

Parameters:

* **src** – The source tile or number to copy elements from when `predicate` is True

* **dst** – The destination tile to copy elements to

* **predicate** – A tile that determines which elements to copy

* **reverse_pred** – A boolean that reverses the effect of `predicate`.

---

### nki.isa.tensor_partition_reduce {#nki-isa-tensor_partition_reduce}

# nki.isa.tensor_partition_reduce

nki.isa.tensor_partition_reduce

nki.isa.tensor_partition_reduce(*dst*, *op*, *data*, *name=None*)[[source]](../../../_modules/nki/isa.html#tensor_partition_reduce)
Apply a reduction operation across partitions of an input `data` tile using GpSimd Engine.

Parameters:

* **dst** – output tile with reduced result

* **op** – the reduction operator (add, max, bitwise_or, bitwise_and)

* **data** – the input tile to be reduced

---

### nki.isa.tensor_reduce {#nki-isa-tensor_reduce}

# nki.isa.tensor_reduce

nki.isa.tensor_reduce

nki.isa.tensor_reduce(*dst*, *op*, *data*, *axis*, *negate=False*, *keepdims=False*, *name=None*)[[source]](../../../_modules/nki/isa.html#tensor_reduce)
Apply a reduction operation to the free axes of an input `data` tile using Vector Engine.

The reduction operator is specified in the `op` input field
(see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for a list of supported reduction operators).
`nisa.tensor_reduce` supports two types of reduction operators: 1) bitvec operators (e.g., bitwise_and, bitwise_or)
and 2) arithmetic operators (e.g., add, subtract, multiply).

The reduction axes are specified in the `axis` field using a list of integer(s) to indicate axis indices.
The reduction axes can contain up to four free axes and must start at the most minor free axis.
Since axis 0 is the partition axis in a tile, the reduction axes must contain axis 1 (most-minor). In addition,
the reduction axes must be consecutive: e.g., [1, 2, 3, 4] is a legal `axis` field, but [1, 3, 4] is not.

When the reduction `op` is an arithmetic operator, the instruction can also multiply the output reduction
results by `-1.0` before writing into the output tile, at no additional performance cost. This behavior is
controlled by the `negate` input field.

**Memory types.**

Both the input `data` and `dst` tiles can be in SBUF or PSUM.

**Data types.**

For bitvec operators, the input/output data types must be integer types and Vector Engine treats
all input elements as bit patterns without any data type casting. For arithmetic operators,
the input/output data types can be any supported NKI data types, but the engine automatically casts
input data types to float32
and performs the reduction operation in float32 math. The float32 reduction results are cast to the
data type of `dst`.

**Layout.**

`nisa.tensor_reduce` only supports free axes reduction. Therefore, the partition dimension of the input
`data` is considered the parallel compute dimension. To perform a partition axis reduction, we can either:

* invoke a `nisa.nc_transpose` instruction on the input tile and then this `nisa.tensor_reduce`
on the transposed tile, or

* invoke `nki.isa.nc_matmul` instructions to multiply a `nl.ones([128, 1], dtype=data.dtype)` as a stationary
tensor with the input tile as a moving tensor. See more discussion on Tensor Engine alternative usage in
[Trainium architecture guide](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/nki/arch/trainium2_arch.html).

**Tile size.**

The partition dimension size of input `data` and output `dst` tiles must be the same and must not exceed 128.
The number of elements per partition of `data` must not
exceed the physical size of each SBUF partition. The number of elements per partition in `dst` must be consistent
with the `axis` field. For example, if `axis` indicates all free dimensions of `data` are reduced,
the number of elements per partition in `dst` must be 1.

Parameters:

* **dst** – output tile of the reduction result

* **op** – the reduction operator (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported reduction operators)

* **data** – the input tile to be reduced

* **axis** – int or tuple/list of ints. The axis (or axes) along which to operate; must be free dimensions, not partition dimension (0); can only be the last contiguous dim(s) of the tile: `[1], [1,2], [1,2,3], [1,2,3,4]`

* **negate** – if True, reduction result is multiplied by `-1.0`;
only applicable when op is an arithmetic operator

* **keepdims** – If this is set to True, the axes which are reduced are left in the result as dimensions with size one.
With this option, the result will broadcast correctly against the input array.

---

### nki.isa.tensor_scalar {#nki-isa-tensor_scalar}

# nki.isa.tensor_scalar

nki.isa.tensor_scalar

nki.isa.tensor_scalar(*dst*, *data*, *op0*, *operand0*, *reverse0=False*, *op1=None*, *operand1=None*, *reverse1=False*, *engine=engine.unknown*, *name=None*)[[source]](../../../_modules/nki/isa.html#tensor_scalar)
Apply up to two math operators to the input `data` tile by broadcasting scalar/vector operands
in the free dimension using Vector or Scalar or GpSimd Engine: `(data <op0> operand0) <op1> operand1`.

The input `data` tile can be an SBUF or PSUM tile. Both `operand0` and `operand1` can be
SBUF or PSUM tiles of shape `(data.shape[0], 1)`, i.e., vectors,
or compile-time constant scalars.

`op1` and `operand1` are optional, but must be `None` (default values) when unused.
Note, performing one operator has the same performance cost as performing two operators in the instruction.

When the operators are non-commutative (e.g., subtract), we can reverse ordering of the inputs for each operator through:

* `reverse0 = True`: `tmp_res = operand0 <op0> data`

* `reverse1 = True`: `operand1 <op1> tmp_res`

The `tensor_scalar` instruction supports two types of operators: 1) bitvec
operators (e.g., bitwise_and) and 2) arithmetic operators (e.g., add).
See [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for the full list of supported operators.
The two operators, `op0` and `op1`, in a `tensor_scalar` instruction must be of the same type
(both bitvec or both arithmetic).
If bitvec operators are used, the `tensor_scalar` instruction must run on Vector Engine. Also, the input/output
data types must be integer types, and input elements are treated as bit patterns without any data type casting.

If arithmetic operators are used, the `tensor_scalar` instruction can run on Vector or Scalar or GpSimd Engine.
However, each engine supports limited arithmetic operators (see :ref:`tbl-aluop`). The Scalar Engine on trn2 only
supports some operator combinations:

* `op0=nl.multiply` and `op1=nl.add`

* `op0=nl.multiply` and `op1=None`

* `op0=nl.add` and `op1=None`

Also, arithmetic operators impose no restriction on the input/output data types,
but the engine automatically casts input data types to float32
and performs the operators in float32 math. The float32 computation results are
cast to `dst.dtype` at no additional performance cost.

Parameters:

* **dst** – an output tile of `(data <op0> operand0) <op1> operand1` computation

* **data** – the input tile

* **op0** – the first math operator used with operand0 (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators)

* **operand0** – a scalar constant or a tile of shape `(data.shape[0], 1)`, where data.shape[0]
is the partition axis size of the input `data` tile

* **reverse0** – reverse ordering of inputs to `op0`; if false, `operand0` is the rhs of `op0`;
if true, `operand0` is the lhs of `op0`

* **op1** – the second math operator used with operand1 (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators);
this operator is optional

* **operand1** – a scalar constant or a tile of shape `(data.shape[0], 1)`, where data.shape[0]
is the partition axis size of the input `data` tile

* **reverse1** – reverse ordering of inputs to `op1`; if false, `operand1` is the rhs of `op1`;
if true, `operand1` is the lhs of `op1`

* **engine** – (optional) the engine to use for the operation: nki.isa.vector_engine, nki.isa.scalar_engine,
nki.isa.gpsimd_engine (only allowed for rsqrt) or nki.isa.unknown_engine (default, let
compiler select best engine based on the input tile shape).

---

### nki.isa.tensor_scalar_cumulative {#nki-isa-tensor_scalar_cumulative}

# nki.isa.tensor_scalar_cumulative

nki.isa.tensor_scalar_cumulative

nki.isa.tensor_scalar_cumulative(*dst*, *src*, *op0*, *op1*, *imm0*, *imm1=None*, *reduce_cmd=reduce_cmd.reset_reduce*)[[source]](../../../_modules/nki/isa.html#tensor_scalar_cumulative)
Perform tensor-scalar arithmetic operation with cumulative reduction using Vector Engine.

The operation applies a scalar operation to each tensor element, then performs a cumulative
reduction, storing the cumulative results in the destination tensor.

The operation can be expressed in pseudocode as:


```python

```


if reduce_cmd == reset_reduce:
if op1 == add or op1 == subtract:reg = 0

elif op1 == mult:reg = 1

elif op1 == max:reg = -inf

elif op1 == min:reg = +inf

elif reduce_cmd == reduce:reg = reg

elif reduce_cmd == load_reduce:reg = imm1

for i in len(in_tensor):
if not reverse0:reg = op1(op0(in_tensor[i], imm0), reg)
out_tensor[i] = reg

else:reg = op1(op0(imm0, in_tensor[i]), reg)
out_tensor[i] = reg

**Operation constraints:**

* Scalar operation (`op0`) must be an arithmetic op (e.g., add, mult, max)

* Reduction operation (`op1`) is limited to add, subtract, mult, max, min

* 
Input / output dtypes are restricted to BF16, FP16, FP32, FP8, UINT8, UINT16, INT8, INT16

INT32/UINT32 are not supported as input/output dtypes (ISA limitation)

**Accumulator behavior:**

The Vector Engine maintains internal accumulator registers controlled via `reduce_cmd`:

* `reset_reduce`: Reset accumulator based on reduction operation type

* `load_reduce`: Initialize accumulator with `imm1` value

* `reduce`: Continue with existing accumulator value

Parameters:

* **dst** – The destination tensor to write cumulative results to

* **src** – The source tensor to process

* **op0** – Scalar arithmetic operation to apply to each element

* **op1** – Cumulative arithmetic operation for cumulative computation

* **imm0** – Scalar or vector value for tensor-scalar operation. Must be FP32 datatype

* **imm1** – (optional) Initial scalar or vector value for the accumulator when `load_reduce`
is specified as the `reduce_cmd`. Must be FP32 datatype

* **reduce_cmd** – (optional) Control accumulator behavior using `nisa.reduce_cmd` values,
defaults to `reset_reduce`

---

### nki.isa.tensor_scalar_reduce {#nki-isa-tensor_scalar_reduce}

# nki.isa.tensor_scalar_reduce

nki.isa.tensor_scalar_reduce

nki.isa.tensor_scalar_reduce(*dst*, *data*, *op0*, *operand0*, *reduce_op*, *reduce_res*, *reverse0=False*, *name=None*)[[source]](../../../_modules/nki/isa.html#tensor_scalar_reduce)
Perform the same computation as `nisa.tensor_scalar` with one math operator
and also a reduction along the free dimension of the `nisa.tensor_scalar` result using Vector Engine.

Refer to [nisa.tensor_scalar](nki.isa.tensor_scalar.md) for semantics of `data/op0/operand0`.
Unlike regular `nisa.tensor_scalar` where two operators are supported, only one
operator is supported in this API. Also, `op0` can only be arithmetic operation in [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop).
Bitvec operators are not supported in this API.

In addition to [nisa.tensor_scalar](nki.isa.activation.md) computation, this API also performs a reduction
along the free dimension(s) of the [nisa.tensor_scalar](nki.isa.activation.md) result, at a small additional
performance cost. The reduction result is returned in `reduce_res` in-place, which must be a
SBUF/PSUM tile with the same partition axis size as the input tile `data` and one element per partition.
The `reduce_op` can be any of `nl.add`, `nl.subtract`, `nl.multiply`, `nl.max` or `nl.min`.

Reduction axis is not configurable in this API. If the input tile has multiple free axis, the API will
reduce across all of them.

\[\begin{split}result = data <op0> operand0 \\
reduce\_res = reduce\_op(dst, axis=<FreeAxis>)\end{split}\]

Parameters:

* **dst** – an output tile of `(data <op0> operand0)` computation

* **data** – the input tile

* **op0** – the math operator used with operand0 (any arithmetic operator in [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) is allowed)

* **operand0** – a scalar constant or a tile of shape `(data.shape[0], 1)`, where data.shape[0]
is the partition axis size of the input `data` tile

* **reverse0** – (not supported yet) reverse ordering of inputs to `op0`; if false, `operand0` is the rhs of `op0`;
if true, `operand0` is the lhs of `op0`. <– currently not supported yet.

* **reduce_op** – the reduce operation to perform on the free dimension of `data <op0> operand0`

* **reduce_res** – a tile of shape `(data.shape[0], 1)`, where data.shape[0]
is the partition axis size of the input `data` tile. The result of `reduce_op(data <op0> operand0)`
is written in-place into the tile.

---

### nki.isa.tensor_tensor {#nki-isa-tensor_tensor}

# nki.isa.tensor_tensor

nki.isa.tensor_tensor

nki.isa.tensor_tensor(*dst*, *data1*, *data2*, *op*, *engine=engine.unknown*, *name=None*)[[source]](../../../_modules/nki/isa.html#tensor_tensor)
Perform an element-wise operation of input two tiles using Vector Engine or GpSimd Engine.
The two tiles must have the same partition axis size and the same number of elements per partition.

The element-wise operator is specified using the `op` field. Valid choices for `op`:

* Any supported *binary* operator that runs on the Vector Engine. (See [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for details.)

* `nl.power`. (Which runs on the GpSimd engine.)

For bitvec operators, the input/output data types must be integer types and Vector Engine treats
all input elements as bit patterns without any data type casting. For arithmetic operators, there is no
restriction on the input/output data types, but the engine automatically casts input data types to float32
and performs the element-wise operation in float32 math. The float32 computation results are cast to
`dst.dtype` at no additional performance cost.

Since GpSimd Engine cannot access PSUM, the input/output tiles cannot be in PSUM if `op` is `nl.power`.
(See [NeuronCore-v2 Compute Engines](../../architecture/trainium_inferentia2_arch.md#arch-sec-neuron-core-engines) for details.)

Otherwise, the output tile can be in either SBUF or PSUM.
However, the two input tiles, `data1` and `data2` cannot both reside in PSUM.
The three legal cases are:

* Both `data1` and `data2` are in SBUF.

* `data1` is in SBUF, while `data2` is in PSUM.

* `data1` is in PSUM, while `data2` is in SBUF.

Note, if you need broadcasting capability in the free dimension for either input tile, you should consider
using [nki.isa.tensor_scalar](nki.isa.tensor_scalar.md) API instead,
which has better performance than `nki.isa.tensor_tensor` in general.

Parameters:

* **dst** – an output tile of the element-wise operation

* **data1** – lhs input operand of the element-wise operation

* **data2** – rhs input operand of the element-wise operation

* **op** – a binary math operator (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators)

* **engine** – (optional) the engine to use for the operation: nki.isa.vector_engine, nki.isa.gpsimd_engine
or nki.isa.unknown_engine (default, let compiler select best engine based on the input tile shape).

---

### nki.isa.tensor_tensor_scan {#nki-isa-tensor_tensor_scan}

# nki.isa.tensor_tensor_scan

nki.isa.tensor_tensor_scan

nki.isa.tensor_tensor_scan(*dst*, *data0*, *data1*, *initial*, *op0*, *op1*, *reverse0=False*, *reverse1=False*, *name=None*)[[source]](../../../_modules/nki/isa.html#tensor_tensor_scan)
Perform a scan operation of two input tiles using Vector Engine.

Mathematically, the tensor_tensor_scan instruction on Vector Engine performs
the following computation per partition:


```python
# Let's assume we work with numpy, and data0 and data1 are 2D (with shape[0] being the partition axis)
import numpy as np

result = np.ndarray(data0.shape, dtype=data0.dtype)
result[:, 0] = op1(op0(data0[:. 0], initial), data1[:, 0])

for i in range(1, data0.shape[1]):
    result[:, i] = op1(op0(data0[:, i], result[:, i-1]), data1[:, i])
```


The two input tiles (`data0` and `data1`) must have the same
partition axis size and the same number of elements per partition.
The third input `initial` can either be a float32 compile-time scalar constant
that will be broadcasted in the partition axis of `data0`/`data1`, or a tile
with the same partition axis size as `data0`/`data1` and one element per partition.

The two input tiles, `data0` and `data1` cannot both reside in PSUM. The three legal cases are:

* Both `data1` and `data2` are in SBUF.

* `data1` is in SBUF, while `data2` is in PSUM.

* `data1` is in PSUM, while `data2` is in SBUF.

The scan operation supported by this API has two programmable
math operators in `op0` and `op1` fields.
Both `op0` and `op1` can be any binary arithmetic operator
supported by NKI (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for details).
We can optionally reverse the input operands of `op0` by setting `reverse0` to True
(or `op1` by setting `reverse1`). Reversing operands is useful for non-commutative
operators, such as subtract.

Input/output data types can be any supported NKI data type (see [Supported Data Types](nki.api.shared.md#nki-dtype)),
but the engine automatically casts input data types to float32
and performs the computation in float32 math. The float32 computation results are
cast to `dst.dtype` at no additional performance cost.

Parameters:

* **dst** – an output tile of the scan operation

* **data0** – lhs input operand of the scan operation

* **data1** – rhs input operand of the scan operation

* **initial** – starting state of the scan; can be a SBUF/PSUM tile with 1 element/partition or a scalar
compile-time constant

* **op0** – a binary arithmetic math operator (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators)

* **op1** – a binary arithmetic math operator (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators)

* **reverse0** – reverse ordering of inputs to `op0`; if false, `data0` is the lhs of `op0`;
if true, `data0` is the rhs of `op0`

* **reverse1** – reverse ordering of inputs to `op1`; if false, `data1` is the rhs of `op1`;
if true, `data1` is the lhs of `op1`

---
