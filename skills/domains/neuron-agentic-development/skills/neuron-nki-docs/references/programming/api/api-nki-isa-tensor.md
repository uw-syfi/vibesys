# nki.isa — Tensor Engine

> **Module**: nki.isa
> **Functions**: 17

## Overview

Tensor Engine instructions.

## Functions

### nki.isa.get_nc_version {#nki-isa-get_nc_version}

`nki.isa.get_nc_version()`

**Signature:**
```python
isa.get_nc_version()
```

Returns the nc_version of the current target context.

---

### nki.isa.nc_find_index8 {#nki-isa-nc_find_index8}

`nki.isa.nc_find_index8(dst, data, vals, name)`

**Signature:**
```python
isa.nc_find_index8(dst, data, vals, name=None)
```

Find indices of the 8 given vals in each partition of the data tensor.

This instruction first loads the 8 values,
then loads the data tensor and outputs the indices (starting at 0) of the first
occurrence of each value in the data tensor, for each partition.

The `data` tensor can have at most four free dimensions (maximum rank 5),
while `dst` and `vals` can have at most two free dimensions (maximum rank 3).
These ranks include the partition dimension. The data tensor must have between
8 and 16,384 elements per partition. The vals tensor must have exactly 8 elements per partition.
The output will contain exactly 8 elements per partition and will be uint16 or
uint32 type. Default output type is uint32.

If a value in the vals tensor is not present in the data tensor, its
output slot is set to an all-ones sentinel (`0xFFFFFFFF` for uint32,
`0xFFFF` for uint16).

If provided, a mask is applied only to the data tensor.

- **dst** — a 2D tile containing indices (uint16 or uint32) of the 8 values in each partition with shape [par_dim, 8]
- **data** — the data tensor to find indices from
- **vals** — tensor containing the 8 values per partition whose indices will be found

---

### nki.isa.nc_match_replace8 {#nki-isa-nc_match_replace8}

`nki.isa.nc_match_replace8(dst, data, vals, imm, dst_idx, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.nc_match_replace8(dst, data, vals, imm, dst_idx=None, name=None)
```

Replace first occurrence of each value in `vals` with `imm` in `data`
using the Vector engine and return the replaced tensor. If `dst_idx`
tile is provided, the indices of the matched values are written to `dst_idx`.

Behavior with duplicate vals: when `vals` contains the same value multiple
times within a partition, the instruction processes `vals` in reverse
order (`i = 7, 6, ..., 0`), so each occurrence of a duplicate in `data`
is paired with a distinct column of `dst_idx`. The first matched data
position (smallest input-position index) is written to the highest
`dst_idx` slot among the duplicates, and subsequent occurrences fill
lower slots in descending order. For example, with
`data = [1, 2, 3, 4, 8, 8, 8, 8, 8, 8, 8]` and
`vals = [3, 8, 8, 8, 8, 8, 8, 8]`, `dst_idx` is
`[2, 10, 9, 8, 7, 6, 5, 4]`: column 0 holds the unique match for
`vals[0] = 3` at position 2, and the seven duplicate `8` values are
paired with their seven matching positions `[4, 5, 6, 7, 8, 9, 10]` in
descending order across columns 1-7. This duplicate-vals tie-break is the
opposite of `nc_find_index8`, which pairs duplicate vals with their
matching positions in ascending order across `dst_idx` columns.

The `data`, `dst`, and optional `dst_idx` tensors can have at most four
free dimensions (maximum rank 5). `vals` can have at most two free dimensions
(maximum rank 3). These ranks include the partition dimension.

- **dst** — output tile with replaced values
- **data** — the data tensor to search and replace in
- **vals** — tensor containing the 8 values per partition to match
- **imm** — the immediate float value to replace matched values with
- **dst_idx** — optional tile to store indices of matched values

---

### nki.isa.nc_matmul {#nki-isa-nc_matmul}

`nki.isa.nc_matmul(dst, stationary, moving, is_stationary_onezero, is_moving_onezero, is_transpose, accumulate, tile_position, tile_size, perf_mode, name)`

**Engine:** Tensor Engine

**Signature:**
```python
isa.nc_matmul(dst, stationary, moving, is_stationary_onezero=False, is_moving_onezero=False, is_transpose=False, accumulate=None, tile_position=(), tile_size=(), perf_mode=matmul_perf_mode.none, name=None)
```

Compute `dst = stationary.T @ moving` matrix multiplication using Tensor Engine.

The figure below illustrates how to map a matrix multiplication from a mathematical definition
to `nisa.nc_matmul` on Tensor Engine. The stationary tensor is loaded into the systolic array first and
stays in place, while the moving tensor streams through the array during computation.
For more detailed discussion of Tensor Engine capabilities, see
[Trainium arch guide](../../architecture/trainium_inferentia2_arch.md).

**Performance mode.**

On NeuronCore-v2, performance mode is not supported.
On NeuronCore-v3 and NeuronCore-v4, Tensor Engine supports FP8 double performance mode, enabled by setting
performance mode to `double_row`.
See [Trainium2 arch guide](../../architecture/trainium2_arch.md)
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

**Accumulation mode.**

The `accumulate` parameter controls whether the matmul result overwrites or accumulates onto the
`dst` PSUM tile:

- `accumulate=False`: **Overwrites** the PSUM destination. The first matmul targeting a PSUM location
  **must** use this overwrite mode to start the accumulation.
- `accumulate=True`: **Accumulates** the result to the existing PSUM content. Only valid after the PSUM
  location has been initialized by a matmul with `accumulate=False`. Using `accumulate=True`
  on an uninitialized PSUM location is undefined behavior on all targets.
- `accumulate=None` (default): The compiler automatically infers the correct flag — first write
  overwrites, subsequent writes accumulate.

*Hint*: Use `accumulate=(i > 0)` in a loop to explicitly overwrite on the first iteration
and accumulate on subsequent iterations, or simply omit the parameter to let the compiler handle it.

> **Note:**
> On NeuronCore-v2 and NeuronCore-v3, matmul accumulating into a PSUM location that was value-initialized
> by a non-matmul instruction (e.g., `memset`, `tensor_copy`) is not supported and is
> undefined behavior on hardware.
>

**Transpose mode.**

Tensor Engine can transpose a tile in SBUF by loading it as a stationary tile and using an identity matrix
as the moving tile.
Starting NeuronCore-v3, turning on transpose mode by setting `is_transpose=True` enables bit-accurate
data transpose, which can transpose tensors with NaN/Inf values properly.
See [Trainium2 arch guide](../../architecture/trainium2_arch.md)
for more details.

On NeuronCore-v2, Tensor Engine does not support transpose mode natively. However, setting `is_transpose=True`
ensures neuron-profile identifies this instruction as a transpose for performance metric accounting purposes.

**Memory types.**

The `nc_matmul` instruction *must* read inputs from SBUF and
write outputs to PSUM. Therefore, the `stationary` and `moving` must be SBUF tiles, and `dst` tile
must be a PSUM tile.

**Data types.**

