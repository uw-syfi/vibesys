# nki.isa

The `nki.isa` module exposes low-level ISA instructions for compute, data movement, and synchronization.
These APIs map to individual Tensor Engine, Vector Engine, Scalar Engine, and DMA Engine operations,
giving you fine-grained control over the underlying hardware capabilities.

<a id="nki-isa"></a>

## NKI ISA

| Symbol | Summary |
| --- | --- |
| [nc_matmul](api-nki-isa-tensor.md#nki-isa-nc_matmul) | Compute `dst = stationary.T @ moving` matrix multiplication using Tensor Engine. |
| [nc_matmul_mx](api-nki-isa-tensor.md#nki-isa-nc_matmul_mx) | Compute matrix multiplication of MXFP8/MXFP4 quantized matrices with integrated dequantization using Tensor Engine. |
| [nc_transpose](api-nki-isa-memory.md#nki-isa-nc_transpose) | Perform a 2D transpose between the partition axis and the free axis of input `data` using Tensor or Vector Engine. |
| [activation](api-nki-isa-scalar.md#nki-isa-activation) | Apply an activation function on every element of the input tile using Scalar Engine or GpSIMD Engine (on NeuronCore-v4+), with an optional scale/bias operation before the activation and an optional reduction operation after the activation in the same instruction (on Scalar Engine). |
| [activation_reduce](api-nki-isa-scalar.md#nki-isa-activation_reduce) | Perform the same computation as `nisa.activation` and also a reduction along the free dimension of the `nisa.activation` result using Scalar Engine. |
| [activate2](api-nki-isa-scalar.md#nki-isa-activate2) | Perform tensor activation with configurable tensor-scalar operations and optional reduction using Scalar Engine or GpSIMD Engine (on NeuronCore-v4+). |
| [tensor_reduce](api-nki-isa-tensor.md#nki-isa-tensor_reduce) | Apply a reduction operation to the free axes of an input `data` tile using Vector Engine. |
| [tensor_partition_reduce](api-nki-isa-tensor.md#nki-isa-tensor_partition_reduce) | Apply a reduction operation across partitions of an input `data` tile using GpSimd Engine. |
| [tensor_tensor](api-nki-isa-tensor.md#nki-isa-tensor_tensor) | Perform an element-wise operation of input two tiles using Vector Engine or GpSimd Engine. |
| [tensor_tensor_scan](api-nki-isa-tensor.md#nki-isa-tensor_tensor_scan) | Perform a scan operation of two input tiles using Vector Engine. |
| [scalar_tensor_tensor](api-nki-isa-tensor.md#nki-isa-scalar_tensor_tensor) | Apply two math operators in sequence using Vector Engine: `(data <op0> operand0) <op1> operand1`. |
| [tensor_scalar](api-nki-isa-tensor.md#nki-isa-tensor_scalar) | Apply up to two math operators to the input `data` tile by broadcasting scalar/vector operands in the free dimension using Vector or Scalar or GpSimd Engine: `(data <op0> operand0) <op1> operand1`. |
| [tensor_scalar_reduce](api-nki-isa-tensor.md#nki-isa-tensor_scalar_reduce) | Perform the same computation as `nisa.tensor_scalar` with one math operator and also a reduction along the free dimension of the `nisa.tensor_scalar` result using Vector Engine. |
| [tensor_scalar_cumulative](api-nki-isa-tensor.md#nki-isa-tensor_scalar_cumulative) | Perform tensor-scalar arithmetic operation with cumulative reduction using Vector Engine. |
| [tensor_copy](api-nki-isa-memory.md#nki-isa-tensor_copy) | Create a copy of `src` tile within NeuronCore on-chip SRAMs using Vector, Scalar or GpSimd Engine. |
| [tensor_copy_predicated](api-nki-isa-tensor.md#nki-isa-tensor_copy_predicated) | Conditionally copy elements from the `src` tile to the `dst` tile on SBUF / PSUM based on a `predicate` using Vector Engine. |
| [exponential](api-nki-isa-misc.md#nki-isa-exponential) | Apply exponential function to each element after subtracting a max_value using Vector Engine. |
| [reciprocal](api-nki-isa-misc.md#nki-isa-reciprocal) | Compute element-wise reciprocal (1.0/x) of the input `data` tile using Vector Engine. |
| [quantize_mx](api-nki-isa-misc.md#nki-isa-quantize_mx) | Quantize FP16/BF16 data to MXFP8 tensors (both data and scales) using Vector Engine. |
| [iota](api-nki-isa-utility.md#nki-isa-iota) | Generate a constant literal pattern into SBUF using GpSimd Engine. |
| [dropout](api-nki-isa-misc.md#nki-isa-dropout) | Randomly replace some elements of the input tile `data` with zeros based on input probabilities using Vector Engine. |
| [affine_select](api-nki-isa-utility.md#nki-isa-affine_select) | Select elements between an input tile `on_true_tile` and a scalar value `on_false_value` according to a boolean predicate tile using GpSimd Engine. |
| [range_select](api-nki-isa-misc.md#nki-isa-range_select) | Select elements from `on_true_tile` based on comparison with bounds using Vector Engine. |
| [select_reduce](api-nki-isa-misc.md#nki-isa-select_reduce) | Selectively copy elements from either `on_true` or `on_false` to the destination tile based on a `predicate` using Vector Engine, with optional reduction (max). |
| [sequence_bounds](api-nki-isa-misc.md#nki-isa-sequence_bounds) | Compute the sequence bounds for a given set of segment IDs using GpSIMD Engine. |
| [memset](api-nki-isa-memory.md#nki-isa-memset) | Initialize `dst` by filling it with a compile-time constant `value`, using Vector or GpSimd Engine. |
| [bn_stats](api-nki-isa-vector.md#nki-isa-bn_stats) | Compute mean- and variance-related statistics for each partition of an input tile `data` in parallel using Vector Engine. |
| [bn_aggr](api-nki-isa-vector.md#nki-isa-bn_aggr) | Aggregate one or multiple `bn_stats` outputs to generate a mean and variance per partition using Vector Engine. |
| [local_gather](api-nki-isa-memory.md#nki-isa-local_gather) | Gather SBUF data in `src_buffer` using `index` on GpSimd Engine. |
| [nc_n_gather](api-nki-isa-utility.md#nki-isa-nc_n_gather) | Gather elements from `data` according to `indices` using GpSimd Engine. |
| [dma_copy](api-nki-isa-memory.md#nki-isa-dma_copy) | Copy data from `src` to `dst` using DMA engines. |
| [dma_transpose](api-nki-isa-memory.md#nki-isa-dma_transpose) | Perform a transpose on input `src` using DMA Engine. |
| [dma_compute](api-nki-isa-misc.md#nki-isa-dma_compute) | Perform math operations using compute logic inside DMA engines with element-wise scaling and reduction. |
| [max8](api-nki-isa-misc.md#nki-isa-max8) | Find the 8 largest values in each partition of the source tile. |
| [nc_find_index8](api-nki-isa-tensor.md#nki-isa-nc_find_index8) | Find indices of the 8 given vals in each partition of the data tensor. |
| [nc_match_replace8](api-nki-isa-tensor.md#nki-isa-nc_match_replace8) | Replace first occurrence of each value in `vals` with `imm` in `data` using the Vector engine and return the replaced tensor. |
| [nc_stream_shuffle](api-nki-isa-tensor.md#nki-isa-nc_stream_shuffle) | Apply cross-partition data movement within a quadrant of 32 partitions from source tile `src` to destination tile `dst` using Vector Engine. |
| [register_alloc](api-nki-isa-misc.md#nki-isa-register_alloc) | Allocate a virtual register and optionally initialize it with a value. |
| [register_load](api-nki-isa-misc.md#nki-isa-register_load) | Load a scalar value from memory (HBM or SBUF) into a virtual register. |
| [register_move](api-nki-isa-misc.md#nki-isa-register_move) | Move a value into a virtual register. |
| [register_store](api-nki-isa-misc.md#nki-isa-register_store) | Store the value from a virtual register into memory (HBM/SBUF). |
| [scalar_load](api-nki-isa-misc.md#nki-isa-scalar_load) | Load a scalar value from a 1x1 tensor. |
| [scalar_store](api-nki-isa-misc.md#nki-isa-scalar_store) | Store a scalar value to a 1x1 tensor in memory. |
| [core_barrier](api-nki-isa-local-collective.md#nki-isa-core_barrier) | Synchronize execution across multiple NeuronCores by implementing a barrier mechanism. |
| [sendrecv](api-nki-isa-local-collective.md#nki-isa-sendrecv) | Perform point-to-point communication between NeuronCores by sending and receiving data simultaneously using DMA engines. |
| [sendrecv_compute](api-nki-isa-local-collective.md#nki-isa-sendrecv_compute) | Perform point-to-point communication between NeuronCores and reduce the local and received tiles into a single result, using the DMA engine's reduction capability. |
| [rng](api-nki-isa-misc.md#nki-isa-rng) | Generate pseudo random numbers using the Vector or GpSimd Engine. |
| [rand2](api-nki-isa-misc.md#nki-isa-rand2) | Generate pseudo random numbers with uniform distribution using Vector Engine. |
| [rand_set_state](api-nki-isa-misc.md#nki-isa-rand_set_state) | Seed the pseudo random number generator (PRNG) inside the engine. |
| [rand_get_state](api-nki-isa-misc.md#nki-isa-rand_get_state) | Store the current pseudo random number generator (PRNG) states from the engine. |
| [set_rng_seed](api-nki-isa-misc.md#nki-isa-set_rng_seed) | Seed the pseudo random number generator (PRNG) inside the Vector Engine. |
| [nonzero_with_count](api-nki-isa-misc.md#nki-isa-nonzero_with_count) | Find indices of nonzero elements in an input tensor and their total count using GpSimd Engine. |
| [topk](api-nki-isa-tensor.md#nki-isa-topk) | Find the k largest values and their indices in each 16-partition group using GpSIMD Engine. |

## NKI ISA Config Enums

| Symbol | Summary |
| --- | --- |
| [engine](api-nki-isa-misc.md#nki-isa-engine) | Neuron Device engines. |
| [dma_engine](api-nki-isa-misc.md#nki-isa-dma_engine) | DMA transfer engine. |
| [reduce_cmd](api-nki-isa-misc.md#nki-isa-reduce_cmd) | Engine register reduce commands. |
| [dge_mode](api-nki-isa-misc.md#nki-isa-dge_mode) | Descriptor Generation Engine mode. |
| [oob_mode](api-nki-isa-misc.md#nki-isa-oob_mode) | Out-of-bounds access mode. |
| [matmul_perf_mode](api-nki-isa-misc.md#nki-isa-matmul_perf_mode) | Performance mode for matmul. |

## Target

| Symbol | Summary |
| --- | --- |
| [nc_version](api-nki-isa-tensor.md#nki-isa-nc_version) | NeuronCore version. |
| [get_nc_version](api-nki-isa-tensor.md#nki-isa-get_nc_version) | Returns the nc_version of the current target context. |

## Constants
