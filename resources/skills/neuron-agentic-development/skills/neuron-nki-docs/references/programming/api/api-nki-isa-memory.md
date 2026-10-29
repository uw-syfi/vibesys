# nki.isa — Data Movement & Memory

> **Module**: nki.isa
> **Functions**: 6

## Overview

Data Movement & Memory instructions.

## Functions

### nki.isa.dma_copy {#nki-isa-dma_copy}

`nki.isa.dma_copy(dst, src, priority, oob_mode, dge_mode, engine, name)`

**Engine:** DMA Engine

**Signature:**
```python
isa.dma_copy(dst, src, priority=None, oob_mode=oob_mode.error, dge_mode=dge_mode.unknown, engine=engine.unknown, name=None)
```

Copy data from `src` to `dst` using DMA engines.

This instruction performs data movement between memory locations (SBUF or HBM) using DMA engines.
The operation copies data from the source tensor to the destination tensor: `dst = src`.

`nisa.dma_copy` supports different modes of DMA descriptor generation (DGE):

- `nisa.dge_mode.none`: Neuron Runtime generates DMA descriptors and stores them into HBM before NEFF execution.
- `nisa.dge_mode.swdge`: Gpsimd Engine generates DMA descriptors as part of the `nisa.dma_copy` instruction
  during NEFF execution.
- `nisa.dge_mode.hwdge`: Sync Engine or Scalar Engine sequencers invoke DGE hardware block to generate DMA
  descriptors as part of the `nisa.dma_copy` instruction during NEFF execution.

See `Trainium2 arch guide` and `Introduction to DMA with NKI` for more discussion.

When either `sw_dge` or `hw_dge` mode is used, the `src` and `dst` tensors can have a dynamic start address
which depends on a variable that cannot be resolved at compile time. When `sw_dge` is selected, `nisa.dma_copy`
can also perform a gather or scatter operation, using a list of dynamic indices from SBUF.
In both of these dynamic modes, out-of-bound address checking is turned on automatically during execution.
By default a runtime error is raised (`oob_mode=oob_mode.error` as default setting).
Developers can disable this error and make the `nisa.dma_copy` instruction skip the DMA transfer for a given dynamic
address or index when it is out of bound using `oob_mode=oob_mode.skip`.

**Memory types.**

Both `src` and `dst` tiles can be in HBM or SBUF. However, if both tiles are in SBUF, consider using an alternative
for better performance:

- nisa.tensor_copy for direct copies
- nisa.nc_n_gather to gather elements within each partition independently
- nisa.local_gather to gather elements within groups of partitions

**Data types.**

Both `src` and `dst` tiles can be any supported NKI data types (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).

The DMA engines automatically handle data type conversion when `src` and `dst` have different data types.
The conversion is performed through a two-step process: first casting from `src.dtype` to float32, then
from float32 to `dst.dtype`.
See nki-type-casting for more information about type casting behavior.

**Tile size.**

The total number of data elements in `src` must match that of `dst`.

**Indirect addressing (gather/scatter).**

`nisa.dma_copy` supports indirect addressing for dynamic row selection at runtime. This enables
gather (read from dynamic rows) and scatter (write to dynamic rows) patterns. Indirect addressing
is activated by calling `.ap()` on `src` or `dst` with a `vector_offset` or `scalar_offset`
parameter.

There are two types of indirect addressing:

*Vector indirection* provides per-partition dynamic offsets. Each of the hardware partitions
gets its own index, enabling gather/scatter where different partitions access different rows.
Use `.ap(pattern=..., vector_offset=idx_tensor, indirect_dim=0)` where `idx_tensor` is an
SBUF tensor of shape `(P, 1)` containing one row index per partition.
The tensor being indexed (the one `.ap()` is called on) must be in HBM.

*Scalar indirection* provides a single dynamic offset applied uniformly to all partitions.
Use `.ap(pattern=..., scalar_offset=reg_or_tensor, indirect_dim=N)` where the offset is
either a 1x1 SBUF tensor or a `VirtualRegister` from `nisa.register_alloc()`.