The input `stationary` and `moving` tiles can be one of these supported data types:
`float8_e4m3/float8_e5m2/bfloat16/float16/tfloat32/float32`. Starting NeuronCore-v4, `float8_e4m3fn`
(OCP FP8) is also supported; `float8_e4m3` (legacy) and `float8_e4m3fn` cannot be mixed in the same matmul.
The `stationary` and `moving` tiles can have different data types, with one exception: if one of the input
tiles is `tfloat32/float32`, the other tile must also be `tfloat32/float32`.
On NeuronCore-v3 and NeuronCore-v4, when performance mode is `double_row`, `stationary` and `moving` tiles
must be one of `float8_e4m3` or `float8_e5m2` (plus `float8_e4m3fn` on NeuronCore-v4), but the two input
tiles can have different float8 formats (except that legacy `float8_e4m3` and OCP `float8_e4m3fn` cannot
appear together).

The accumulation precision internal to Tensor Engine is float32.
The `dst` tile must be a float32 tile in NeuronCore-v2 and NeuronCore-v3. Starting NeuronCore-v4,
`dst` can either be a float32 or bfloat16 tile.

**Layout.**

If performance mode is off, the contraction dimension of the matmul must be along the partition dimension in
both `stationary` and `moving` tiles.

If performance mode is `double_row`, the contraction dimension of the matmul is split between the partition dimension
and the first free dimension after the partition dimension in both `stationary` and `moving` tiles.
The first free dimension must be 2. For example, to perform a matmul of `[1, 256]@[256, 3]=[1, 3]`, the stationary
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

The `dst`, `stationary`, and `moving` tiles can have at most three free
dimensions (maximum rank 4, including the partition dimension).

Explicit tiling is required when the high-level matmul operation exceeds the tile size limits of `nc_matmul`.

**Profiler view syntax.**

Each `nc_matmul` call lowers to two ISA instructions in the profiler: a load instruction
(to load the stationary operand into the Tensor Engine) followed by a multiply instruction.
Both instructions will appear in profiler output for a single `nc_matmul` call.

The multiply instruction operands are displayed in a compact ISA syntax:

```text
src=<dtype>@<address>[<strides>][<num_elem>]
dst=<dtype>@<address>[<strides>][<num_elem>]
<M>*<K> acc_flags=<flags> psum_zero=<val>

```

Where:

- `<dtype>`: data type (e.g., `bfloat16`, `fp8e4`, `fp8e5`)
- `<address>`: hex memory address in SBUF (for src) or PSUM (for dst)
- `[<strides>]`: element strides per dimension (multi-dimensional)
- `[<num_elem>]`: number of elements per dimension (multi-dimensional)
- `<M>*<K>`: matmul dimensions (M rows × K contraction)
- `acc_flags`: accumulator control flags (e.g., `2` = reset accumulator)
- `psum_zero`: PSUM zero-initialization control value

**Tensor indirection.**

On NeuronCore-v4 and later, `dst`, `stationary`, and `moving` support tensor
indirection (gather/scatter) by passing a view created with `.indirect(index)`.
When `is_transpose=True`, `moving` cannot use tensor indirection. Tensor
indirection is incompatible with hardware tiling, so `tile_position` must be
`(0, 0)` (or unset) when it is used. The indirect operands must be a Tensor-Engine
float dtype (`float16`, `bfloat16`, `float8_e4m3`, `float8_e5m2`,
`float8_e4m3fn`).

When operands are manually allocated, their base partitions must satisfy:

- the `index` of every `.indirect()` view starts on a quadrant boundary
  (a multiple of 32);
- if `stationary` or `moving` uses `.indirect()`, the operand starts on
  the same partition as its `index`;
- if `dst` uses `.indirect()`, `dst`'s `index` starts on the same
  partition as `stationary`.

- **dst** — the matmul output
- **stationary** — the stationary operand
- **moving** — the moving operand
- **is_stationary_onezero** — hints to the compiler whether the `stationary` operand is a tile with ones/zeros only;
                       setting this field explicitly could lead to 2x better performance
                       if `stationary` tile is in float32; the field has no impact for non-float32 `stationary`
- **is_moving_onezero** — hints to the compiler whether the `moving` operand is a tile with ones/zeros only;
                       setting this field explicitly could lead to 2x better performance
                       if `moving` tile is in float32; the field has no impact for non-float32 `moving`
- **is_transpose** — controls Tensor Engine transpose mode on/off starting NeuronCore-v3
- **accumulate** — if True, accumulate the matmul result into the existing `dst` PSUM tile content;
                   if False, overwrite the existing content;
                   if None (default), auto-detect based on whether this PSUM location was previously written.
                   Not exposed for `nc_transpose`.
- **tile_position** — a 2D tuple (start_row, start_column) to control starting row in Tensor Engine tiling mode; start_column must be 0
- **tile_size** — a 2D tuple (row_size, column_size) to control row tile size in Tensor Engine tiling mode; column_size must be 128
- **perf_mode** — controls Tensor Engine FP8 double performance mode on/off starting NeuronCore-v3: `matmul_perf_mode.none` (default) disables double FP8 mode; `matmul_perf_mode.double_row` enables double FP8 mode which achieves 2x matmul throughput by packing two FP8 weight/ifmap element pairs and computing two multiplications in parallel per cycle; cannot be combined with column tiling mode. See the [Trainium2 arch guide](../../architecture/trainium2_arch.md) for more information.

Examples:

```python
@nki.jit
def nc_matmul_accumulate_kernel(lhsT, rhs):
    '''Tiled matmul with accumulate=(i > 0).

    Tiles K=512 into 4 tiles of 128, accumulating partial products in PSUM.
    The first iteration overwrites PSUM (accumulate=False via i > 0 == False),
    subsequent iterations accumulate (accumulate=True via i > 0 == True).
    '''
    K, M = lhsT.shape
    K_, N = rhs.shape
    TILE_K = 128

    result = nl.ndarray((M, N), dtype=nl.float32, buffer=nl.shared_hbm)
    res_psum = nl.ndarray((M, N), dtype=nl.float32, buffer=nl.psum)

    for i in nl.static_range(K // TILE_K):
        lhs_sb = nl.ndarray((TILE_K, M), dtype=lhsT.dtype, buffer=nl.sbuf)
        rhs_sb = nl.ndarray((TILE_K, N), dtype=rhs.dtype, buffer=nl.sbuf)
        nisa.dma_copy(dst=lhs_sb, src=lhsT[i * TILE_K : (i + 1) * TILE_K, :])
        nisa.dma_copy(dst=rhs_sb, src=rhs[i * TILE_K : (i + 1) * TILE_K, :])
        nisa.nc_matmul(
            dst=res_psum, stationary=lhs_sb, moving=rhs_sb, accumulate=(i > 0)
        )

    res_sb = nl.ndarray((M, N), dtype=nl.float32, buffer=nl.sbuf)
    nisa.tensor_copy(dst=res_sb, src=res_psum)
    nisa.dma_copy(dst=result, src=res_sb)
    return result
```

---

### nki.isa.nc_matmul_mx {#nki-isa-nc_matmul_mx}

`nki.isa.nc_matmul_mx(dst, stationary, moving, stationary_scale, moving_scale, tile_position, tile_size, accumulate, name)`

**Engine:** Tensor Engine

**Signature:**
```python
isa.nc_matmul_mx(dst, stationary, moving, stationary_scale, moving_scale, tile_position=None, tile_size=None, accumulate=None, name=None)
```

