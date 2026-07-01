# API Symbol Lookup Index

Quick reference for finding NKI API function and symbol documentation. Symbols are organized alphabetically within their respective modules.

---

## Quick Module Reference

| Module | Description | Documentation |
|--------|-------------|---------------|
| `nki` | Top-level NKI module | [nki](../programming/api/nki.md) |
| `nki.language` | High-level language APIs | [nki.language](../programming/api/nki.language.md) |
| `nki.isa` | Low-level ISA instructions | [nki.isa](../programming/api/nki.isa.md) |
| `nki.api.shared` | Shared data types and operators | [nki.api.shared](../programming/api/nki.api.shared.md) |

---

## A

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `abs_max` | nki.language | Element-wise absolute maximum (trn3 only) | [nki.api.shared](../programming/api/nki.api.shared.md) |
| `abs_min` | nki.language | Element-wise absolute minimum (trn3 only) | [nki.api.shared](../programming/api/nki.api.shared.md) |
| `activate2` | nki.isa | Two-stage tensor-scalar + activation in one instruction (trn3 only) | [nki.isa.activate2](../programming/api/api-nki-isa-scalar.md#nki-isa-activation) |
| `activation` | nki.isa | Apply activation function with optional scale/bias | [nki.isa.activation](../programming/api/api-nki-isa-scalar.md#nki-isa-activation) |
| `activation_reduce` | nki.isa | Activation with free-dimension reduction | [nki.isa.activation_reduce](../programming/api/api-nki-isa-scalar.md#nki-isa-activation_reduce) |
| `affine_range` | nki.language | Loop iterator (legacy alias for `range`) | [nki.language.affine_range](../programming/api/nki.language.md) |
| `affine_select` | nki.isa | Select elements using affine predicate | [nki.isa.affine_select](../programming/api/api-nki-isa-utility.md#nki-isa-affine_select) |

---

## B

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `bfloat16` | nki.language | BF16 data type (1S,8E,7M) | [nki.language.bfloat16](../programming/api/api-nki-language-types.md) |
| `bn_aggr` | nki.isa | Aggregate batch norm statistics | [nki.isa.bn_aggr](../programming/api/api-nki-isa-vector.md#nki-isa-bn_aggr) |
| `bn_stats` | nki.isa | Compute batch norm statistics | [nki.isa.bn_stats](../programming/api/api-nki-isa-vector.md#nki-isa-bn_stats) |
| `bool_` | nki.language | Boolean data type | [nki.language.bool_](../programming/api/api-nki-language-types.md) |

---

## C

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `core_barrier` | nki.isa | Synchronize across NeuronCores | [nki.isa.core_barrier](../programming/api/api-nki-isa-tensor.md#nki-isa-core_barrier) |

---

## D

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `device_print` | nki.language | Print debug output from kernel | [nki.language.device_print](../programming/api/nki.language.md) |
| `dge_mode` | nki.isa | DMA Descriptor Generation Engine mode enum | [nki.isa.dge_mode](../programming/api/nki.isa.md) |
| `dma_compute` | nki.isa | Math operations using DMA engines (replaces dma_copy RMW) | [nki.isa.dma_compute](../programming/api/api-nki-isa-memory.md#nki-isa-dma_compute) |
| `dma_copy` | nki.isa | Copy data using DMA engines | [nki.isa.dma_copy](../programming/api/api-nki-isa-memory.md#nki-isa-dma_copy) |
| `dma_engine` | nki.isa | DMA engine enum (dma, gpsimd_dma) | [nki.isa.dma_engine](../programming/api/nki.isa.md) |
| `dma_transpose` | nki.isa | Transpose using DMA engines | [nki.isa.dma_transpose](../programming/api/api-nki-isa-memory.md#nki-isa-dma_transpose) |
| `dropout` | nki.isa | Apply dropout to tensor | [nki.isa.dropout](../programming/api/api-nki-isa-scalar.md#nki-isa-dropout) |
| `ds` | nki.language | Dynamic slice for tensor indexing | [nki.language.ds](../programming/api/nki.language.md) |

---

## E

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `engine` | nki.isa | Neuron Device engine enum | [nki.isa.engine](../programming/api/nki.isa.md) |
| `exponential` | nki.isa | Dedicated exponential instruction (Trn3/NeuronCore-v4 only) | [nki.isa.exponential](../programming/api/nki.isa.md) |

---

## F

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `float16` | nki.language | FP16 data type | [nki.language.float16](../programming/api/api-nki-language-types.md) |
| `float32` | nki.language | FP32 data type | [nki.language.float32](../programming/api/api-nki-language-types.md) |
| `float4_e2m1fn_x4` | nki.language | 4x packed float4 for MXFP matmul | [nki.language.float4_e2m1fn_x4](../programming/api/api-nki-language-types.md) |
| `float8_e4m3` | nki.language | FP8 E4M3 data type | [nki.language.float8_e4m3](../programming/api/api-nki-language-types.md) |
| `float8_e4m3fn_x4` | nki.language | 4x packed FP8 E4M3 for MXFP matmul | [nki.language.float8_e4m3fn_x4](../programming/api/api-nki-language-types.md) |
| `float8_e5m2` | nki.language | FP8 E5M2 data type | [nki.language.float8_e5m2](../programming/api/api-nki-language-types.md) |
| `float8_e5m2_x4` | nki.language | 4x packed FP8 E5M2 for MXFP matmul | [nki.language.float8_e5m2_x4](../programming/api/api-nki-language-types.md) |

---

## G

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `get_nc_version` | nki.isa | Get NeuronCore version | [nki.isa.get_nc_version](../programming/api/api-nki-isa-tensor.md#nki-isa-get_nc_version) |

---

## H

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `hbm` | nki.language | HBM memory buffer (alias of private_hbm) | [nki.language.hbm](../programming/api/api-nki-language-memory.md#nki-language-hbm) |

---

## I

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `int8` | nki.language | 8-bit signed integer | [nki.language.int8](../programming/api/api-nki-language-types.md) |
| `int16` | nki.language | 16-bit signed integer | [nki.language.int16](../programming/api/api-nki-language-types.md) |
| `int32` | nki.language | 32-bit signed integer | [nki.language.int32](../programming/api/api-nki-language-types.md) |
| `iota` | nki.isa | Generate constant literal pattern | [nki.isa.iota](../programming/api/api-nki-isa-utility.md#nki-isa-iota) |

---

## L

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `local_gather` | nki.isa | Gather SBUF data using indices | [nki.isa.local_gather](../programming/api/api-nki-isa-memory.md#nki-isa-local_gather) |

---

## M

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `max8` | nki.isa | Find 8 largest values per partition | [nki.isa.max8](../programming/api/api-nki-isa-utility.md#nki-isa-max8) |
| `memset` | nki.isa | Initialize tensor with constant value | [nki.isa.memset](../programming/api/api-nki-isa-memory.md#nki-isa-memset) |

---

## N

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `nc_find_index8` | nki.isa | Find indices of 8 values in data | [nki.isa.nc_find_index8](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_find_index8) |
| `nc_match_replace8` | nki.isa | Replace values and optionally return indices | [nki.isa.nc_match_replace8](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_match_replace8) |
| `nc_matmul` | nki.isa | Matrix multiplication on Tensor Engine | [nki.isa.nc_matmul](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_matmul) |
| `nc_matmul_mx` | nki.isa | MXFP quantized matrix multiplication | [nki.isa.nc_matmul_mx](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_matmul_mx) |
| `nc_n_gather` | nki.isa | Gather elements using indices | [nki.isa.nc_n_gather](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_n_gather) |
| `nc_stream_shuffle` | nki.isa | Cross-partition data shuffle | [nki.isa.nc_stream_shuffle](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_stream_shuffle) |
| `nc_transpose` | nki.isa | 2D transpose between P and F axes | [nki.isa.nc_transpose](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_transpose) |
| `nc_version` | nki.isa | NeuronCore version enum | [nki.isa.nc_version](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_version) |
| `ndarray` | nki.language | Create tensor on specified buffer | [nki.language.ndarray](../programming/api/nki.language.md) |
| `nonzero_with_count` | nki.isa | Find indices of nonzero elements and count (NeuronCore-v3+) | [nki.isa.nonzero_with_count](../programming/api/api-nki-isa-utility.md#nki-isa-nonzero_with_count) |
| `num_programs` | nki.language | Number of SPMD programs in grid | [nki.language.num_programs](../programming/api/nki.language.md) |

---

## P

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `private_hbm` | nki.language | Private HBM memory buffer | [nki.language.private_hbm](../programming/api/api-nki-language-memory.md#nki-language-private_hbm) |
| `program_id` | nki.language | Index of current SPMD program | [nki.language.program_id](../programming/api/nki.language.md) |
| `program_ndim` | nki.language | Number of dimensions in SPMD grid | [nki.language.program_ndim](../programming/api/nki.language.md) |
| `psum` | nki.language | PSUM memory buffer | [nki.language.psum](../programming/api/api-nki-language-memory.md#nki-language-psum) |

---

## Q

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `quantize_mx` | nki.isa | Quantize to MXFP8 format | [nki.isa.quantize_mx](../programming/api/api-nki-isa-tensor.md#nki-isa-quantize_mx) |

---

## R

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `rand2` | nki.isa | Generate uniform random numbers | [nki.isa.rand2](../programming/api/nki.isa.md) |
| `rand_get_state` | nki.isa | Get PRNG state from engine | [nki.isa.rand_get_state](../programming/api/nki.isa.md) |
| `rand_set_state` | nki.isa | Set PRNG state in engine | [nki.isa.rand_set_state](../programming/api/nki.isa.md) |
| `range_select` | nki.isa | Select elements based on range comparison | [nki.isa.range_select](../programming/api/api-nki-isa-utility.md#nki-isa-range_select) |
| `reciprocal` | nki.isa | Compute element-wise 1/x | [nki.isa.reciprocal](../programming/api/api-nki-isa-scalar.md#nki-isa-reciprocal) |
| `reduce_cmd` | nki.isa | Engine register reduce commands enum | [nki.isa.reduce_cmd](../programming/api/nki.isa.md) |
| `register_alloc` | nki.isa | Allocate virtual register | [nki.isa.register_alloc](../programming/api/nki.isa.md) |
| `register_load` | nki.isa | Load scalar from memory to register | [nki.isa.register_load](../programming/api/nki.isa.md) |
| `register_move` | nki.isa | Move value from source register to destination register | [nki.isa.register_move](../programming/api/nki.isa.md) |
| `register_store` | nki.isa | Store register value to memory | [nki.isa.register_store](../programming/api/nki.isa.md) |
| `rng` | nki.isa | Generate pseudo random numbers | [nki.isa.rng](../programming/api/nki.isa.md) |

---

## S

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `simulate` | nki | Run NKI kernel on CPU without NeuronDevice (experimental) | [nki.simulate](../programming/api/api-nki-tools.md#nki-simulate) |
| `sbuf` | nki.language | State Buffer memory | [nki.language.sbuf](../programming/api/api-nki-language-memory.md#nki-language-sbuf) |
| `scalar_tensor_tensor` | nki.isa | Two-op sequence with scalar broadcast | [nki.isa.scalar_tensor_tensor](../programming/api/api-nki-isa-tensor.md#nki-isa-scalar_tensor_tensor) |
| `select_reduce` | nki.isa | Conditional copy with optional reduction | [nki.isa.select_reduce](../programming/api/api-nki-isa-utility.md#nki-isa-select_reduce) |
| `sendrecv` | nki.isa | Point-to-point NeuronCore communication | [nki.isa.sendrecv](../programming/api/nki.isa.md) |
| `sequence_bounds` | nki.isa | Compute sequence bounds from segment IDs | [nki.isa.sequence_bounds](../programming/api/api-nki-isa-utility.md#nki-isa-sequence_bounds) |
| `sequential_range` | nki.language | Loop iterator (legacy alias for `range`) | [nki.language.sequential_range](../programming/api/nki.language.md) |
| `set_rng_seed` | nki.isa | Seed Vector Engine PRNG | [nki.isa.set_rng_seed](../programming/api/nki.isa.md) |
| `shared_hbm` | nki.language | Shared HBM across kernel instances | [nki.language.shared_hbm](../programming/api/api-nki-language-memory.md#nki-language-shared_hbm) |
| `static_range` | nki.language | Loop iterator (legacy alias for `range`) | [nki.language.static_range](../programming/api/nki.language.md) |

---

## T

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `tensor_copy` | nki.isa | Copy tensor within on-chip SRAM | [nki.isa.tensor_copy](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_copy) |
| `tensor_copy_predicated` | nki.isa | Conditional element copy | [nki.isa.tensor_copy_predicated](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_copy_predicated) |
| `tensor_partition_reduce` | nki.isa | Reduce across partitions | [nki.isa.tensor_partition_reduce](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_partition_reduce) |
| `tensor_reduce` | nki.isa | Reduce along free axes | [nki.isa.tensor_reduce](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_reduce) |
| `tensor_scalar` | nki.isa | Tensor-scalar operations with broadcasting | [nki.isa.tensor_scalar](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_scalar) |
| `tensor_scalar_cumulative` | nki.isa | Tensor-scalar with cumulative reduction | [nki.isa.tensor_scalar_cumulative](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_scalar_cumulative) |
| `tensor_scalar_reduce` | nki.isa | Tensor-scalar with free-dim reduction | [nki.isa.tensor_scalar_reduce](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_scalar_reduce) |
| `tensor_tensor` | nki.isa | Element-wise operation on two tensors | [nki.isa.tensor_tensor](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_tensor) |
| `tensor_tensor_scan` | nki.isa | Scan operation on two tensors | [nki.isa.tensor_tensor_scan](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_tensor_scan) |
| `tfloat32` | nki.language | TF32 data type (1S,8E,10M) | [nki.language.tfloat32](../programming/api/api-nki-language-types.md) |
| `tile_size` | nki.language | Tile size constants | [nki.language.tile_size](../programming/api/nki.language.md) |

---

## U

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `uint8` | nki.language | 8-bit unsigned integer | [nki.language.uint8](../programming/api/api-nki-language-types.md) |
| `uint16` | nki.language | 16-bit unsigned integer | [nki.language.uint16](../programming/api/api-nki-language-types.md) |
| `uint32` | nki.language | 32-bit unsigned integer | [nki.language.uint32](../programming/api/api-nki-language-types.md) |

---

## Z

| Symbol | Module | Description | Documentation |
|--------|--------|-------------|---------------|
| `zeros` | nki.language | Create zero-filled tensor | [nki.language.zeros](../programming/api/nki.language.md) |

---

## Symbols by Category

### Tensor Creation
| Symbol | Documentation |
|--------|---------------|
| `nki.language.ndarray` | [Link](../programming/api/nki.language.md) |
| `nki.language.zeros` | [Link](../programming/api/nki.language.md) |

### Memory Buffers
| Symbol | Documentation |
|--------|---------------|
| `nki.language.sbuf` | [Link](../programming/api/api-nki-language-memory.md#nki-language-sbuf) |
| `nki.language.psum` | [Link](../programming/api/api-nki-language-memory.md#nki-language-psum) |
| `nki.language.hbm` | [Link](../programming/api/api-nki-language-memory.md#nki-language-hbm) |
| `nki.language.private_hbm` | [Link](../programming/api/api-nki-language-memory.md#nki-language-private_hbm) |
| `nki.language.shared_hbm` | [Link](../programming/api/api-nki-language-memory.md#nki-language-shared_hbm) |

### Loop Iterators
| Symbol | Documentation |
|--------|---------------|
| `range` (recommended) | Standard Python range |
| `nki.language.static_range` | [Link](../programming/api/nki.language.md) (legacy alias for `range`) |
| `nki.language.affine_range` | [Link](../programming/api/nki.language.md) (legacy alias for `range`) |
| `nki.language.sequential_range` | [Link](../programming/api/nki.language.md) (legacy alias for `range`) |

### Data Types
| Symbol | Documentation |
|--------|---------------|
| `nki.language.bool_` | [Link](../programming/api/api-nki-language-types.md) |
| `nki.language.int8` | [Link](../programming/api/api-nki-language-types.md) |
| `nki.language.int16` | [Link](../programming/api/api-nki-language-types.md) |
| `nki.language.int32` | [Link](../programming/api/api-nki-language-types.md) |
| `nki.language.uint8` | [Link](../programming/api/api-nki-language-types.md) |
| `nki.language.uint16` | [Link](../programming/api/api-nki-language-types.md) |
| `nki.language.uint32` | [Link](../programming/api/api-nki-language-types.md) |
| `nki.language.float16` | [Link](../programming/api/api-nki-language-types.md) |
| `nki.language.float32` | [Link](../programming/api/api-nki-language-types.md) |
| `nki.language.bfloat16` | [Link](../programming/api/api-nki-language-types.md) |
| `nki.language.tfloat32` | [Link](../programming/api/api-nki-language-types.md) |
| `nki.language.float8_e4m3` | [Link](../programming/api/api-nki-language-types.md) |
| `nki.language.float8_e5m2` | [Link](../programming/api/api-nki-language-types.md) |

### Matrix Operations (Tensor Engine)
| Symbol | Documentation |
|--------|---------------|
| `nki.isa.nc_matmul` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_matmul) |
| `nki.isa.nc_matmul_mx` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_matmul_mx) |
| `nki.isa.nc_transpose` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_transpose) |

### Vector Operations (Vector Engine)
| Symbol | Documentation |
|--------|---------------|
| `nki.isa.tensor_tensor` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_tensor) |
| `nki.isa.tensor_tensor_scan` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_tensor_scan) |
| `nki.isa.tensor_scalar` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_scalar) |
| `nki.isa.tensor_reduce` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_reduce) |
| `nki.isa.bn_stats` | [Link](../programming/api/api-nki-isa-vector.md#nki-isa-bn_stats) |
| `nki.isa.bn_aggr` | [Link](../programming/api/api-nki-isa-vector.md#nki-isa-bn_aggr) |
| `nki.isa.reciprocal` | [Link](../programming/api/api-nki-isa-scalar.md#nki-isa-reciprocal) |

### Scalar Operations (Scalar Engine)
| Symbol | Documentation |
|--------|---------------|
| `nki.isa.activation` | [Link](../programming/api/api-nki-isa-scalar.md#nki-isa-activation) |
| `nki.isa.activation_reduce` | [Link](../programming/api/api-nki-isa-scalar.md#nki-isa-activation_reduce) |
| `nki.isa.dropout` | [Link](../programming/api/api-nki-isa-scalar.md#nki-isa-dropout) |

### DMA Operations
| Symbol | Documentation |
|--------|---------------|
| `nki.isa.dma_copy` | [Link](../programming/api/api-nki-isa-memory.md#nki-isa-dma_copy) |
| `nki.isa.dma_transpose` | [Link](../programming/api/api-nki-isa-memory.md#nki-isa-dma_transpose) |
| `nki.isa.dma_compute` | [Link](../programming/api/api-nki-isa-memory.md#nki-isa-dma_compute) |

### Copy Operations
| Symbol | Documentation |
|--------|---------------|
| `nki.isa.tensor_copy` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_copy) |
| `nki.isa.tensor_copy_predicated` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_copy_predicated) |

### Utility Functions
| Symbol | Documentation |
|--------|---------------|
| `nki.isa.iota` | [Link](../programming/api/api-nki-isa-utility.md#nki-isa-iota) |
| `nki.isa.memset` | [Link](../programming/api/api-nki-isa-memory.md#nki-isa-memset) |
| `nki.isa.affine_select` | [Link](../programming/api/api-nki-isa-utility.md#nki-isa-affine_select) |
| `nki.isa.range_select` | [Link](../programming/api/api-nki-isa-utility.md#nki-isa-range_select) |
| `nki.isa.select_reduce` | [Link](../programming/api/api-nki-isa-utility.md#nki-isa-select_reduce) |
| `nki.isa.max8` | [Link](../programming/api/api-nki-isa-utility.md#nki-isa-max8) |
| `nki.isa.sequence_bounds` | [Link](../programming/api/api-nki-isa-utility.md#nki-isa-sequence_bounds) |

### Gather/Scatter Operations
| Symbol | Documentation |
|--------|---------------|
| `nki.isa.local_gather` | [Link](../programming/api/api-nki-isa-memory.md#nki-isa-local_gather) |
| `nki.isa.nc_n_gather` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_n_gather) |
| `nki.isa.nc_find_index8` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_find_index8) |
| `nki.isa.nc_match_replace8` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_match_replace8) |

### Quantization
| Symbol | Documentation |
|--------|---------------|
| `nki.isa.quantize_mx` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-quantize_mx) |

### Random Number Generation
| Symbol | Documentation |
|--------|---------------|
| `nki.isa.rng` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.rand2` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.rand_set_state` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.rand_get_state` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.set_rng_seed` | [Link](../programming/api/nki.isa.md) |

### Multi-Core/SPMD
| Symbol | Documentation |
|--------|---------------|
| `nki.language.program_id` | [Link](../programming/api/nki.language.md) |
| `nki.language.num_programs` | [Link](../programming/api/nki.language.md) |
| `nki.language.program_ndim` | [Link](../programming/api/nki.language.md) |
| `nki.isa.core_barrier` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-core_barrier) |
| `nki.isa.sendrecv` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.nc_stream_shuffle` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_stream_shuffle) |

### Register Operations
| Symbol | Documentation |
|--------|---------------|
| `nki.isa.register_alloc` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.register_load` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.register_move` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.register_store` | [Link](../programming/api/nki.isa.md) |

### Enums and Constants
| Symbol | Documentation |
|--------|---------------|
| `nki.isa.engine` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.reduce_cmd` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.dge_mode` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.dma_engine` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.oob_mode` | [Link](../programming/api/nki.isa.md) |
| `nki.isa.nc_version` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_version) |
| `nki.isa.get_nc_version` | [Link](../programming/api/api-nki-isa-tensor.md#nki-isa-get_nc_version) |
| `nki.language.tile_size` | [Link](../programming/api/nki.language.md) |

---

## See Also

- [API Reference Index](../programming/api/index.md) - Complete API documentation
- [nki.language Module](../programming/api/nki.language.md) - Language-level APIs
- [nki.isa Module](../programming/api/nki.isa.md) - ISA-level APIs
- [Shared APIs](../programming/api/nki.api.shared.md) - Shared data types and operators