`vector_offset` and `scalar_offset` are mutually exclusive.

**DMA Batching (2D vector_offset).**

The vector_offset tensor can also have shape `(P, M)` with `M > 1`.
The hardware reads the index tensor in column-major order
(`indices.T.flatten()`) and writes the resulting rows to `dst` in linear
(row-major) order.

DMA Batching has the following hardware-imposed restrictions:

#. The 2D vector_offset must be on `src` (gather); a 2D vector_offset on `dst`
   (multi-column scatter) is not supported.
#. When `M > 1`, `P` must be exactly 128
#. Both `src` and `dst` tensors must be contiguous in memory.

**Indirect gather example** (`vector_offset` on `src`):

```python
import nki
import nki.isa as nisa
import nki.language as nl

@nki.jit
def indirect_gather_kernel(data, indices):
    P, F = indices.shape[0], data.shape[1]
    output = nl.ndarray((P, F), dtype=data.dtype, buffer=nl.shared_hbm)

    idx = nl.ndarray((P, 1), dtype=nl.uint32, buffer=nl.sbuf)
    nisa.dma_copy(dst=idx, src=indices)

    dst = nl.ndarray((P, F), dtype=data.dtype, buffer=nl.sbuf)
    nisa.dma_copy(
        dst=dst,
        src=data.ap(
            pattern=[[F, P], [1, F]],
            vector_offset=idx,
            indirect_dim=0,
        ),
    )

    nisa.dma_copy(dst=output, src=dst)
    return output

```

**Indirect scatter example** (`vector_offset` on `dst`):

```python
import nki
import nki.isa as nisa
import nki.language as nl

@nki.jit
def indirect_scatter_kernel(src_data, indices, output):
    P, F = src_data.shape

    src = nl.ndarray((P, F), dtype=src_data.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=src, src=src_data)

    idx = nl.ndarray((P, 1), dtype=nl.uint32, buffer=nl.sbuf)
    nisa.dma_copy(dst=idx, src=indices)

    nisa.dma_copy(
        dst=output.ap(
            pattern=[[F, P], [1, F]],
            vector_offset=idx,
            indirect_dim=0,
        ),
        src=src,
    )
    return output

```

- **dst** — the destination tensor to copy data into
- **src** — the source tensor to copy data from
- **priority** — (optional): DMA quality-of-service priority level 0-3 where lower is higher priority (NeuronCore-v4+ only)
- **dge_mode** — (optional) specify which Descriptor Generation Engine (DGE) mode to use for DMA descriptor generation: `nki.isa.dge_mode.none` (turn off DGE) or `nki.isa.dge_mode.swdge` (software DGE) or `nki.isa.dge_mode.hwdge` (hardware DGE)  or `nki.isa.dge_mode.unknown` (by default, let compiler select the best DGE mode). Hardware based DGE is only supported for NeuronCore-v3 or newer. See [Trainium2 arch guide](../../architecture/trainium2_arch.md) for more information.
- **oob_mode** — (optional) Specifies how to handle out-of-bounds (oob) array indices during indirect access operations. Valid modes are:

    - `oob_mode.error`: (Default) Raises an error when encountering out-of-bounds indices.
    - `oob_mode.skip`: Silently skips any operations involving out-of-bounds indices.

    For example, when using indirect gather/scatter operations, out-of-bounds indices can occur if the index array contains values that exceed the dimensions of the target array.

- **engine** — (optional) the engine to use for HWDGE descriptor generation: `nki.isa.engine.sync` or `nki.isa.engine.scalar`.
               Only valid when `dge_mode=nisa.dge_mode.hwdge`. `nki.isa.engine.unknown` by default.

---

### nki.isa.dma_transpose {#nki-isa-dma_transpose}

`nki.isa.dma_transpose(dst, src, axes, priority, dge_mode, oob_mode, name)`

**Engine:** DMA Engine

**Signature:**
```python
isa.dma_transpose(dst, src, axes=None, priority=None, dge_mode=dge_mode.unknown, oob_mode=oob_mode.error, name=None)
```

Perform a transpose on input `src` using DMA Engine.