Compute matrix multiplication of MXFP8/MXFP4 quantized matrices with integrated dequantization using Tensor Engine.

> **Note:**
>
> Available only on NeuronCore-v4 and newer.
>

The NeuronCore-v4 Tensor Engine supports matrix multiplication of MXFP8/MXFP4 quantized matrices as defined in the
[OCP Microscaling standard](https://www.opencompute.org/documents/ocp-microscaling-formats-mx-v1-0-spec-final-pdf).
This instruction performs matrix multiplication between quantized `stationary` and `moving` matrices while
applying dequantization scales during computation. The micro-scaling group size is 32 elements in groups of
8 partitions × 4 elements per partition of both `stationary` and `moving` tensors.
See [Trainium3 arch guide](../../architecture/trainium3_arch.md)
for more detailed discussion.

**Tiling Mode.**

NeuronCore Tensor Engine is built upon a systolic array with 128 rows and 128 columns of processing elements (PEs).
For `nc_matmul_mx`, Tensor Engine supports only row tiling mode, which allows multiple `nc_matmul_mx` instructions with
a stationary partition dimension size smaller than 128 to run in parallel to improve hardware utilization.
Row tiling mode slices the 128 PE rows into 2x 64 row tiles or 4x 32 row tiles.

The row tile size can be set in the `tile_size` field as a tuple `(row_size, column_size)`,
where `column_size` must be 128. The stationary tile size must not exceed the chosen `tile_size`.

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

**Data types.**

The input `stationary` and `moving` tiles must be float8_e5m2_x4, float8_e4m3fn_x4, or float4_e2m1fn_x4
(4-packed quantized data types). The `stationary_scale` and `moving_scale` tiles must be float8_e8m0fnu
or uint8 (prefer `float8_e8m0fnu`: OCP MX standard; uint8 accepted for backward compatibility).
The `dst` tile can be float32 or bfloat16.

**Layout.**

The contraction dimension of the matrix multiplication is along the partition dimension of `stationary`
and `moving` tensors and also the x4 dimension within each packed data type element
(float8_e5m2_x4, float8_e4m3fn_x4, or float4_e2m1fn_x4).

The free dimension of the `stationary` tile matches the partition
dimension of the output `dst` tile in size, while the free dimension of the `moving` tile
matches the free dimension of the `dst` tile in size.

The scale tensors follow a special layout requirement. See more details in `nisa.quantize_mx` API doc.

*Tile size*

- The partition dimension size of `stationary` and `moving` must be identical and be either 32, 64, or 128.
- The free dimension size of `stationary` must be even and not exceed 128.
- The free dimension size of `moving` must not exceed 4096 when `dst` is in float32 or 8192 when `dst` is in
  bfloat16, matching the size of 8x PSUM banks (the entire PSUM).
- The scale tensors have partition dimensions that depend on whether the data tensors span multiple quadrants.
  See more details in `nisa.quantize_mx` API doc.
- `dst` can have at most three free dimensions (maximum rank 4). Each of
  `stationary`, `moving`, `stationary_scale`, and `moving_scale` must be
  contiguous in memory (multiple free dimensions are allowed if they collapse to a
  single contiguous run). These ranks include the partition dimension.

**Profiler view syntax.**

`nc_matmul_mx` uses the same profiler output format as nisa.nc_matmul,
except the source access pattern is interpreted as an MX-quantized tensor:
`src=<dtype>@$MX[<data_addr>,<scale_addr>,<start_scale_partition>]@[<step_elem>][<num_elem>]`.

**Tensor indirection.**

`dst` supports tensor indirection (gather/scatter) independently by passing a
view created with `.indirect(index)`. The `(stationary, stationary_scale)` and
`(moving, moving_scale)` pairs must agree on tensor indirection: either both
members of a pair use `.indirect()` with the **same** index, or neither does —
because in MX format the data bytes and scale exponents form a single logical
tensor. Tensor indirection is incompatible with hardware tiling, so
`tile_position` must be `(0, 0)` (or unset) when it is used.
(`nc_matmul_mx` itself is only available on NeuronCore-v4 and later.)

When operands are manually allocated, their base partitions must satisfy:

- the `index` of every `.indirect()` view starts on a quadrant boundary
  (a multiple of 32);
- if `stationary` or `moving` uses `.indirect()`, the operand starts on
  the same partition as its `index`;
- if `dst` uses `.indirect()`, `dst`'s `index` starts on the same
  partition as `stationary`.

- **dst** — the matrix multiplication output (PSUM tile)
- **stationary** — the stationary quantized matrix (SBUF tile)
- **moving** — the moving quantized matrix (SBUF tile)
- **stationary_scale** — the dequantization scales for stationary matrix
                         (SBUF tile)
- **moving_scale** — the dequantization scales for moving matrix (SBUF tile)
- **tile_position** — (optional) a 2D tuple (start_row, start_column) to control
                      the starting row in Tensor Engine tiling mode;
                      `start_column` must be 0
- **tile_size** — a 2D tuple (row_size, column_size) to control row and
                  column tile sizes in Tensor Engine tiling mode; `column_size`
                  must be 128.
- **accumulate** — (optional) if True, accumulate the matmul result into the existing
                   `dst` PSUM tile content; if False, overwrite the
                   existing content; if None (default), auto-detect based on
                   whether this PSUM location was previously written

---

### nki.isa.nc_stream_shuffle {#nki-isa-nc_stream_shuffle}

`nki.isa.nc_stream_shuffle(dst, src, shuffle_mask, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.nc_stream_shuffle(dst, src, shuffle_mask, name=None)
```

Apply cross-partition data movement within a quadrant of 32 partitions from source tile
`src` to destination tile `dst` using Vector Engine.

Both source and destination tiles can be in either SBUF or PSUM, and passed in by reference as arguments.
In-place shuffle is allowed, i.e., `dst` same as `src`. `shuffle_mask` is a 32-element list. Each mask
element must be in data type int or affine expression. `shuffle_mask[i]` indicates which input partition the
output partition [i] copies from within each 32-partition quadrant. The special value `shuffle_mask[i]=255`
means the output tensor in partition [i] will be unmodified. `nc_stream_shuffle` can be applied to multiple
of quadrants. In the case with more than one quadrant, the shuffle is applied to each quadrant independently,
and the same `shuffle_mask` is used for each quadrant. For more information about the cross-partition data movement,
see [cross-partition data movement](../../architecture/trainium_inferentia2_arch.md).

This API has 4 constraints on `src` and `dst`:

#. `dst` must have same data type as `src`.
#. `dst` must have the same number of elements per partition as `src`.
#. `src` and `dst` can have at most four free dimensions (maximum rank 5,
   including the partition dimension).
#. The access start partition of `src` (`src_start_partition`), does not have to match or be in the same quadrant
   as that of `dst` (`dst_start_partition`). However, `src_start_partition`/`dst_start_partition` needs to follow
   some special hardware rules with the number of active partitions `num_active_partitions`.
   `num_active_partitions = ceil(max(src_num_partitions, dst_num_partitions)/32) * 32`, where `src_num_partitions` and
   `dst_num_partitions` refer to the number of partitions the `src` and `dst` tensors access respectively.
   `src_start_partition`/`dst_start_partition` is constrained based on the value of `num_active_partitions`:

  * If `num_active_partitions` is 96/128, `src_start_partition`/`dst_start_partition` must be 0.

  * If `num_active_partitions` is 64, `src_start_partition`/`dst_start_partition` must be 0/64.

  * If `num_active_partitions` is 32, `src_start_partition`/`dst_start_partition` must be 0/32/64/96.

