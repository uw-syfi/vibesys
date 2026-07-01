# NKI Documentation - Hierarchical Table of Contents

This document provides a comprehensive hierarchical index of all NKI (Neuron Kernel Interface) documentation.

---

## 1. Getting Started

- [Introduction to NKI](../programming/nki-introduction.md) - Overview of Neuron Kernel Interface
- [Quickstart: Implement and Run Your First Kernel](../programming/quickstart-implement-run-kernel.md) - Step-by-step guide for beginners
- [Set Up Your Environment](../programming/setup-env.md) - Environment configuration for NKI development

---

## 2. Architecture Guides

- [NKI Architecture Guides Overview](../architecture/nki_arch_guides.md) - Introduction to architecture documentation

### 2.1 NeuronDevice Architectures
- [Trainium/Inferentia2 Architecture](../architecture/trainium_inferentia2_arch.md) - NeuronCore-v2 architecture details
  - NeuronCore-v2 Compute Engines (Tensor, Vector, Scalar, GpSimd)
  - Memory Hierarchy (HBM, SBUF, PSUM)
  - Data Movement with DMA Engines
- [Trainium2 Architecture](../architecture/trainium2_arch.md) - NeuronCore-v3 architecture details
- [Trainium3 Architecture](../architecture/trainium3_arch.md) - NeuronCore-v4 architecture details

---

## 3. Programming Guide

### 3.1 Core Concepts
- [NKI Language Guide](../programming/nki-language-guide.md) - Comprehensive language syntax guide
- [NKI Compiler Documentation](../programming/nki-compiler.md) - Compiler integration and usage
- [Memory Hierarchy Overview](../programming/memory-hierarchy-overview.md) - Understanding HBM, SBUF, and PSUM
- [Tiling Overview](../programming/tiling-overview.md) - Tiling concepts and layout considerations
- [Data Representation Overview](../programming/data-representation-overview.md) - Data types and representation
- [Indexing Overview](../programming/indexing-overview.md) - Tensor indexing in NKI
- [DMA Overview](../programming/nki-dma-overview.md) - Data movement operations

### 3.2 Advanced Topics
- [NKI APS](../programming/nki-aps.md) - Advanced programming systems
- [Logical NeuronCore (LNC)](../programming/lnc.md) - Logical NeuronCore configurations
- [Framework Custom Operators](../programming/framework_custom_op.md) - PyTorch and JAX integration
- [Using Prebuilt Kernels](../programming/tutorial-use-a-prebuilt-kernel.md) - Working with NKI Library kernels

### 3.3 API Reference
- [API Reference Index](../programming/api/index.md) - Complete API documentation index
- [API Overview](../programming/api/api-overview.md) - High-level API organization

#### nki Module
- [nki Module](../programming/api/nki.md) - Top-level nki module

#### nki.language Module
- [nki.language Module Overview](../programming/api/nki.language.md) - Language-level APIs
- [nki.language - Creation Operations](../programming/api/api-nki-language-creation.md) - Tensor creation (ndarray, zeros)
- [nki.language - Memory Operations](../programming/api/api-nki-language-memory.md) - Memory buffers (sbuf, psum, hbm)
- [nki.language - Dimension Operations](../programming/api/api-nki-language-dims.md) - Dimension handling
- [nki.language - Data Types](../programming/api/api-nki-language-types.md) - Supported data types
- [nki.language - Miscellaneous](../programming/api/api-nki-language-misc.md) - Other language APIs