The permutation of transpose follow the rules described below:

1. For 2-d input tile, the permutation will be [1, 0]
2. For 3-d input tile, the permutation will be [2, 1, 0]
3. For 4-d input tile, the permutation will be [3, 1, 2, 0]

**DMA Direct Transpose Constraints**

The only valid `dge_mode` s are `unknown` and `hwdge`. If `hwdge`, this instruction will be lowered
to a Hardware DGE transpose. This has additional restrictions:

1. For 2-d input tiles, `src.shape[0] == 16`.
   For 3-d/4-d input tiles, `src.shape[0]` must be in `{1, 2, 4, 8, 16}`
   and `(src.shape[0] * src.shape[-2]) % 16 == 0`.
2. `src.shape[-1] <= 128`
3. `src.dtype` is 2 bytes

**DMA Indirect Transpose Constraints**

The only valid `dge_mode` s are `unknown` and `swdge`. This instruction will be lowered
to a Software DGE transpose (`dma_gather_transpose`). This has additional restrictions:

#. When `src` is 4D: `len(src[1])` or `len(src[2])` must be 1
#. `src.shape[-1] <= 128`
#. `src.dtype` is 2 bytes
#. `src` tensor must be on HBM
#. `indices` must be 2-d
#. `indices.shape[0] * indices.shape[1]` must be `>=` `src.shape[0]`
#. `src.shape[0]` must be divisible by 16
#. `indices.shape[0]` must be in `[16, 128]` and divisible by 16
#. When `indices.shape[1] > 1`: `indices.shape[0]` must be exactly 128
#. `indices.dtype` is `np.uint32`
#. `indices` tensor must be on SBUF
#. TRN2+ only

Indirect transpose effectively performs the following operation:
`flat_indices = indices.T.flatten()[:src.shape[0]]`
`gathered = src[flat_indices, :]`
`dst = gathered.T`

**Indirect transpose example with 1D indices** (`indices.shape=[128, 1]`):

```python
import nki
import nki.isa as nisa
import nki.language as nl

@nki.jit
def gather_transpose_kernel(src_hbm, idx_hbm):
    P, F = 128, 128
    output = nl.ndarray((P, F), dtype=src_hbm.dtype, buffer=nl.shared_hbm)

    idx_sb = nl.load(idx_hbm)

    dst_sb = nl.ndarray((P, F), dtype=src_hbm.dtype, buffer=nl.sbuf)
    nisa.memset(dst=dst_sb, value=0)

    src_ap = src_hbm.ap(
        pattern=[[P, F], [1, P]],
        vector_offset=idx_sb,
        indirect_dim=0,
    )
    nisa.dma_transpose(dst=dst_sb, src=src_ap, axes=(1, 0))

    nisa.dma_copy(dst=output, src=dst_sb)
    return output

```

**Indirect transpose example with 2D indices** (`indices.shape=[128, N]` where N > 1):

```python
@nki.jit
def gather_transpose_2d_kernel(src_hbm, idx_hbm):
    '''Gather-transpose with 2D indices [128, N] to handle larger gather sets.

    When indices.shape[1] > 1, indices.shape[0] must be exactly 128.
    Total elements gathered = 128 * N (up to src.shape[0]).

    Hardware uses column-major flattening: flat_indices = indices.T.flatten()
    '''
    N_COLS = 2  # Number of columns in index tensor
    P = 128  # Partition dimension (max 128)
    F = 128 * N_COLS  # Free dimension: 256

    output = nl.ndarray((P, F), dtype=src_hbm.dtype, buffer=nl.shared_hbm)

    idx_sb = nl.load(idx_hbm)

    dst_sb = nl.ndarray((P, F), dtype=src_hbm.dtype, buffer=nl.sbuf)
    nisa.memset(dst=dst_sb, value=0)

    src_ap = src_hbm.ap(
        pattern=[[P, F], [1, P]],
        vector_offset=idx_sb,
        indirect_dim=0,
    )
    nisa.dma_transpose(dst=dst_sb, src=src_ap, axes=(1, 0))

    nisa.dma_copy(dst=output, src=dst_sb)
    return output

```