- **dst** — the destination tile
- **src** — the source tile
- **shuffle_mask** — a 32-element list that specifies the shuffle source and destination partition

---

### nki.isa.nc_version {#nki-isa-nc_version}

`nki.isa.nc_version(value)`

**Signature:**
```python
isa.nc_version(value)
```

NeuronCore version.

**Attributes**

| Name | Description |
| --- | --- |
| `gen2` | Trn1/Inf2 target |
| `gen3` | Trn2 target |
| `gen4` | Trn3 target |

---

### nki.isa.scalar_tensor_tensor {#nki-isa-scalar_tensor_tensor}

`nki.isa.scalar_tensor_tensor(dst, data, op0, operand0, op1, operand1, reverse0, reverse1, reduce_cmd, reduce_op, reduce_res, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.scalar_tensor_tensor(dst, data, op0, operand0, op1, operand1, reverse0=False, reverse1=False, reduce_cmd=reduce_cmd.reset_reduce, reduce_op=..., reduce_res=None, name=None)
```

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
See nki-type-casting for more information about type casting behavior.

**Layout.**

The parallel computation dimension of `nisa.scalar_tensor_tensor` is along the partition dimension.

**Tile size.**

The partition dimension size of input `data`, `operand1`, and output `dst` tiles must be
the same and must not exceed 128. The total number of elements per partition of input `data`, `operand1`,
and output `dst` tiles must be the same and must not exceed the
physical size of each SBUF partition.
If operand0 is not a scalar, the partition dimension size of `operand0` must be the same as that of `data`
and the number of elements per partition of `operand0` must be 1.
The `data`, `operand1`, and `dst` tiles can have at most two free dimensions
(maximum rank 3, including the partition dimension).

In addition to the elementwise computation above, this API can optionally perform a
reduction along the free dimension(s) of the elementwise result using Vector Engine,
at a small additional performance cost. The reduction result is written into
`reduce_res`, which must be a SBUF/PSUM tile with the same partition axis size as
the input tile `data` and one element per partition. `reduce_op` must be `nl.add`;
bitvec operators do not support accumulation.

.. math::
  dst = (data <op0> operand0) <op1> operand1 \\
  reduce\_res = reduce\_op(dst, axis=<FreeAxis>)

**Accumulator behavior:**

The Vector Engine maintains internal accumulator registers that can be controlled via the `reduce_cmd` parameter:

- `reduce_cmd.reset_reduce`: (default) Reset accumulators to 0, then accumulate the
  current results.
- `reduce_cmd.reduce`: Continue accumulating without resetting.

> **Note:**
> The accumulator registers are shared across Vector Engine accumulation instructions including
> `~nki.isa.exponential`,
> `~nki.isa.range_select`,
> `~nki.isa.scalar_tensor_tensor`,
> `~nki.isa.select_reduce`,
> `~nki.isa.tensor_scalar_cumulative`, and
> `~nki.isa.tensor_scalar_reduce`.
>

- **dst** — the output tile
- **data** — the input tile
- **op0** — the first math operator used with operand0 (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators)
- **operand0** — a scalar constant or a tile of shape `(data.shape[0], 1)`, where data.shape[0]
                is the partition axis size of the input `data` tile
- **reverse0** — reverse ordering of inputs to `op0`; if false, `operand0` is the rhs of `op0`;
                 if true, `operand0` is the lhs of `op0`
- **op1** — the second math operator used with operand1 (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators)
- **operand1** — a tile with the same size as `data` for element-wise operation
- **reverse1** — reverse ordering of inputs to `op1`; if false, `operand1` is the rhs of `op1`;
                 if true, `operand1` is the lhs of `op1`
- **reduce_cmd** — (optional) Controls the accumulator behavior, supports:
                   `reset_reduce` (default) and `reduce`.
- **reduce_op** — (optional) Reduction operator to apply (only `nl.add` is supported)
- **reduce_res** — (optional) `(data.shape[0], 1)` SBUF/PSUM tile that receives the DVE accumulator
                   value after `reduce_cmd` is applied, via a fused non-destructive read out
                   (the accumulator is preserved, so a following DVE reduction continues to accumulate into it).
                   Pass `None` to skip the read out when chaining multiple instructions.

---

### nki.isa.tensor_copy_predicated {#nki-isa-tensor_copy_predicated}

`nki.isa.tensor_copy_predicated(dst, src, predicate, reverse_pred, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.tensor_copy_predicated(dst, src, predicate, reverse_pred=False, name=None)
```

Conditionally copy elements from the `src` tile to the `dst` tile on SBUF / PSUM
based on a `predicate` using Vector Engine.

This instruction provides low-level control over conditional data movement on NeuronCores,
optimized for scenarios where only selective copying of elements is needed. Either `src` or
`predicate` may be in PSUM, but not both simultaneously. Both `src` and `predicate` are permitted to be in SBUF.
`reverse_pred` can only be set to true if `src` is a scalar.

Shape and data type constraints:

1. `src` (if it is a tensor), `dst`, and `predicate` must occupy the same number of partitions and same number of elements per partition.
2. `predicate` must be of type `uint8`, `uint16`, or `uint32`.
3. `src` and `dst` must share the same data type.
4. `dst`, `predicate`, and a tensor `src` can have at most three free
   dimensions (maximum rank 4, including the partition dimension).
5. if `src` is a scalar, i.e. a Python `int` or `float`, only the *kind* must match `dst`: a Python `float` requires a float `dst` dtype and a Python `int` requires an integer `dst` dtype. The bit widths need **not** match -- the scalar is materialized in `dst`'s dtype, so e.g. a Python `int` may be copied into an `int8`.

**Behavior:**

- Where predicate is True: The corresponding elements from `src` are copied to `dst` tile. If `src` is a scalar, the scalar is copied to the `dst` tile.
- Where predicate is False: The corresponding values in `dst` tile are unmodified

**Tensor indirection.**

On NeuronCore-v4 and later, `dst` and `predicate` support tensor indirection
(gather/scatter) by passing a view created with `.indirect(index)`. `src` does
**not** support tensor indirection. Runs on the Vector engine.

When operands are manually allocated, their base partitions must satisfy:

- the `index` of every `.indirect()` view starts on a quadrant boundary
  (a multiple of 32);
- if `predicate` uses `.indirect()`, `predicate` starts on the same
  partition as its `index`;
- if `dst` uses `.indirect()` and `predicate` is in SBUF, `dst`'s
  `index` starts on the same partition as `predicate`;
- if `dst` uses `.indirect()` and `predicate` is in PSUM and uses
  `.indirect()`, `dst`'s `index` starts on the same partition as
  `predicate`'s `index`.

:param `src`: The source tile or number to copy elements from when `predicate` is True
:param `dst`: The destination tile to copy elements to
:param `predicate`: A tile that determines which elements to copy
- **reverse_pred** — A boolean that reverses the effect of `predicate`. Only available when `src` is a scalar.

