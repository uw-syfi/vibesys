# Task-Based Documentation Routing

This index maps common user goals and tasks to the relevant NKI documentation. Use this guide to quickly find the documentation you need based on what you want to accomplish.

---

## I want to...

### Get Started with NKI

| Task | Recommended Documentation |
|------|--------------------------|
| **Install NKI and set up my environment** | [Set Up Your Environment](../programming/setup-env.md) |
| **Write my first NKI kernel** | [Quickstart: Implement and Run Your First Kernel](../programming/quickstart-implement-run-kernel.md) |
| **Understand what NKI is and when to use it** | [Introduction to NKI](../programming/nki-introduction.md), [FAQ](../reference/nki_faq.md) |
| **Learn NKI syntax and programming model** | [NKI Language Guide](../programming/nki-language-guide.md) |
| **Understand NKI compilation process** | [NKI Compiler Documentation](../programming/nki-compiler.md) |

---

### Understand Hardware Architecture

| Task | Recommended Documentation |
|------|--------------------------|
| **Learn about Trainium/Inferentia2 architecture** | [Trainium/Inferentia2 Architecture](../architecture/trainium_inferentia2_arch.md) |
| **Learn about Trainium2 architecture** | [Trainium2 Architecture](../architecture/trainium2_arch.md) |
| **Learn about Trainium3 architecture** | [Trainium3 Architecture](../architecture/trainium3_arch.md) |
| **Understand NeuronCore compute engines** | [Trainium/Inferentia2 Architecture - Compute Engines](../architecture/trainium_inferentia2_arch.md#neuroncore-v2-compute-engines) |
| **Understand memory hierarchy (HBM, SBUF, PSUM)** | [Memory Hierarchy Overview](../programming/memory-hierarchy-overview.md), [Architecture Guide](../architecture/trainium_inferentia2_arch.md#data-movement) |
| **Learn about Tensor Engine capabilities** | [Architecture Guide - Tensor Engine](../architecture/trainium_inferentia2_arch.md#tensor-engine) |
| **Learn about Vector/Scalar/GpSimd Engines** | [Architecture Guide - Vector Engine](../architecture/trainium_inferentia2_arch.md#vector-engine), [Scalar Engine](../architecture/trainium_inferentia2_arch.md#scalar-engine), [GpSimd Engine](../architecture/trainium_inferentia2_arch.md#gpsimd-engine) |

---

### Implement Common Operations

| Task | Recommended Documentation |
|------|--------------------------|
| **Implement matrix multiplication** | [Matrix Multiplication Tutorial](../programming/tutorials/matrix_multiplication.md), [nki.isa.nc_matmul](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_matmul) |
| **Implement 2D transpose** | [2D Transpose Tutorial](../programming/tutorials/transpose2d.md), [nki.isa.nc_transpose](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_transpose) |
| **Implement average pooling** | [Average Pooling 2D Tutorial](../programming/tutorials/average_pool2d.md) |
| **Implement normalization (LayerNorm, RMSNorm)** | [RMSNorm-Quant Kernel](../reference/library/rmsnorm-quant.md), [bn_stats/bn_aggr](../programming/api/api-nki-isa-vector.md) |
| **Implement attention mechanism** | [Attention CTE Kernel](../reference/library/attention-cte.md), [Attention TKG Kernel](../reference/library/attention-tkg.md) |
| **Implement MLP layers** | [MLP Kernel](../reference/library/mlp.md) |
| **Implement state space models (Mamba)** | [Fused Mamba Tutorial](../programming/tutorials/fused_mamba.md) |
| **Implement QKV projection** | [QKV Kernel](../reference/library/qkv.md) |
| **Implement output projection** | [Output Projection CTE](../reference/library/output-projection-cte.md), [Output Projection TKG](../reference/library/output-projection-tkg.md) |

---

### Work with Data Movement

| Task | Recommended Documentation |
|------|--------------------------|
| **Load data from HBM to SBUF** | [nki.isa.dma_copy](../programming/api/api-nki-isa-memory.md#nki-isa-dma_copy), [DMA Overview](../programming/nki-dma-overview.md) |
| **Store data from SBUF to HBM** | [nki.isa.dma_copy](../programming/api/api-nki-isa-memory.md#nki-isa-dma_copy) |
| **Transpose data during DMA** | [nki.isa.dma_transpose](../programming/api/api-nki-isa-memory.md#nki-isa-dma_transpose) |
| **Copy data within on-chip memory** | [nki.isa.tensor_copy](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_copy) |
| **Perform gather operations** | [nki.isa.local_gather](../programming/api/api-nki-isa-memory.md#nki-isa-local_gather), [nki.isa.nc_n_gather](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_n_gather) |
| **Initialize memory with constant value** | [nki.isa.memset](../programming/api/api-nki-isa-memory.md#nki-isa-memset) |

---

### Perform Tensor Operations

| Task | Recommended Documentation |
|------|--------------------------|
| **Element-wise operations between tensors** | [nki.isa.tensor_tensor](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_tensor) |
| **Tensor-scalar operations with broadcasting** | [nki.isa.tensor_scalar](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_scalar) |
| **Reduce tensor along axes (sum, max, etc.)** | [nki.isa.tensor_reduce](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_reduce) |
| **Reduce across partitions** | [nki.isa.tensor_partition_reduce](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_partition_reduce) |
| **Apply activation functions (relu, gelu, exp, etc.)** | [nki.isa.activation](../programming/api/api-nki-isa-scalar.md#nki-isa-activation) |
| **Compute reciprocal (1/x)** | [nki.isa.reciprocal](../programming/api/api-nki-isa-scalar.md#nki-isa-reciprocal) |
| **Perform scan operations** | [nki.isa.tensor_tensor_scan](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_tensor_scan) |
| **Generate index patterns (iota)** | [nki.isa.iota](../programming/api/api-nki-isa-utility.md#nki-isa-iota) |
| **Apply causal masking** | [nki.isa.affine_select](../programming/api/api-nki-isa-utility.md#nki-isa-affine_select) |
| **Find top-k values** | [nki.isa.max8](../programming/api/api-nki-isa-utility.md#nki-isa-max8) |

---

### Work with Quantization

| Task | Recommended Documentation |
|------|--------------------------|
| **Quantize to MXFP8/MXFP4** | [nki.isa.quantize_mx](../programming/api/api-nki-isa-tensor.md#nki-isa-quantize_mx) |
| **Perform MXFP matmul** | [nki.isa.nc_matmul_mx](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_matmul_mx), [MXFP Matmul Guide](../optimization/mxfp-matmul.md) |
| **Use FP8 data types** | [Data Types](../programming/api/api-nki-language-types.md), [Architecture Guide](../architecture/trainium2_arch.md) |

---

### Implement Distributed/Multi-Core Kernels

| Task | Recommended Documentation |
|------|--------------------------|
| **Write SPMD kernels** | [Logical NeuronCore](../programming/lnc.md), `nl.program_id`, `nl.num_programs` |
| **Use multiple NeuronCores** | [Logical NeuronCore](../programming/lnc.md) |
| **Synchronize across cores** | [nki.isa.core_barrier](../programming/api/api-nki-isa-tensor.md#nki-isa-core_barrier) |
| **Send/receive data between cores** | [nki.isa.sendrecv](../programming/api/api-nki-isa-tensor.md#nki-isa-sendrecv) |
| **Get program/core ID** | [nki.language.program_id](../programming/api/nki.language.md), [nki.language.num_programs](../programming/api/nki.language.md) |

---

### Optimize Performance

| Task | Recommended Documentation |
|------|--------------------------|
| **Profile my NKI kernel** | [Profiling with Neuron Profile](../optimization/use-neuron-profile.md) |
| **Optimize overall kernel performance** | [NKI Performance Guide](../optimization/nki_perf_guide.md) |
| **Improve arithmetic intensity** | [Performance Guide - Arithmetic Intensity](../optimization/nki_perf_guide.md#improving-arithmetic-intensity) |
| **Optimize compute efficiency** | [Performance Guide - Compute Efficiency](../optimization/nki_perf_guide.md#optimizing-compute-efficiency) |
| **Optimize data movement** | [Performance Guide - Data Movement](../optimization/nki_perf_guide.md#optimizing-data-movement-efficiency) |
| **Reduce tensor transposes** | [Performance Guide - Opt #8](../optimization/nki_perf_guide.md#opt-8-tensore-only-mitigating-overhead-from-tensor-transposes) |
| **Overlap compute and data loading** | [Performance Guide - Opt #4](../optimization/nki_perf_guide.md#opt-4-overlap-data-loading-with-computation) |
| **Enable engine pipelining** | [Performance Guide - Opt #3](../optimization/nki_perf_guide.md#opt-3-overlap-execution-across-compute-engines-through-pipelining) |
| **Optimize tile sizes** | [Tiling Overview](../programming/tiling-overview.md), [Matrix Multiplication Tutorial](../programming/tutorials/matrix_multiplication.md) |
| **Combine instructions** | [Performance Guide - Opt #6](../optimization/nki_perf_guide.md#opt-6-combine-instructions) |

---

### Integrate with ML Frameworks

| Task | Recommended Documentation |
|------|--------------------------|
| **Use NKI with PyTorch** | [Framework Custom Operators - PyTorch](../programming/framework_custom_op.md#nki-framework-custom-op-pytorch) |
| **Use NKI with JAX** | [Framework Custom Operators - JAX](../programming/framework_custom_op.md#nki-framework-custom-op-jax) |
| **Use prebuilt NKI Library kernels** | [Using Prebuilt Kernels](../programming/tutorial-use-a-prebuilt-kernel.md), [API Index](../programming/api/index.md) |

---

### Debug and Troubleshoot

| Task | Recommended Documentation |
|------|--------------------------|
| **Understand compiler error messages** | [Compiler Error Codes Index](../debugging/error-codes-index.md) |
| **Fix out-of-memory errors (EOOM)** | [EOOM001](../debugging/error-codes/EOOM001.md), [EOOM002](../debugging/error-codes/EOOM002.md) |
| **Fix unsupported operator errors** | [EVRF001](../debugging/error-codes/EVRF001.md), [EUOC002](../debugging/error-codes/EUOC002.md) |
| **Fix instruction limit errors** | [EBVF030](../debugging/error-codes/EBVF030.md), [EVRF007](../debugging/error-codes/EVRF007.md) |
| **Fix data type errors** | [ESPP004](../debugging/error-codes/ESPP004.md), [EVRF004](../debugging/error-codes/EVRF004.md) |
| **Debug numerical issues** | [FAQ - Debugging](../reference/nki_faq.md#how-can-i-debug-numerical-issues-in-nki-kernels) |
| **Print debug output from kernel** | [nki.language.device_print](../programming/api/nki.language.md) |

---

### Work with Random Numbers

| Task | Recommended Documentation |
|------|--------------------------|
| **Generate random numbers** | [nki.isa.rng](../programming/api/api-nki-isa-tensor.md#nki-isa-rng), [nki.isa.rand2](../programming/api/api-nki-isa-tensor.md#nki-isa-rand2) |
| **Implement dropout** | [nki.isa.dropout](../programming/api/api-nki-isa-scalar.md#nki-isa-dropout) |
| **Set/get RNG state** | [nki.isa.rand_set_state](../programming/api/api-nki-isa-tensor.md#nki-isa-rand_set_state), [nki.isa.rand_get_state](../programming/api/api-nki-isa-tensor.md#nki-isa-rand_get_state) |
| **Seed random number generator** | [nki.isa.set_rng_seed](../programming/api/api-nki-isa-tensor.md#nki-isa-set_rng_seed) |

---

### Understand Tiling and Layout

| Task | Recommended Documentation |
|------|--------------------------|
| **Understand tiling concepts** | [Tiling Overview](../programming/tiling-overview.md) |
| **Understand partition vs free dimensions** | [Tiling Overview](../programming/tiling-overview.md), [Architecture Guide](../architecture/trainium_inferentia2_arch.md) |
| **Handle tensors larger than tile limits** | [Matrix Multiplication Tutorial - Tiling](../programming/tutorials/matrix_multiplication.md) |
| **Understand indexing** | [Indexing Overview](../programming/indexing-overview.md) |

---

### Migrate or Update NKI Code

| Task | Recommended Documentation |
|------|--------------------------|
| **Update from NKI 0.2.0 to 0.3.0 (GA)** | [NKI 0.3.0 Update Guide](../reference/migration/nki-030-update-guide.md) |
| **Migrate from Beta 1 to Beta 2** | [NKI Migration Guide](../reference/migration/nki-migration-guide.md) |
| **Update block dimension usage** | [Block Dimension Migration Guide](../reference/migration/nki_block_dimension_migration_guide.md) |
| **Check version information** | [NKI Versions](../optimization/nki-beta-versions.md) |
| **Review release notes** | [NKI Release Notes](../reference/nki_rn.md) |
| **Run kernel without hardware (CPU simulator)** | [NKI 0.3.0 Update Guide - CPU Simulator](../reference/migration/nki-030-update-guide.md#nki-cpu-simulator) |

---

## Common Workflows

### New Kernel Development Workflow
1. [Set up environment](../programming/setup-env.md)
2. [Follow quickstart](../programming/quickstart-implement-run-kernel.md)
3. [Study language guide](../programming/nki-language-guide.md)
4. [Understand architecture](../architecture/trainium_inferentia2_arch.md)
5. [Implement kernel using tutorials](../programming/tutorials/tutorials.md)
6. [Profile and optimize](../optimization/nki_perf_guide.md)

### Performance Optimization Workflow
1. [Profile kernel](../optimization/use-neuron-profile.md)
2. [Identify bottlenecks](../optimization/nki_perf_guide.md)
3. [Apply relevant optimizations](../optimization/nki_perf_guide.md)
4. [Re-profile to verify improvements](../optimization/use-neuron-profile.md)

### Debugging Workflow
1. [Check error code](../debugging/error-codes-index.md)
2. [Review FAQ](../reference/nki_faq.md)
3. [Use device_print for debugging](../programming/api/nki.language.md)
4. [Consult architecture guide](../architecture/trainium_inferentia2_arch.md)