**4D indirect transpose example with 2D indices**:

```python
@nki.jit
def gather_transpose_4d_kernel(src_hbm, idx_hbm):
    '''4D gather-transpose with 2D indices [128, N].

    Pattern: [[d1*d2*d3, F], [d2*d3, d1], [d3, d2], [1, d3]]
    '''
    T, d1, d2, d3 = src_hbm.shape
    _, N = idx_hbm.shape
    F = 128 * N

    idx_sb = nl.load(idx_hbm)

    dst_sb = nl.ndarray((d3, d1, d2, F), dtype=src_hbm.dtype, buffer=nl.sbuf)
    nisa.memset(dst=dst_sb, value=0)

    src_ap = src_hbm.ap(
        pattern=[[d1 * d2 * d3, F], [d2 * d3, d1], [d3, d2], [1, d3]],
        vector_offset=idx_sb,
        indirect_dim=0,
    )

    nisa.dma_transpose(dst=dst_sb, src=src_ap, axes=(3, 1, 2, 0))

    output = nl.ndarray((d3, d1, d2, F), dtype=src_hbm.dtype, buffer=nl.shared_hbm)
    nisa.dma_copy(dst=output, src=dst_sb)

    return output

```

- **dst** — the destination of transpose, must be a tile in SBUF.
- **src** — the source of transpose, must be a tile in HBM or SBUF. `src.dtype == dst.dtype`
- **axes** — transpose axes where the i-th axis of the transposed tile will correspond to the axes[i] of the source.
             Supported axes are `(1, 0)`, `(2, 1, 0)`, and `(3, 1, 2, 0)`.
- **priority** — (optional): DMA quality-of-service priority level 0-3 where lower is higher priority (NeuronCore-v4+ only)
- **dge_mode** — (optional) specify which Descriptor Generation Engine (DGE) mode to use for DMA descriptor generation: `nki.isa.dge_mode.none` (turn off DGE) or `nki.isa.dge_mode.swdge` (software DGE) or `nki.isa.dge_mode.hwdge` (hardware DGE)  or `nki.isa.dge_mode.unknown` (by default, let compiler select the best DGE mode). Hardware based DGE is only supported for NeuronCore-v3 or newer. See [Trainium2 arch guide](../../architecture/trainium2_arch.md) for more information.
- **oob_mode** — (optional) Specifies how to handle runtime out-of-bounds (oob) array indices during indirect access operations. Valid modes are:

    - `oob_mode.error`: (Default) Raises an error when encountering runtime out-of-bounds indices.

    - `oob_mode.skip`: Silently skips any operations involving out-of-bounds indices. Only valid when `src` uses indirect indexing.

---

### nki.isa.local_gather {#nki-isa-local_gather}

`nki.isa.local_gather(dst, src_buffer, index, num_elem_per_idx, num_valid_indices, name)`

**Engine:** GpSimd Engine

**Signature:**
```python
isa.local_gather(dst, src_buffer, index, num_elem_per_idx=1, num_valid_indices=None, name=None)
```

Gather SBUF data in `src_buffer` using `index` on GpSimd Engine.

Each of the eight GpSimd cores in GpSimd Engine connects to 16 contiguous SBUF partitions
(e.g., core[0] connected to partition[0:16]) and performs gather from the connected 16
SBUF partitions *independently* in parallel. The indices used for gather on each core should also
come from the same 16 connected SBUF partitions. If you only need to gather elements within a partition,
consider using nisa.nc_n_gather instead, which supports gathering more indices.

During execution of the instruction, each GpSimd core reads a 16-partition slice from `index`, flattens
all indices into a 1D array `indices_1d` (along the partition dimension first).
By default with no `num_valid_indices` specified, each GpSimd core
will treat all indices from its corresponding 16-partition `index` slice as valid indices.
However, when the number of valid indices per core
is not a multiple of 16, users can explicitly specify the valid index count per core in `num_valid_indices`.
Note, `num_valid_indices` must not exceed the total element count in each 16-partition `index` slice
(i.e., `num_valid_indices <= index.size / (index.shape[0] / 16)`).