#### nki.isa Module
- [nki.isa Module Overview](../programming/api/nki.isa.md) - Instruction Set Architecture APIs
- [nki.isa - Tensor Engine](../programming/api/api-nki-isa-tensor.md) - Tensor Engine instructions (nc_matmul, nc_transpose)
- [nki.isa - Vector Engine](../programming/api/api-nki-isa-vector.md) - Vector Engine instructions (bn_stats, bn_aggr)
- [nki.isa - Scalar Engine](../programming/api/api-nki-isa-scalar.md) - Scalar Engine instructions (activation, dropout)
- [nki.isa - Memory Operations](../programming/api/api-nki-isa-memory.md) - DMA instructions (dma_copy, dma_transpose)
- [nki.isa - Utility Functions](../programming/api/api-nki-isa-utility.md) - Utility instructions (iota, memset, affine_select)
- [nki.isa - Miscellaneous](../programming/api/api-nki-isa-misc.md) - Other ISA APIs

#### Shared APIs
- [nki.api.shared](../programming/api/nki.api.shared.md) - Shared data types and operators

#### Tools
- [NKI Tools](../programming/api/api-nki-tools.md) - Development and debugging tools

### 3.4 Tutorials
- [NKI Tutorials Index](../programming/tutorials/tutorials.md) - All tutorials overview

#### Basic Operations
- [Matrix Multiplication](../programming/tutorials/matrix_multiplication.md) - Matmul implementation and optimization
- [2D Transpose](../programming/tutorials/transpose2d.md) - Efficient transpose operations
- [Average Pooling 2D](../programming/tutorials/average_pool2d.md) - Pooling kernel implementation

#### Advanced Kernels
- [Fused Mamba](../programming/tutorials/fused_mamba.md) - State space model kernel

#### Performance
- [Kernel Optimization](../programming/tutorials/kernel-optimization.md) - Optimization techniques

---

## 4. Performance Optimization

- [NKI Performance Guide](../optimization/nki_perf_guide.md) - Comprehensive optimization strategies
  - Improving Arithmetic Intensity
  - Optimizing Compute Efficiency
  - Optimizing Data Movement Efficiency
- [Profiling with Neuron Profile](../optimization/use-neuron-profile.md) - Using neuron-profile tool
- [Deep Dives Overview](../optimization/deep-dives-overview.md) - In-depth optimization topics
- [MXFP Matrix Multiplication](../optimization/mxfp-matmul.md) - Microscaling FP matmul optimization
- [NKI Versions](../optimization/nki-beta-versions.md) - Version history (Beta 1, Beta 2, GA)

---

## 5. Reference

### 5.1 General Reference
- [NKI FAQ](../reference/nki_faq.md) - Frequently asked questions
- [NKI Release Notes](../reference/nki_rn.md) - Version history and changes

### 5.2 Migration Guides
- [NKI 0.3.0 Update Guide](../reference/migration/nki-030-update-guide.md) - Updating from NKI 0.2.0 to 0.3.0 (GA)
- [NKI Migration Guide (Beta 1 to Beta 2)](../reference/migration/nki-migration-guide.md) - Upgrading from Beta 1 to Beta 2
- [Block Dimension Migration Guide](../reference/migration/nki_block_dimension_migration_guide.md) - Block dimension changes

### 5.3 NKI Library Kernels
Pre-built reference kernels for common operations:

#### Normalization and Quantization
- [RMSNorm-Quant Kernel](../reference/library/rmsnorm-quant.md) - RMS normalization with quantization
- [RMSNorm-Quant Design](../reference/library/design-rmsnorm-quant.md) - Design documentation

#### QKV Projection
- [QKV Kernel](../reference/library/qkv.md) - Query-Key-Value projection

#### Attention Kernels
- [Attention CTE Kernel](../reference/library/attention-cte.md) - Context encoding attention
- [Attention TKG Kernel](../reference/library/attention-tkg.md) - Token generation attention

#### MLP Kernels
- [MLP Kernel](../reference/library/mlp.md) - Multi-Layer Perceptron

#### Output Projection
- [Output Projection CTE Kernel](../reference/library/output-projection-cte.md) - Context encoding output projection
- [Output Projection TKG Kernel](../reference/library/output-projection-tkg.md) - Token generation output projection

---

## 6. Debugging

- [Compiler Error Codes Index](../debugging/error-codes-index.md) - All error codes overview

