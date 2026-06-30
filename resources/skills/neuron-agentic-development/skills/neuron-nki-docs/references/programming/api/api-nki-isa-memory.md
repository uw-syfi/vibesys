# NKI ISA - Memory Operations

> **Module**: nki.isa
> **Total Functions**: 5

## Overview

DMA and memory management instructions.

## Functions

### nki.isa.dma_compute {#nki-isa-dma_compute}

# nki.isa.dma_compute

nki.isa.dma_compute

nki.isa.dma_compute(*dst*, *srcs*, *reduce_op*, *scales=None*, *unique_indices=True*, *name=None*)[[source]](../../../_modules/nki/isa.html#dma_compute)
Perform math operations using compute logic inside DMA engines with element-wise scaling and reduction.

This instruction leverages the compute capabilities within DMA engines to perform scaled element-wise operations
followed by reduction across multiple source tensors. The computation follows the pattern:
`dst = reduce_op(srcs[0] * scales[0], srcs[1] * scales[1], ...)`, where each source tensor is first
multiplied by its corresponding scale factor, then all scaled results are combined using the specified
reduction operation.
Currently, only `nl.add` is supported for `reduce_op`, and
all values in `scales` must be `1.0`.

The DMA engines perform all computations in float32 precision internally. Input tensors are automatically
cast from their source data types to float32 before computation, and the final float32 result is cast
to the output data type in a pipelined fashion.

**Memory types.**

Both input `srcs` tensors and output `dst` tensor can be in HBM or SBUF.
Both `srcs` and `dst` tensors must have compile-time known addresses.

**Data types.**

All input `srcs` tensors and the output `dst` tensor can be any supported NKI data types
(see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information). The DMA engines automatically cast input data types to float32
before performing the scaled reduction computation. The float32 computation results are then cast to the
data type of `dst` in a pipelined fashion.

**Layout.**

The computation is performed element-wise across all tensors, with the reduction operation applied
across the scaled source tensors at each element position.

**Tile size.**

The element count of each tensor in `srcs` and `dst` must match exactly.
The max number of source tensors in `srcs` is 16.

Parameters:

* **dst** – the output tensor to store the computed results

* **srcs** – a list of input tensors to be scaled and reduced

* **reduce_op** – the reduction operation to apply (currently only `nl.add` is supported)

* **scales** – (optional) a list of scale factors corresponding to each tensor in `srcs` (must be [1.0, 1.0, …]); default is None

* **unique_indices** – (optional) whether the indices are unique; default is True

---

### nki.isa.dma_copy {#nki-isa-dma_copy}

# nki.isa.dma_copy

nki.isa.dma_copy

nki.isa.dma_copy(*dst*, *src*, *oob_mode=oob_mode.error*, *dge_mode=dge_mode.unknown*, *name=None*)[[source]](../../../_modules/nki/isa.html#dma_copy)
Copy data from `src` to `dst` using DMA engines.

This instruction performs data movement between memory locations (SBUF or HBM) using DMA engines. The basic operation
copies data from the source tensor to the destination tensor: `dst = src`.

**Important NKI 0.3.0 changes:**

* `dma_copy` no longer supports reading directly from PSUM. Copy the PSUM tensor to SBUF first using `nisa.tensor_copy`.
* The `dst_rmw_op` and `unique_indices` parameters have been removed. Use `nisa.dma_compute` for read-modify-write operations.
* When using `dge_mode=dge_mode.hwdge`, source and destination element types must match. Use `.view()` to reinterpret data as a different type before the copy.

`nisa.dma_copy` supports different modes of DMA descriptor generation (DGE):

* `nisa.dge_mode.none`: Neuron Runtime generates DMA descriptors and stores them into HBM before NEFF execution.

* `nisa.dge_mode.swdge`: Gpsimd Engine generates DMA descriptors as part of the `nisa.dma_copy` instruction
during NEFF execution.

* `nisa.dge_mode.hwdge`: Sync Engine or Scalar Engine sequencers invoke DGE hardware block to generate DMA
descriptors as part of the `nisa.dma_copy` instruction during NEFF execution.

See Trainium2 arch guide and Introduction to DMA with NKI for more discussion.

When either `sw_dge` or `hw_dge` mode is used, the `src` and `dst` tensors can have a dynamic start address
which depends on a variable that cannot be resolved at compile time. When `sw_dge` is selected, `nisa.dma_copy`
can also perform a gather or scatter operation, using a list of **unique** dynamic indices from SBUF.
In both of these dynamic modes, out-of-bound address checking is turned on automatically during execution.
By default a runtime error is raised (`oob_mode=oob_mode.error` as default setting).
Developers can disable this error and make the nisa.dma_copy instruction skips the DMA transfer for a given dynamic
address or index when it is out of bound using `oob_mode=oob_mode.skip`.

**Memory types.**

Both `src` and `dst` tiles can be in HBM or SBUF. However, if both tiles are in SBUF, consider using
[nisa.tensor_copy](nki.isa.tensor_copy.md) instead for better performance.

**Data types.**

Both `src` and `dst` tiles can be any supported NKI data types (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).

The DMA engines automatically handle data type conversion when `src` and `dst` have different data types.
The conversion is performed through a two-step process: first casting from `src.dtype` to float32, then
from float32 to `dst.dtype`.

**Tile size.**

The total number of data elements in `src` must match that of `dst`.

Parameters:

* **dst** – the destination tensor to copy data into (must be in SBUF or HBM; cannot be PSUM)

* **src** – the source tensor to copy data from (must be in SBUF or HBM; cannot be PSUM)

* **dge_mode** – (optional) specify which Descriptor Generation Engine (DGE) mode to use for DMA descriptor generation: `nki.isa.dge_mode.none` (turn off DGE) or `nki.isa.dge_mode.swdge` (software DGE) or `nki.isa.dge_mode.hwdge` (hardware DGE) or `nki.isa.dge_mode.unknown` (by default, let compiler select the best DGE mode). Hardware based DGE is only supported for NeuronCore-v3 or newer. When using `hwdge`, source and destination element types must match. See [Trainium2 arch guide](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/nki/arch/trainium2_arch.html) for more information.

* **oob_mode** – (optional) Specifies how to handle out-of-bounds (oob) array indices during indirect access operations. Valid modes are:

`oob_mode.error`: (Default) Raises an error when encountering out-of-bounds indices.

* `oob_mode.skip`: Silently skips any operations involving out-of-bounds indices.

For example, when using indirect gather/scatter operations, out-of-bounds indices can occur if the index array contains values that exceed the dimensions of the target array.

---

### nki.isa.dma_transpose {#nki-isa-dma_transpose}

# nki.isa.dma_transpose

nki.isa.dma_transpose

nki.isa.dma_transpose(*dst*, *src*, *axes=None*, *dge_mode=dge_mode.unknown*, *name=None*)[[source]](../../../_modules/nki/isa.html#dma_transpose)
Perform a transpose on input `src` using DMA Engine.

The permutation of transpose follow the rules described below:

* For 2-d input tile, the permutation will be [1, 0]

* For 3-d input tile, the permutation will be [2, 1, 0]

* For 4-d input tile, the permutation will be [3, 1, 2, 0]

`dst.shape` must match the transposed `src.shape` exactly, including rank. The compiler raises an assertion error if ranks differ.

The only valid `dge_mode` s are `unknown` and `hwdge`. If `hwdge`, this instruction will be lowered
to a Hardware DGE transpose. This has additional restrictions:

* `src.shape[0] == 16`

* `src.shape[-1] % 128 == 0` (relaxed to `<= 128` when `src` uses indirect access pattern with `vector_offset`)

* `dtype` is 2 bytes

Parameters:

* **dst** – the destination tile. Must have the same rank as the transposed `src`.

* **src** – the source of transpose, must be a tile in HBM or SBUF.

* **axes** – transpose axes where the i-th axis of the transposed tile will correspond to the axes[i] of the source.
Supported axes are `(1, 0)`, `(2, 1, 0)`, and `(3, 1, 2, 0)`.

* **dge_mode** – (optional) specify which Descriptor Generation Engine (DGE) mode to use for DMA descriptor generation: `nki.isa.dge_mode.none` (turn off DGE) or `nki.isa.dge_mode.swdge` (software DGE) or `nki.isa.dge_mode.hwdge` (hardware DGE) or `nki.isa.dge_mode.unknown` (by default, let compiler select the best DGE mode). Hardware based DGE is only supported for NeuronCore-v3 or newer. See [Trainium2 arch guide](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/nki/arch/trainium2_arch.html) for more information.

---

### nki.isa.local_gather {#nki-isa-local_gather}

# nki.isa.local_gather

nki.isa.local_gather

nki.isa.local_gather(*dst*, *src_buffer*, *index*, *num_elem_per_idx=1*, *num_valid_indices=None*, *name=None*)[[source]](../../../_modules/nki/isa.html#local_gather)
Gather SBUF data in `src_buffer` using `index` on GpSimd Engine.

Each of the eight GpSimd cores in GpSimd Engine connects to 16 contiguous SBUF partitions
(e.g., core[0] connected to partition[0:16]) and performs gather from the connected 16
SBUF partitions *independently* in parallel. The indices used for gather on each core should also
come from the same 16 connected SBUF partitions.

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

* The partition axis size of `src_buffer` must match that of `index` and must
be a multiple of 16. In other words, `src_buffer.shape[0] == index.shape[0] and src_buffer.shape[0] % 16 == 0`.

* The number of contiguous elements to gather per index per partition `num_elem_per_idx`
must be one of the following values: `[1, 2, 4, 8, 16, 32]`.

* The number of indices for gather per core must be less than or equal to 4096.

Parameters:

* **dst** – an output tile of the gathered data

* **src_buffer** – an input tile for gathering.

* **index** – an input tile with indices used for gathering.

* **num_elem_per_idx** – an optional integer value to read multiple contiguous elements per index per partition; default is 1.

* **num_valid_indices** – an optional integer value to specify the number of valid indices per GpSimd core; default is
`index.size / (index.shape[0] / 16)`.

Click [`here`](../../downloads/test_nki_isa_local_gather.py) to download the
full NKI code example with equivalent numpy implementation.

---

### nki.isa.memset {#nki-isa-memset}

# nki.isa.memset

nki.isa.memset

nki.isa.memset(*dst*, *value*, *engine=engine.unknown*, *name=None*)[[source]](../../../_modules/nki/isa.html#memset)
Initialize `dst` by filling it with a compile-time constant `value`, using Vector or GpSimd Engine.
The memset instruction supports all valid NKI dtypes (see [Supported Data Types](nki.api.shared.md#nki-dtype)).

Parameters:

* **dst** – destination tile to initialize.

* **value** – the constant value to initialize with

* **engine** – specify which engine to use for memset: `nki.isa.vector_engine` or `nki.isa.gpsimd_engine` ;
`nki.isa.unknown_engine` by default, lets compiler select the best engine for the given
input tile shape

---