Next, each GpSimd core uses the flattened `indices_1d` indices as *partition offsets* to gather from
the connected 16-partition slice of `src_buffer`. Optionally, this API also allows gathering of multiple
contiguous elements starting at each index to improve gather throughput, as indicated by `num_elem_per_idx`.
Behavior of out-of-bound index access is undefined.

Even though all eight GpSimd cores can gather with completely different indices, a common use case for
this API is to make all cores gather with the same set of indices (i.e., partition offsets). In this case,
users can generate indices into 16 partitions, replicate them eight times to 128 partitions and then feed them into
`local_gather`.

As an example, if `src_buffer` is (128, 512) in shape and `index` is (128, 4) in shape, where the partition
dimension size is 128, `local_gather` effectively performs the following operation:

```python
num_gpsimd_cores = 8
num_partitions_per_core = 16

src_buffer = np.random.random_sample([128, 512, 4]).astype(np.float32) * 100
index_per_core = np.random.randint(low=0, high=512, size=(16, 4), dtype=np.uint16)
# replicate 8 times for 8 GpSimd cores
index = np.tile(index_per_core, (num_gpsimd_cores, 1))
num_elem_per_idx = 4
index_hw = index * num_elem_per_idx
num_valid_indices = 64
output_shape = (128, 4, 16, 4)

num_active_cores = index.shape[0] / num_partitions_per_core
num_valid_indices = num_valid_indices if num_valid_indices \
  else index.size / num_active_cores

output_np = np.ndarray(shape=(128, num_valid_indices, num_elem_per_idx),
                       dtype=src_buffer.dtype)

for i_core in range(num_gpsimd_cores):
  start_par = i_core * num_partitions_per_core
  end_par = (i_core + 1) * num_partitions_per_core
  indices_1d = index[start_par:end_par].flatten(order='F')[0: num_valid_indices]

  output_np[start_par:end_par, :, :] = np.take(
    src_buffer[start_par:end_par],
    indices_1d, axis=1)

output_np = output_np.reshape(output_shape)

```

`local_gather` preserves the input data types from `src_buffer` in the gather output.
Therefore, no data type casting is allowed in this API. The indices in `index` tile must be uint16 types.

This API has three tile size constraints [subject to future relaxation]:

#. The partition axis size of `src_buffer` must match that of `index` and must
   be a multiple of 16. In other words, `src_buffer.shape[0] == index.shape[0] and src_buffer.shape[0] % 16 == 0`.
#. The number of contiguous elements to gather per index per partition `num_elem_per_idx`
   must be one of the following values: `[1, 2, 4, 8, 16, 32]`.
#. The number of indices for gather per core must be less than or equal to 4096.
#. `src_buffer` and `dst` can have at most four free dimensions (maximum
   rank 5, including the partition dimension).

- **dst** — an output tile of the gathered data
- **src_buffer** — an input tile for gathering.
- **index** — an input tile with indices used for gathering.
- **num_elem_per_idx** — an optional integer value to read multiple contiguous elements per index per partition; default is 1.
- **num_valid_indices** — an optional integer value to specify the number of valid indices per GpSimd core; default is
                          `index.size / (index.shape[0] / 16)`.

Click [here](../../test/test_nki_isa_local_gather.py) to download the
full NKI code example with equivalent numpy implementation.

---

### nki.isa.memset {#nki-isa-memset}

`nki.isa.memset(dst, value, engine, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.memset(dst, value, engine=engine.unknown, name=None)
```