### 6.1 Error Code Reference
Individual error code documentation:

| Error Code | Description |
|------------|-------------|
| [EARG001](../debugging/error-codes/EARG001.md) | Unsupported LNC configuration |
| [EBVF030](../debugging/error-codes/EBVF030.md) | Instruction limit exceeded |
| [EHCA005](../debugging/error-codes/EHCA005.md) | Unrecognized custom call target |
| [EOOM001](../debugging/error-codes/EOOM001.md) | Activation memory limit exceeded |
| [EOOM002](../debugging/error-codes/EOOM002.md) | Activation memory limit exceeded (variant) |
| [ESFH002](../debugging/error-codes/ESFH002.md) | 64-bit to 32-bit conversion error |
| [ESPP004](../debugging/error-codes/ESPP004.md) | Unsupported data type |
| [ESPP047](../debugging/error-codes/ESPP047.md) | Unsupported 8-bit floating-point type |
| [EUOC002](../debugging/error-codes/EUOC002.md) | Unsupported operator |
| [EVRF001](../debugging/error-codes/EVRF001.md) | Unsupported operator (verification) |
| [EVRF004](../debugging/error-codes/EVRF004.md) | Complex data types unsupported |
| [EVRF005](../debugging/error-codes/EVRF005.md) | Unsupported FP8 variants |
| [EVRF006](../debugging/error-codes/EVRF006.md) | RNG algorithm error |
| [EVRF007](../debugging/error-codes/EVRF007.md) | Instruction limit exceeded (verification) |
| [EVRF009](../debugging/error-codes/EVRF009.md) | Memory limit exceeded (verification) |
| [EVRF010](../debugging/error-codes/EVRF010.md) | Simultaneous dilation unsupported |
| [EVRF011](../debugging/error-codes/EVRF011.md) | Strided convolution with dilated input |
| [EVRF013](../debugging/error-codes/EVRF013.md) | TopK integer input unsupported |
| [EVRF015](../debugging/error-codes/EVRF015.md) | Unrecognized custom call target (verification) |
| [EVRF016](../debugging/error-codes/EVRF016.md) | Scatter-reduce data type error |
| [EVRF017](../debugging/error-codes/EVRF017.md) | Reduce-window base dilation |
| [EVRF018](../debugging/error-codes/EVRF018.md) | Reduce-window window dilation |
| [EVRF019](../debugging/error-codes/EVRF019.md) | Reduce-window operand count |
| [EVRF022](../debugging/error-codes/EVRF022.md) | Shift-right-arithmetic bit width |
| [EVRF024](../debugging/error-codes/EVRF024.md) | Output tensor size limit |
| [EVRF031](../debugging/error-codes/EVRF031.md) | Scatter out-of-bounds |
| [EXSP001](../debugging/error-codes/EXSP001.md) | Memory limit exceeded (expansion) |
| [EXTP004](../debugging/error-codes/EXTP004.md) | Instruction limit exceeded (expansion) |

---

## Quick Navigation

| Category | Key Documents |
|----------|--------------|
| **New Users** | [Quickstart](../programming/quickstart-implement-run-kernel.md), [Language Guide](../programming/nki-language-guide.md) |
| **Architecture** | [Trainium/Inf2](../architecture/trainium_inferentia2_arch.md), [Trainium2](../architecture/trainium2_arch.md) |
| **API Reference** | [nki.language](../programming/api/nki.language.md), [nki.isa](../programming/api/nki.isa.md) |
| **Tutorials** | [Matrix Multiplication](../programming/tutorials/matrix_multiplication.md), [Fused Mamba](../programming/tutorials/fused_mamba.md) |
| **Performance** | [Performance Guide](../optimization/nki_perf_guide.md), [Profiling](../optimization/use-neuron-profile.md) |
| **Debugging** | [Error Codes](../debugging/error-codes-index.md), [FAQ](../reference/nki_faq.md) |
