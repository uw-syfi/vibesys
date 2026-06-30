# RMSNorm-Quant Kernel API Reference

RMSNorm-Quant Kernel API Reference
This topic provides the API reference for the `RMSNorm-Quant` kernel. The kernel performs optional RMS normalization followed by quantization to fp8.

The kernel supports:

* Optional RMS normalization before quantization

* 8-bit quantization along the last dimension of the input tensor

* Single program multiple data (SPMD) sharding for distributed computation

* Flexible input tensor shapes (minimum 2 dimensions)

* Input validation with configurable dimension limits

* Lower bound clipping for numerical stability

## Background

The `RMSNorm-Quant` kernel processes tensors along their last dimension (processing dimension), with all other dimensions collapsed into a single outer dimension. This design allows for efficient processing of tensors with arbitrary shapes, as long as they have at least 2 dimensions.

For detailed information about the mathematical operations and implementation details, refer to the [RMSNorm-Quant Kernel Design Specification](design-rmsnorm-quant.md).

## API Reference

**Source code for this kernel API can be found at**: [aws-neuron/nki-library](https://github.com/aws-neuron/nki-library)

### RmsNormQuantKernelArgs

*class *nkilib.core.rmsnorm_quant.rmsnorm_quant.RmsNormQuantKernelArgs
RMS Norm Quantization Kernel arguments.

lower_bound*: [float](https://docs.python.org/3/library/functions.html#float)*
Non-negative float used for clipping input values and scale.

norm_type*: NormType** = NormType.RMS_NORM*
Normalization type to use [`RMS_NORM`, `NO_NORM`]

eps*: [float](https://docs.python.org/3/library/functions.html#float)** = 1e-6*
Epsilon value for numerical stability, model hyperparameter

needs_rms_normalization() &#x2192; [bool](https://docs.python.org/3/library/functions.html#bool)
Returns True if RMS normalization should be applied, False otherwise.

has_lower_bound() &#x2192; [bool](https://docs.python.org/3/library/functions.html#bool)
Returns True if a positive lower bound is specified, False otherwise.

**Raises**:

* **AssertionError** – Raised when unsupported normalization types are used, negative bounds are provided, or invalid epsilon values are specified.

### rmsnorm_quant_kernel

nkilib.core.rmsnorm_quant.rmsnorm_quant.rmsnorm_quant_kernel(*hidden: nl.ndarray*, *ln_w: nl.ndarray*, *kargs: [RmsNormQuantKernelArgs](#nkilib.core.rmsnorm_quant.rmsnorm_quant.RmsNormQuantKernelArgs)*)
Entrypoint NKI kernel that performs one of the following:

* Perform RMSNorm and quantize the normalized hidden over the hidden dimension (`H`, or `axis=-1`).

* Quantize hidden over dimension `H`.

The kernel supports no specialization, or specialization along 1 dimension (1D SPMD grid).

Parameters:

* **hidden** (`nl.ndarray`) – Input hidden states tensor with minimum 2 dimensions. For 3D inputs, expected layout is `[B, S, H]`. For 2D inputs, layout is `[outer_dim, processing_dim]` where outer_dim is the product of all major dimensions.

* **ln_w** (`nl.ndarray`) – Gamma multiplicative bias vector with `[H]` or `[1, H]` layout. Required when RMS normalization is enabled.

* **kargs** (`RmsNormQuantKernelArgs`) – Kernel arguments specifying normalization type, bounds, and epsilon values. See [`RmsNormQuantKernelArgs`](#nkilib.core.rmsnorm_quant.rmsnorm_quant.RmsNormQuantKernelArgs) for details.

Returns:
Output tensor with shape `[..., H + 4]` on HBM where the last dimension is extended by 4 elements. The first H elements store the possibly normalized and quantized tensor, while the last 4 elements store fp8 floats that can be reinterpreted as fp32 dequantization scales.

Return type:
`nl.ndarray`

**Constraints**:

* Input tensor must have at least 2 dimensions

* For 3D inputs: batch dimension ≤ MAX_B, sequence length ≤ MAX_S, hidden dimension ≤ MAX_H

* For 2D inputs: processing dimension ≤ MAX_H, outer dimension ≤ MAX_B × MAX_S

* When RMS normalization is enabled, ln_w must have shape [H] or [1, H] where H matches the processing dimension

* Supports 1D SPMD grid or no specialization

> **Note**
>
> Note
> 
> 
> The autocast argument may NOT be respected properly. The kernel automatically handles dimension validation and provides detailed error messages for constraint violations.

## Implementation Details

The kernel implementation includes several key optimizations:

* **Input Tensor Outer Dimension Collapse**: All major dimensions are collapsed into one for simplification, allowing the kernel to process along the minor dimension efficiently.

* **Tiling**: The kernel is tiled on the major dimension by a size equal to the hardware’s maximum partition dimension, ensuring full utilization of the hardware engines’ input width.

* **SBUF/PSUM Allocation**: Uses Stack Allocator for consistent and deterministic memory allocations within the kernel scope.

* **SPMD Sharding**: Supports splitting computation across the constituent cores of a Logical Neuron Core by sharding on the outer-most dimension with automatic load balancing for non-divisible dimensions.

* **Gamma Broadcast**: Improves pipeline parallelism by distributing work to the TensorEngine through matrix multiplication against a vector of ones.

* **Activation Reduce**: Uses specialized instructions to perform reduce-add operations efficiently along with square operations.

* **Optimized Batch Processing**: Processes tiles in batches of 8 for improved efficiency, with remainder handling for non-divisible cases.

* **Input Validation**: Comprehensive validation of tensor dimensions against hardware limits (MAX_B, MAX_S, MAX_H) with detailed error messages.

* **Numerical Stability**: Implements lower bound clipping and minimum dequantization scale clamping to prevent numerical instabilities.

## See Also

* [RMSNorm-Quant Kernel Design Specification](design-rmsnorm-quant.md)