Initialize `dst` by filling it with a compile-time constant `value`, using Vector or GpSimd Engine.
The memset instruction supports all valid NKI dtypes (see [Supported Data Types](nki.api.shared.md#nki-dtype)).
`dst` can have at most four free dimensions (maximum rank 5, including the
partition dimension).

- **dst** — destination tile to initialize.
- **value** — the constant value to initialize with
- **engine** — specify which engine to use for memset: `nki.isa.engine.vector` or `nki.isa.engine.gpsimd` ;
               `nki.isa.engine.unknown` by default, lets compiler select the best engine for the given
               input tile shape

> **Note:**
> For x4 packed types (`float8_e4m3fn_x4`, `float8_e5m2_x4`,
> `float4_e2m1fn_x4`), only `value=0` is supported.

---

### nki.isa.nc_transpose {#nki-isa-nc_transpose}

`nki.isa.nc_transpose(dst, data, engine, name)`

**Engine:** Tensor Engine

**Signature:**
```python
isa.nc_transpose(dst, data, engine=engine.unknown, name=None)
```

Perform a 2D transpose between the partition axis and the free axis of input `data` using Tensor or Vector Engine.

If the `data` tile has more than one free axis, this API implicitly flattens all free axes into one axis
and then performs a 2D transpose.

2D transpose on Tensor Engine is implemented by performing a matrix multiplication between `data` as the
stationary tensor and an identity matrix as the moving tensor. This is equivalent to calling `nisa.nc_matmul`
directly with `is_transpose=True`. See architecture guide
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
Vector Engine supports up to four free dimensions in `data` (maximum rank 5, including the partition dimension).
Tensor Engine supports up to three free dimensions in `data` (maximum rank 4).
`dst` supports up to one free dimension (maximum rank 2).
If no `engine` is specified, Neuron Compiler will automatically select an engine
based on the input shape.

- **dst** — the transpose output
- **data** — the input tile to be transposed
- **engine** — specify which engine to use for transpose: `nki.isa.engine.tensor` or `nki.isa.engine.vector`;
               by default, the best engine will be selected for the given input tile shape

---

### nki.isa.tensor_copy {#nki-isa-tensor_copy}

`nki.isa.tensor_copy(dst, src, engine, name)`

**Engine:** Vector Engine

**Signature:**
```python
isa.tensor_copy(dst, src, engine=engine.unknown, name=None)
```

Create a copy of `src` tile within NeuronCore on-chip SRAMs using Vector, Scalar or GpSimd Engine.

The output tile has the same partition axis size and also the same number of elements per partition
as the input tile `src`.

All three compute engines, Vector, Scalar and GpSimd Engine can perform tensor copy. However, their copy behavior
is slightly different across engines:

- Scalar Engine on NeuronCore-v2 performs copy by first casting the input tile to FP32 internally and then casting from
  FP32 to `dst.dtype`. Users should be cautious with assigning this instruction to Scalar Engine when the input data
  type cannot be precisely cast to FP32 (e.g., INT32).
- Both GpSimd and Vector Engine can operate in two modes: (1) bit-accurate copy when input and output data types are
  the same or (2) intermediate FP32 cast when input and output data types differ, similar to Scalar Engine.

See nki-type-casting for more information about type casting behavior.

In addition, since GpSimd Engine cannot access PSUM in NeuronCore, Scalar or Vector Engine must be chosen when the input or
output tile is in PSUM (see [NeuronCore-v2 Compute Engines](../../architecture/trainium_inferentia2_arch.md#arch-sec-neuron-core-engines) for details). By default, this API returns
a tile in SBUF, unless the returned value is assigned to a pre-declared PSUM tile.

On NeuronCore v2, `tensor_copy` is not supported on the Scalar Engine. Instead, use nisa.activation with `op=nl.copy`.

The `src` and `dst` tiles can have at most four free dimensions (maximum rank 5,
including the partition dimension).

**Tensor indirection.**

On NeuronCore-v4 and later, both `dst` and `src` support tensor indirection
(gather/scatter) by passing a view created with `.indirect(index)`. When tensor
indirection is used, the operation must run on the Vector or Scalar engine.

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

In addition, on the Scalar engine a scattered `dst` cannot be in PSUM.

- **dst** — a tile with the same content and partition axis size as the `src` tile.
- **src** — the source of copy, must be a tile in SBUF or PSUM.
- **engine** — (optional) the engine to use for the operation: `nki.isa.engine.vector`, `nki.isa.engine.scalar`,
              `nki.isa.engine.gpsimd` or `nki.isa.engine.unknown` (default, compiler selects best engine based on engine workload).

---

