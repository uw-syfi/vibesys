# nki.isa

nki.isa

## NKI ISA


| [ nc_matmul ](generated/nki.isa.nc_matmul.md#nki.isa.nc_matmul) | Compute dst = stationary.T &#64; moving matrix multiplication using Tensor Engine. |
| --- | --- |
| [ nc_matmul_mx ](generated/nki.isa.nc_matmul_mx.md#nki.isa.nc_matmul_mx) | Compute matrix multiplication of MXFP8/MXFP4 quantized matrices with integrated dequantization using Tensor Engine. |
| [ nc_transpose ](generated/nki.isa.nc_transpose.md#nki.isa.nc_transpose) | Perform a 2D transpose between the partition axis and the free axis of input data using Tensor or Vector Engine. |
| [ activation ](generated/nki.isa.activation.md#nki.isa.activation) | Apply an activation function on every element of the input tile using Scalar Engine, with an optional scale/bias operation before the activation and an optional reduction operation after the activation in the same instruction. |
| [ activation_reduce ](generated/nki.isa.activation_reduce.md#nki.isa.activation_reduce) | Perform the same computation as nisa.activation and also a reduction along the free dimension of the nisa.activation result using Scalar Engine. |
| [ activate2 ](generated/nki.isa.activate2.md#nki.isa.activate2) | Apply activation to result of two-stage tensor-scalar pipeline `(data op0 imm0) op1 imm1` with optional reduction, all in one Scalar Engine instruction. Trn3 only. |
| [ tensor_reduce ](generated/nki.isa.tensor_reduce.md#nki.isa.tensor_reduce) | Apply a reduction operation to the free axes of an input data tile using Vector Engine. |
| [ tensor_partition_reduce ](generated/nki.isa.tensor_partition_reduce.md#nki.isa.tensor_partition_reduce) | Apply a reduction operation across partitions of an input data tile using GpSimd Engine. |
| [ tensor_tensor ](generated/nki.isa.tensor_tensor.md#nki.isa.tensor_tensor) | Perform an element-wise operation of input two tiles using Vector Engine or GpSimd Engine. |
| [ tensor_tensor_scan ](generated/nki.isa.tensor_tensor_scan.md#nki.isa.tensor_tensor_scan) | Perform a scan operation of two input tiles using Vector Engine. |
| [ scalar_tensor_tensor ](generated/nki.isa.scalar_tensor_tensor.md#nki.isa.scalar_tensor_tensor) | Apply two math operators in sequence using Vector Engine: (data &lt;op0&gt; operand0) &lt;op1&gt; operand1 . |
| [ tensor_scalar ](generated/nki.isa.tensor_scalar.md#nki.isa.tensor_scalar) | Apply up to two math operators to the input data tile by broadcasting scalar/vector operands in the free dimension using Vector or Scalar or GpSimd Engine: (data &lt;op0&gt; operand0) &lt;op1&gt; operand1 . |
| [ tensor_scalar_reduce ](generated/nki.isa.tensor_scalar_reduce.md#nki.isa.tensor_scalar_reduce) | Perform the same computation as nisa.tensor_scalar with one math operator and also a reduction along the free dimension of the nisa.tensor_scalar result using Vector Engine. |
| [ tensor_scalar_cumulative ](generated/nki.isa.tensor_scalar_cumulative.md#nki.isa.tensor_scalar_cumulative) | Perform tensor-scalar arithmetic operation with cumulative reduction using Vector Engine. |
| [ tensor_copy ](generated/nki.isa.tensor_copy.md#nki.isa.tensor_copy) | Create a copy of src tile within NeuronCore on-chip SRAMs using Vector, Scalar or GpSimd Engine. |
| [ tensor_copy_predicated ](generated/nki.isa.tensor_copy_predicated.md#nki.isa.tensor_copy_predicated) | Conditionally copy elements from the src tile to the destination tile on SBUF / PSUM based on a predicate using Vector Engine. |
| [ reciprocal ](generated/nki.isa.reciprocal.md#nki.isa.reciprocal) | Compute element-wise reciprocal (1.0/x) of the input data tile using Vector Engine. |
| [ quantize_mx ](generated/nki.isa.quantize_mx.md#nki.isa.quantize_mx) | Quantize FP16/BF16 data to MXFP8 tensors (both data and scales) using Vector Engine. |
| [ iota ](generated/nki.isa.iota.md#nki.isa.iota) | Generate a constant literal pattern into SBUF using GpSimd Engine. |
| [ dropout ](generated/nki.isa.dropout.md#nki.isa.dropout) | Randomly replace some elements of the input tile data with zeros based on input probabilities using Vector Engine. |
| [ exponential ](generated/nki.isa.exponential.md#nki.isa.exponential) | Dedicated exponential instruction with max subtraction, faster than `nisa.activation(op=nl.exp)`. Trn3 (NeuronCore-v4) only. |
| [ affine_select ](generated/nki.isa.affine_select.md#nki.isa.affine_select) | Select elements between an input tile on_true_tile and a scalar value on_false_value according to a boolean predicate tile using GpSimd Engine. |
| [ range_select ](generated/nki.isa.range_select.md#nki.isa.range_select) | Select elements from on_true_tile based on comparison with bounds using Vector Engine. |
| [ select_reduce ](generated/nki.isa.select_reduce.md#nki.isa.select_reduce) | Selectively copy elements from either on_true or on_false to the destination tile based on a predicate using Vector Engine, with optional reduction (max). |
| [ sequence_bounds ](generated/nki.isa.sequence_bounds.md#nki.isa.sequence_bounds) | Compute the sequence bounds for a given set of segment IDs using GpSIMD Engine. |
| [ memset ](generated/nki.isa.memset.md#nki.isa.memset) | Initialize dst by filling it with a compile-time constant value , using Vector or GpSimd Engine. |
| [ bn_stats ](generated/nki.isa.bn_stats.md#nki.isa.bn_stats) | Compute mean- and variance-related statistics for each partition of an input tile data in parallel using Vector Engine. |
| [ bn_aggr ](generated/nki.isa.bn_aggr.md#nki.isa.bn_aggr) | Aggregate one or multiple bn_stats outputs to generate a mean and variance per partition using Vector Engine. |
| [ local_gather ](generated/nki.isa.local_gather.md#nki.isa.local_gather) | Gather SBUF data in src_buffer using index on GpSimd Engine. |
| [ dma_copy ](generated/nki.isa.dma_copy.md#nki.isa.dma_copy) | Copy data from src to dst using DMA engines with optional read-modify-write operations. |
| [ dma_transpose ](generated/nki.isa.dma_transpose.md#nki.isa.dma_transpose) | Perform a transpose on input src using DMA Engine. |
| [ dma_compute ](generated/nki.isa.dma_compute.md#nki.isa.dma_compute) | Perform math operations using compute logic inside DMA engines with element-wise scaling and reduction. |
| [ max8 ](generated/nki.isa.max8.md#nki.isa.max8) | Find the 8 largest values in each partition of the source tile. |
| [ nonzero_with_count ](generated/nki.isa.nonzero_with_count.md#nki.isa.nonzero_with_count) | Find indices of nonzero elements and their total count using GpSimd Engine. NeuronCore-v3+ only. |
| [ nc_n_gather ](generated/nki.isa.nc_n_gather.md#nki.isa.nc_n_gather) | Gather elements from data according to indices using GpSimd Engine. |
| [ nc_find_index8 ](generated/nki.isa.nc_find_index8.md#nki.isa.nc_find_index8) | Find indices of the 8 given vals in each partition of the data tensor. |
| [ nc_match_replace8 ](generated/nki.isa.nc_match_replace8.md#nki.isa.nc_match_replace8) | Replace first occurrence of each value in vals with imm in data using the Vector engine and return the replaced tensor. |
| [ nc_stream_shuffle ](generated/nki.isa.nc_stream_shuffle.md#nki.isa.nc_stream_shuffle) | Apply cross-partition data movement within a quadrant of 32 partitions from source tile src to destination tile dst using Vector Engine. |
| [ register_alloc ](generated/nki.isa.register_alloc.md#nki.isa.register_alloc) | Allocate a virtual register and optionally initialize it with an integer value x . |
| [ register_load ](generated/nki.isa.register_load.md#nki.isa.register_load) | Load a scalar value from memory (HBM or SBUF) into a virtual register. |
| [ register_move ](generated/nki.isa.register_move.md#nki.isa.register_move) | Move a value from a source VirtualRegister into a destination register. |
| [ register_store ](generated/nki.isa.register_store.md#nki.isa.register_store) | Store the value from a virtual register into memory (HBM/SBUF). |
| [ core_barrier ](generated/nki.isa.core_barrier.md#nki.isa.core_barrier) | Synchronize execution across multiple NeuronCores by implementing a barrier mechanism. |
| [ sendrecv ](generated/nki.isa.sendrecv.md#nki.isa.sendrecv) | Perform point-to-point communication between NeuronCores by sending and receiving data simultaneously using DMA engines. Uses `dma_engine` enum for engine selection. |
| [ rng ](generated/nki.isa.rng.md#nki.isa.rng) | Generate pseudo random numbers using the Vector or GpSimd Engine. |
| [ rand2 ](generated/nki.isa.rand2.md#nki.isa.rand2) | Generate pseudo random numbers with uniform distribution using Vector Engine. |
| [ rand_set_state ](generated/nki.isa.rand_set_state.md#nki.isa.rand_set_state) | Seed the pseudo random number generator (PRNG) inside the engine. |
| [ rand_get_state ](generated/nki.isa.rand_get_state.md#nki.isa.rand_get_state) | Store the current pseudo random number generator (PRNG) states from the engine to SBUF. |
| [ set_rng_seed ](generated/nki.isa.set_rng_seed.md#nki.isa.set_rng_seed) | Seed the pseudo random number generator (PRNG) inside the Vector Engine. |


## NKI ISA Config Enums


| [ engine ](generated/nki.isa.engine.md#nki.isa.engine) | Neuron Device engines |
| --- | --- |
| [ reduce_cmd ](generated/nki.isa.reduce_cmd.md#nki.isa.reduce_cmd) | Engine Register Reduce commands |
| [ dge_mode ](generated/nki.isa.dge_mode.md#nki.isa.dge_mode) | Neuron Descriptor Generation Engine Mode |
| [ dma_engine ](generated/nki.isa.dma_engine.md#nki.isa.dma_engine) | DMA transfer engine selection (`dma_engine.dma` for shared DMA, `dma_engine.gpsimd_dma` for GPSIMD's internal DMA engine) |
| [ oob_mode ](generated/nki.isa.oob_mode.md#nki.isa.oob_mode) | Out-of-bounds handling mode (`oob_mode.error`, `oob_mode.skip`) |
| [ nc_version ](generated/nki.isa.nc_version.md#nki.isa.nc_version) | NeuronCore version enum |


## Target


| [ nc_version ](generated/nki.isa.nc_version.md#nki.isa.nc_version) | NeuronCore version |
| --- | --- |
| [ get_nc_version ](generated/nki.isa.get_nc_version.md#nki.isa.get_nc_version) | Returns the nc_version of the current target context. |