---

### nki.isa.tensor_partition_reduce {#nki-isa-tensor_partition_reduce}

`nki.isa.tensor_partition_reduce(dst, op, data, name)`

**Engine:** GpSimd Engine

**Signature:**
```python
isa.tensor_partition_reduce(dst, op, data, name=None)
```

Apply a reduction operation across partitions of an input `data` tile using GpSimd Engine.

The `data` and `dst` tiles can have at most four free dimensions (maximum
rank 5, including the partition dimension).

- **dst** — output tile with reduced result
- **op** — the reduction operator (add, mean, max, bitwise_or, bitwise_and,
    bitwise_xor)
- **data** — the input tile to be reduced

---

### nki.isa.tensor_reduce {#nki-isa-tensor_reduce}

`nki.isa.tensor_reduce(dst, op, data, axis, negate, keepdims, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.tensor_reduce(dst, op, data, axis, negate=False, keepdims=False, name=None)
```

Apply a reduction operation to the free axes of an input `data` tile using Vector Engine.

The reduction operator is specified in the `op` input field
(see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for a list of supported reduction operators).
`nisa.tensor_reduce` supports two types of reduction operators: 1) bitvec operators (e.g., bitwise_and, bitwise_or)
and 2) arithmetic operators (e.g., add, subtract, multiply).

The reduction axes are specified in the `axis` field as an int or list of ints indicating
which dimensions to reduce. The reduction axes must be the last contiguous free dimension(s)
of the tile, ending at the final dimension. Axis 0 (partition axis) cannot be reduced.

For example, given a 4D tile `(P, D1, D2, D3)`:

- `axis=(3,)` reduces only `D3`
- `axis=(2, 3)` reduces `D2` and `D3`
- `axis=(1, 2, 3)` reduces `D1`, `D2`, and `D3`

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
See nki-type-casting for more information about type casting behavior.

**Layout.**

`nisa.tensor_reduce` only supports free axes reduction. Therefore, the partition dimension of the input
`data` is considered the parallel compute dimension. To perform a partition axis reduction, we can either:

1. invoke a `nisa.nc_transpose` instruction on the input tile and then this `nisa.tensor_reduce`
   on the transposed tile, or
2. invoke `nisa.nc_matmul` instructions to multiply a `nl.ones([128, 1], dtype=data.dtype)` tile as a stationary
   tensor with the input tile as a moving tensor. See more discussion on Tensor Engine alternative usage in
   [Trainium architecture guide](../../architecture/trainium2_arch.md).

**Tile size.**

The partition dimension size of input `data` and output `dst` tiles must be the same and must not exceed 128.
The number of elements per partition of `data` must not
exceed the physical size of each SBUF partition. Both `data` and `dst` can have at most four
free dimensions (maximum rank 5, including the partition dimension). The number of elements per
partition in `dst` must be consistent with the `axis` field. For example, if `axis` indicates all free dimensions of `data` are reduced,
the number of elements per partition in `dst` must be 1.

**Tensor indirection.**

On NeuronCore-v4 and later, both `dst` and `data` support tensor indirection
(gather/scatter) by passing a view created with `.indirect(index)`. Runs on the
Vector engine.

When operands are manually allocated, their base partitions must satisfy:

- the `index` of every `.indirect()` view starts on a quadrant boundary
  (a multiple of 32);
- if `data` uses `.indirect()`, `data` starts on the same partition as
  its `index`;
- if `dst` uses `.indirect()` and `data` is in SBUF, `dst`'s `index`
  starts on the same partition as `data`;
- if `dst` uses `.indirect()` and `data` is in PSUM and uses
  `.indirect()`, `dst`'s `index` starts on the same partition as
  `data`'s `index`.

- **dst** — output tile of the reduction result
- **op** — the reduction operator (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported reduction operators)
- **data** — the input tile to be reduced
- **axis** — int or tuple/list of ints. The axis (or axes) along which to reduce;
             must be the last contiguous free dimension(s) ending at the final dim.
             For example, for a 4D tile `(P, D1, D2, D3)`: valid values are
             `(3,)`, `(2, 3)`, or `(1, 2, 3)`. Axis 0 (partition dim) cannot be reduced.
- **negate** — if True, reduction result is multiplied by `-1.0`;
               only applicable when op is an arithmetic operator
- **keepdims** — If this is set to True, the axes which are reduced are left in the result as dimensions with size one.
                 With this option, the result will broadcast correctly against the input array.

---

### nki.isa.tensor_scalar {#nki-isa-tensor_scalar}

`nki.isa.tensor_scalar(dst, data, op0, operand0, reverse0, op1, operand1, reverse1, engine, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.tensor_scalar(dst, data, op0, operand0, reverse0=False, op1=None, operand1=None, reverse1=False, engine=engine.unknown, name=None)
```

Apply up to two math operators to the input `data` tile by broadcasting scalar/vector operands
in the free dimension using Vector or Scalar or GpSimd Engine: `(data <op0> operand0) <op1> operand1`.

The input `data` tile can be an SBUF or PSUM tile. Both `operand0` and `operand1` can be
SBUF or PSUM tiles of shape `(data.shape[0], 1)`, i.e., vectors,
or compile-time constant scalars.

`op1` and `operand1` are optional, but must be `None` (default values) when unused.
Note, performing one operator has the same performance cost as performing two operators in the instruction.

When the operators are non-commutative (e.g., subtract), we can reverse ordering of the inputs for each operator through:

  - `reverse0 = True`: `tmp_res = operand0 <op0> data`
  - `reverse1 = True`: `operand1 <op1> tmp_res`

The `tensor_scalar` instruction supports two types of operators: 1) bitvec
operators (e.g., bitwise_and) and 2) arithmetic operators (e.g., add).
See [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for the full list of supported operators.
The two operators, `op0` and `op1`, in a `tensor_scalar` instruction must be of the same type
(both bitvec or both arithmetic).
If bitvec operators are used, the `tensor_scalar` instruction must run on Vector Engine. Also, the input/output
data types must be integer types, and input elements are treated as bit patterns without any data type casting.

If arithmetic operators are used, the `tensor_scalar` instruction can run on Vector or Scalar or GpSimd Engine.
However, each engine supports limited arithmetic operators (see tbl-aluop). The Scalar Engine on trn2 only
supports some operator combinations:

  - `op0=nl.multiply` and `op1=nl.add`
  - `op0=nl.multiply` and `op1=None`
  - `op0=nl.add` and `op1=None`

Also, arithmetic operators impose no restriction on the data types of input tensor `data` and output tensor `dst`,
but the operand0 and operand1 (if used) must be float32.
The compute engine automatically casts `data.dtype` to float32
and performs the operators in float32 math.
The float32 computation results are cast to `dst.dtype` at no additional performance cost.
See nki-type-casting for more information about type casting behavior.

**Tensor indirection.**

On NeuronCore-v4 and later, `dst` and `data` support tensor indirection
(gather/scatter) by passing a view created with `.indirect(index)`. `operand0`
and `operand1` do **not** support tensor indirection. When tensor indirection is
used, the operation must run on the Vector, Scalar, or GpSimd engine. Tensor
indirection is **not** supported when the op matches the int32/uint32
address-arithmetic pattern (`tensor_scalar_addr`).

When operands are manually allocated, their base partitions must satisfy:

- the `index` of every `.indirect()` view starts on a quadrant boundary
  (a multiple of 32);
- if `data` uses `.indirect()`, `data` starts on the same partition as
  its `index`;
- if `dst` uses `.indirect()` and `data` is in SBUF, `dst`'s `index`
  starts on the same partition as `data`;
- if `dst` uses `.indirect()` and `data` is in PSUM and uses
  `.indirect()`, `dst`'s `index` starts on the same partition as
  `data`'s `index`.

In addition, on the Scalar engine a scattered `dst` cannot be in PSUM.

The `data` and `dst` tiles can normally have at most three free dimensions
(maximum rank 4, including the partition dimension). The specialized int32/uint32
address-arithmetic patterns have a lower limit of two free dimensions (maximum rank 3).

- **dst** — an output tile of `(data <op0> operand0) <op1> operand1` computation
- **data** — the input tile
- **op0** — the first math operator used with operand0 (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators).
- **operand0** — a scalar constant or a tile of shape `(data.shape[0], 1)`, where data.shape[0]
                is the partition axis size of the input `data` tile.
                Must be `None` or `0` when `op0` is a unary operator (e.g., `nl.abs`).
- **reverse0** — reverse ordering of inputs to `op0`; if false, `operand0` is the rhs of `op0`;
                 if true, `operand0` is the lhs of `op0`
- **op1** — the second math operator used with operand1 (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators);
            this operator is optional
- **operand1** — a scalar constant or a tile of shape `(data.shape[0], 1)`, where data.shape[0]
                is the partition axis size of the input `data` tile
- **reverse1** — reverse ordering of inputs to `op1`; if false, `operand1` is the rhs of `op1`;
                 if true, `operand1` is the lhs of `op1`
- **engine** — (optional) the engine to use for the operation: `nki.isa.engine.vector`, `nki.isa.engine.scalar`,
               `nki.isa.engine.gpsimd` (only allowed for rsqrt) or `nki.isa.engine.unknown` (default, let
               compiler select best engine based on the input tile shape).

---

### nki.isa.tensor_scalar_cumulative {#nki-isa-tensor_scalar_cumulative}

`nki.isa.tensor_scalar_cumulative(dst, src, op0, op1, imm0, imm1, reduce_cmd, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.tensor_scalar_cumulative(dst, src, op0, op1, imm0, imm1=None, reduce_cmd=reduce_cmd.reset_reduce, name=None)
```

Perform tensor-scalar arithmetic operation with cumulative reduction using Vector Engine.

The operation applies a scalar operation to each tensor element, then performs a cumulative
reduction, storing the cumulative results in the destination tensor.

The operation can be expressed in pseudocode as:

```python
if reduce_cmd == reset_reduce:
    if op1 == add or op1 == subtract:
        reg = 0
    elif op1 == mult:
        reg = 1
    elif op1 == max:
        reg = -inf
    elif op1 == min:
        reg = +inf
elif reduce_cmd == reduce:
    reg = reg
elif reduce_cmd == load_reduce:
    reg = imm1

for i in len(in_tensor):
    if not reverse0:
        reg = op1(op0(in_tensor[i], imm0), reg)
        out_tensor[i] = reg
    else:
        reg = op1(op0(imm0, in_tensor[i]), reg)
        out_tensor[i] = reg

```

**Operation constraints:**

- Scalar operation (`op0`) must be an arithmetic op (e.g., add, mult, max)
- Reduction operation (`op1`) is limited to add, subtract, mult, max, min
- Input / output dtypes are restricted to BF16, FP16, FP32, FP8, UINT8, UINT16, INT8, INT16
    - INT32/UINT32 are not supported as input/output dtypes (ISA limitation)
- `src` and `dst` can have at most three free dimensions (maximum rank 4,
  including the partition dimension)

**Accumulator behavior:**

The Vector Engine maintains internal accumulator registers controlled via `reduce_cmd`:

- `reset_reduce`: Reset accumulator based on reduction operation type
- `load_reduce`: Initialize accumulator with `imm1` value
- `reduce`: Continue with existing accumulator value

> **Note:**
> The accumulator registers are shared across Vector Engine accumulation instructions including
> `~nki.isa.exponential`,
> `~nki.isa.range_select`,
> `~nki.isa.scalar_tensor_tensor`,
> `~nki.isa.select_reduce`,
> `~nki.isa.tensor_scalar_cumulative`, and
> `~nki.isa.tensor_scalar_reduce`.
>

**Tensor indirection.**

On NeuronCore-v4 and later, `dst` and `src` support tensor indirection
(gather/scatter) by passing a view created with `.indirect(index)`. `imm0` and
`imm1` do **not** support tensor indirection. Runs on the Vector engine.

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

- **dst** — The destination tensor to write cumulative results to
- **src** — The source tensor to process
- **op0** — Scalar arithmetic operation to apply to each element
- **op1** — Cumulative arithmetic operation for cumulative computation
- **imm0** — Scalar or vector value for tensor-scalar operation. Must be FP32 datatype
- **imm1** — (optional) Initial scalar or vector value for the accumulator when `load_reduce`
                        is specified as the `reduce_cmd`. Must be FP32 datatype
- **reduce_cmd** — (optional) Control accumulator behavior using `nisa.reduce_cmd` values,
                            defaults to `reset_reduce`

---

### nki.isa.tensor_scalar_reduce {#nki-isa-tensor_scalar_reduce}

`nki.isa.tensor_scalar_reduce(dst, data, op0, operand0, reduce_op, reduce_res, reverse0, reduce_cmd, reduce_init, reduce_res_negate, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.tensor_scalar_reduce(dst, data, op0, operand0, reduce_op, reduce_res=None, reverse0=False, reduce_cmd=reduce_cmd.reset_reduce, reduce_init=None, reduce_res_negate=False, name=None)
```

Perform the same computation as `nisa.tensor_scalar` with one math operator
and also a reduction along the free dimension of the `nisa.tensor_scalar` result using Vector Engine.

Refer to nisa.tensor_scalar for semantics of `data/op0/operand0`.
Unlike regular `nisa.tensor_scalar` where two operators are supported, only one
operator is supported in this API. Also, `op0` can only be arithmetic operation in [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop).
Bitvec operators are not supported in this API.

In addition to nisa.tensor_scalar computation, this API also performs a reduction
along the free dimension(s) of the nisa.tensor_scalar result, at a small additional
performance cost. The reduction result is written into `reduce_res`, which must be a
SBUF/PSUM tile with the same partition axis size as the input tile `data` and one element per partition.
The `reduce_op` can be any of `nl.add`, `nl.multiply`, `nl.max` or `nl.min`.

Reduction axis is not configurable in this API. If the input tile has multiple free axis, the API will
reduce across all of them. The `data` and `dst` tiles can have at most three
free dimensions (maximum rank 4, including the partition dimension).

.. math::
  result = data <op0> operand0 \\
  reduce\_res = reduce\_op(dst, axis=<FreeAxis>)

**Accumulator behavior:**

The Vector Engine maintains internal accumulator registers that can be controlled via the `reduce_cmd` parameter:

- `reduce_cmd.reset_reduce`: (default) Reset accumulators to the identity value for `reduce_op`,
  then accumulate the current results. The identity is `0` for `nl.add`, `1` for `nl.multiply`,
  `-inf` for `nl.max` and `+inf` for `nl.min`.
- `reduce_cmd.reduce`: Continue accumulating without resetting (useful for multi-step reductions across tiles).
- `reduce_cmd.load_reduce`: Load the values from `reduce_init` into the accumulator, then accumulate
  the current result on top of it.

> **Note:**
> `reduce_init` should only be set when `reduce_cmd` is `load_reduce`.
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

**Tensor indirection.**

On NeuronCore-v4 and later, `dst` and `data` support tensor indirection
(gather/scatter) by passing a view created with `.indirect(index)`. `operand0`,
`reduce_res`, and `reduce_init` do **not** support tensor indirection. Runs on
the Vector engine.

When operands are manually allocated, their base partitions must satisfy:

- the `index` of every `.indirect()` view starts on a quadrant boundary
  (a multiple of 32);
- if `data` uses `.indirect()`, `data` starts on the same partition as
  its `index`;
- if `dst` uses `.indirect()` and `data` is in SBUF, `dst`'s `index`
  starts on the same partition as `data`;
- if `dst` uses `.indirect()` and `data` is in PSUM and uses
  `.indirect()`, `dst`'s `index` starts on the same partition as
  `data`'s `index`.

- **dst** — an output tile of `(data <op0> operand0)` computation
- **data** — the input tile
- **op0** — the math operator used with operand0 (any arithmetic operator in [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) is allowed).
- **operand0** — a scalar constant or a tile of shape `(data.shape[0], 1)`, where data.shape[0]
                is the partition axis size of the input `data` tile.
                Must be `None` or `0` when `op0` is a unary operator (e.g., `nl.abs`).
- **reverse0** — `(not supported yet)` reverse ordering of inputs to `op0`; if false, `operand0` is the rhs of `op0`;
                 if true, `operand0` is the lhs of `op0`. `<-- currently not supported yet.`
- **reduce_op** — the reduce operation to perform on the free dimension of `data <op0> operand0`
- **reduce_res** — a tile of shape `(data.shape[0], 1)`, where data.shape[0]
                is the partition axis size of the input `data` tile. The result of `reduce_op(data <op0> operand0)`
                is written into the tile.

                Pass `None` to keep the reduction result in the Vector Engine's internal
                accumulator without writing it out. This is useful when chaining multiple
                calls that reduce into the same accumulator — only the final call needs to
                pass a tile to retrieve the accumulated result.
- **reduce_cmd** — Control the state of reduction registers for accumulating reduction results.
                   Supported: `reset_reduce` (default), `reduce`, `load_reduce`.
- **reduce_init** — Initial value for reduction when using `reduce_cmd.load_reduce`.
                    Must be provided when `reduce_cmd` is `load_reduce`. Supported dtypes: float32.
- **reduce_res_negate** — Negate the reduction result before writing it to `reduce_res`, does not modify
                          the internal reduction registers.

---

### nki.isa.tensor_tensor {#nki-isa-tensor_tensor}

`nki.isa.tensor_tensor(dst, data1, data2, op, engine, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.tensor_tensor(dst, data1, data2, op, engine=engine.unknown, name=None)
```

Perform an element-wise operation of input two tiles using Vector Engine or GpSimd Engine.
The two tiles must have the same partition axis size and the same number of elements per partition.

The element-wise operator is specified using the `op` field. Valid choices for `op`:

1. Any supported *binary* operator that runs on the Vector Engine. (See [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for details.)
2. `nl.power`. (Which runs on the GpSimd engine.)

For bitvec operators, the input/output data types must be integer types and Vector Engine treats
all input elements as bit patterns without any data type casting. For arithmetic operators, the behavior
depends on the data types:

- **Float types**: The engine casts input data types to float32 and performs the element-wise operation
  in float32 math. The float32 results are cast to `dst.dtype` at no additional performance cost.
- **int32/uint32 types**: When all input/output tiles are int32 or uint32, the operation defaults to
  GpSimd Engine, which uses native integer arithmetic. This ensures exact results for all 32-bit integer
  values. You may override this by passing `engine=nki.isa.engine.vector` explicitly.

See nki-type-casting for more information about type casting behavior.

Since GpSimd Engine cannot access PSUM, the input/output tiles cannot be in PSUM if `op` is `nl.power`.
Similarly, the automatic GpSimd dispatch for int32/uint32 falls back to Vector Engine when any operand
resides in PSUM. (See [NeuronCore-v2 Compute Engines](../../architecture/trainium_inferentia2_arch.md#arch-sec-neuron-core-engines) for details.)

Otherwise, the output tile can be in either SBUF or PSUM.
However, the two input tiles, `data1` and `data2` cannot both reside in PSUM.
The three legal cases are:

1. Both `data1` and `data2` are in SBUF.
2. `data1` is in SBUF, while `data2` is in PSUM.
3. `data1` is in PSUM, while `data2` is in SBUF.

Note, if you need broadcasting capability in the free dimension for either input tile, you should consider
using nki.isa.tensor_scalar API instead,
which has better performance than `nki.isa.tensor_tensor` in general.

The `data1`, `data2`, and `dst` tiles can have at most three free
dimensions (maximum rank 4, including the partition dimension).

**Tensor indirection.**

On NeuronCore-v4 and later, `dst` and `data1` support tensor indirection
(gather/scatter) by passing a view created with `.indirect(index)`. `data2`
does **not** support tensor indirection. When tensor indirection is used, the
operation must run on the Vector or GpSimd engine.

When operands are manually allocated, their base partitions must satisfy:

- the `index` of every `.indirect()` view starts on a quadrant boundary
  (a multiple of 32);
- if `data1` uses `.indirect()`, `data1` starts on the same partition
  as its `index`;
- if `dst` uses `.indirect()` and `data1` is in SBUF, `dst`'s
  `index` starts on the same partition as `data1`;
- if `dst` uses `.indirect()` and `data1` is in PSUM and uses
  `.indirect()`, `dst`'s `index` starts on the same partition as
  `data1`'s `index`.

- **dst** — an output tile of the element-wise operation
- **data1** — lhs input operand of the element-wise operation
- **data2** — rhs input operand of the element-wise operation
- **op** — a binary math operator (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators)
- **engine** — (optional) the engine to use for the operation: `nki.isa.engine.vector`, `nki.isa.engine.gpsimd`
               or `nki.isa.engine.unknown` (default, let compiler select best engine based on the input tile shape).

---

### nki.isa.tensor_tensor_scan {#nki-isa-tensor_tensor_scan}

`nki.isa.tensor_tensor_scan(dst, data0, data1, initial, op0, op1, reverse0, reverse1, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.tensor_tensor_scan(dst, data0, data1, initial, op0, op1, reverse0=False, reverse1=False, name=None)
```

Perform a scan operation of two input tiles using Vector Engine.

Mathematically, the tensor_tensor_scan instruction on Vector Engine performs
the following computation per partition:

```python
# Let's assume we work with numpy, and data0 and data1 are 2D (with shape[0] being the partition axis)
import numpy as np

result = np.ndarray(data0.shape, dtype=data0.dtype)
result[:, 0] = op1(op0(data0[:, 0], initial), data1[:, 0])

for i in range(1, data0.shape[1]):
    result[:, i] = op1(op0(data0[:, i], result[:, i-1]), data1[:, i])

```

The two input tiles (`data0` and `data1`) must have the same
partition axis size and the same number of elements per partition.
The third input `initial` can either be a float32 compile-time scalar constant
that will be broadcasted in the partition axis of `data0`/`data1`, or a tile
with the same partition axis size as `data0`/`data1` and one element per partition.

The two input tiles, `data0` and `data1` cannot both reside in PSUM. The three legal cases are:

1. Both `data0` and `data1` are in SBUF.
2. `data0` is in SBUF, while `data1` is in PSUM.
3. `data0` is in PSUM, while `data1` is in SBUF.

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
See nki-type-casting for more information about type casting behavior.

The `data0`, `data1`, and `dst` tiles can have at most two free dimensions
(maximum rank 3, including the partition dimension).

- **dst** — an output tile of the scan operation
- **data0** — lhs input operand of the scan operation
- **data1** — rhs input operand of the scan operation
- **initial** — starting state of the scan; can be a SBUF/PSUM tile with 1 element/partition or a scalar
                    compile-time constant
- **op0** — a binary arithmetic math operator (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators)
- **op1** — a binary arithmetic math operator (see [Supported Math Operators for NKI ISA](nki.api.shared.md#nki-aluop) for supported operators)
- **reverse0** — reverse ordering of inputs to `op0`; if false, `data0` is the lhs of `op0`;
               if true, `data0` is the rhs of `op0`
- **reverse1** — reverse ordering of inputs to `op1`; if false, `data1` is the rhs of `op1`;
               if true, `data1` is the lhs of `op1`

---

### nki.isa.topk {#nki-isa-topk}

`nki.isa.topk(val_dst, idx_dst, src, n, sorted, name)`

**Engine:** GpSimd Engine

**Signature:**
```python
isa.topk(val_dst, idx_dst, src, n, sorted=False, name=None)
```

Find the k largest values and their indices in each 16-partition group using GpSIMD Engine.

Each **group of 16 partitions** operates independently on its own data.
Within a group the source is read in a snake layout: elements 0..15 fill
partitions 0..15 at free-dim position 0, elements 16..31 fill position 1,
and so on. The parameter `n` gives the number of active BF16 elements in
one snake; storage slots past `n` are ignored. A tile with 128 partitions
therefore holds 8 independent top-k problems, solved by one instruction.

For each group, the instruction selects the top-k elements by value from that
group's own data and writes:

- `val_dst`: the k largest values (BF16)
- `idx_dst`: the original 0-based indices of those values (uint32)

`k` -- the number of results -- is not a parameter. It is exactly the
per-partition free size of `val_dst`: to ask for the top 64, pass a
`val_dst` whose free dimension is 64.

Results are written in the same snake layout as the input, so result `j` of a
group lands at partition `j % 16`, free-dim position `j // 16`, and
`idx_dst` holds indices into that group's own snake (0..n-1). All `k`
results therefore fit in the first `ceil(k / 16)` free-dim positions (for
`k = 64`, positions 0..3):

- Positions `0 .. ceil(k/16) - 1` are written. The hardware writes each
  position across all 16 partitions at once, so when `k` is not a multiple of
  16 the last one carries `16*ceil(k/16) - k` slots of unspecified data,
  which may include NaN.
- Positions `ceil(k/16) .. k - 1` are **never written**. They exist only
  because `k` is read from the free dimension; a narrower tile would simply
  request a smaller `k`.

Read only the first `k` results, using the mapping above.

By default (`sorted=False`) the k results are written in an **unspecified
order** -- the selected set is well defined, but the order within
`val_dst`/`idx_dst` must not be relied upon. Pass `sorted=True` to get
each group's results ordered descending by value; `val_dst` and `idx_dst`
are reordered consistently, so `idx_dst[j]` always remains the source index
of `val_dst[j]`. The sort is not stable: ties between equal values may
resolve in any order. Sorting costs slightly more than the unsorted path and
restricts k to at most 8192.

NaN input is undefined behavior: a NaN may appear in the output even though
NaN never compares greater than any value. Mask or replace NaN before calling
topk. The CPU simulator warns when it sees NaN input.

The source and outputs must be in SBUF. The source must be BF16.

**Estimated instruction cost:**

`4*n + k` GpSIMD Engine cycles per instruction. The 16-partition groups
execute in parallel, one per GpSIMD core, so activating more of them does not
add cycles.

- `n` is the number of active BF16 elements in one 16-partition input snake
- `k` is the number of top elements to select (= `val_dst` free dim)

**Constraints:**

- Source must be BF16 in SBUF.
- `val_dst` must be BF16 in SBUF with shape `[par_dim, k]`.
- `idx_dst` must be uint32 in SBUF with shape `[par_dim, k]`.
- 8 <= n < 65536
- 1 <= k <= 16384 and k <= n
- k <= 8192 when `sorted=True`
- src free dim >= ceil(n / 16)
- partition dim must be a multiple of 16
- `src` can have at most three free dimensions (maximum rank 4); `val_dst`
  and `idx_dst` can have at most two free dimensions (maximum rank 3). These
  ranks include the partition dimension.

- **val_dst** — Output tile for the top-k values (BF16), shape [par_dim, k].
- **idx_dst** — Output tile for the top-k indices (uint32), shape [par_dim, k].
- **src** — Source tile containing the input snake (BF16), shape [par_dim, src_x].
- **n** — Number of active BF16 elements in each 16-partition input snake.
- **sorted** — If True, sort the outputs of each 16-partition group descending
    by value. Defaults to False, which leaves the output order unspecified.

**Example**

```python
import nki
import nki.isa as nisa
import nki.language as nl
import numpy as np
import pytest

@nki.jit
def topk_sorted_kernel(in_tensor):
    '''Select the 8 largest of 42 values per 16-partition group, sorted descending.'''
    par_dim = in_tensor.shape[0]
    n = 42  # number of active BF16 elements in the snake
    src_x = (n + 15) // 16  # = 3 columns; the 48 - 42 = 6 trailing slots are ignored
    k = 8

    src = nl.ndarray((par_dim, src_x), dtype=nl.bfloat16, buffer=nl.sbuf)
    nisa.dma_copy(dst=src, src=in_tensor)

    val_dst = nl.ndarray((par_dim, k), dtype=nl.bfloat16, buffer=nl.sbuf)
    idx_dst = nl.ndarray((par_dim, k), dtype=nl.uint32, buffer=nl.sbuf)
    nisa.topk(val_dst=val_dst, idx_dst=idx_dst, src=src, n=n, sorted=True)

    # The k results land in the first ceil(k/16) = 1 free-dim position: result j at
    # partition j % 16, position j // 16. Kernel outputs must live in shared_hbm.
    val_out = nl.ndarray((par_dim, k), dtype=nl.bfloat16, buffer=nl.shared_hbm)
    idx_out = nl.ndarray((par_dim, k), dtype=nl.uint32, buffer=nl.shared_hbm)
    nisa.dma_copy(dst=val_out, src=val_dst)
    nisa.dma_copy(dst=idx_out, src=idx_dst)
    return val_out, idx_out
```

---

