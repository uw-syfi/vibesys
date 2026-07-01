# MLP Kernel API Reference

MLP Kernel API Reference
This topic provides the API reference for the `MLP` kernel. The kernel implements a Multi-Layer Perceptron with optional normalization fusion and various optimizations.

The kernel supports:

* Both context encoding (CTE) and token generation (TKG) modes

* Optional normalization fusion (RMSNorm, LayerNorm)

* Various activation functions

* Residual connections via fused addition

* Flexible tensor layouts and column tiling optimizations

* Bias addition for all projections and normalization

* FP8 quantization (static and row-wise, TKG mode only)

* Gate and up projection result clamping

* Optional gate projection skipping

* SBUF output for kernel fusion

## Background

The `MLP` kernel is a critical component in transformer architectures, responsible for processing token representations after the attention mechanism. This kernel optimizes the MLP computation by fusing it with optional normalization and supporting various optimizations for both context encoding and token generation scenarios.

> **Note**
>
> Note
> 
> 
> This kernel automatically selects between TKG (Token Generation) and CTE (Context Encoding) implementations based on the batch size × sequence length threshold (currently 96, planned to increase to 128), ensuring optimal performance across different use cases.

## API Reference

**Source code for this kernel API can be found at**: [aws-neuron/nki-library](https://github.com/aws-neuron/nki-library)

### mlp

nkilib.core.mlp.mlp(*hidden_tensor: nl.ndarray*, *gate_proj_weights_tensor: nl.ndarray*, *up_proj_weights_tensor: nl.ndarray*, *down_proj_weights_tensor: nl.ndarray*, *normalization_weights_tensor: Optional[nl.ndarray] = None*, *gate_proj_bias_tensor: Optional[nl.ndarray] = None*, *up_proj_bias_tensor: Optional[nl.ndarray] = None*, *down_proj_bias_tensor: Optional[nl.ndarray] = None*, *normalization_bias_tensor: Optional[nl.ndarray] = None*, *fused_add_tensor: Optional[nl.ndarray] = None*, *store_fused_add_result: [bool](https://docs.python.org/3/library/functions.html#bool) = False*, *activation_fn: ActFnType = ActFnType.SiLU*, *normalization_type: NormType = NormType.NO_NORM*, *quantization_type: QuantizationType = QuantizationType.NONE*, *gate_w_scale: Optional[nl.ndarray] = None*, *up_w_scale: Optional[nl.ndarray] = None*, *down_w_scale: Optional[nl.ndarray] = None*, *gate_up_in_scale: Optional[nl.ndarray] = None*, *down_in_scale: Optional[nl.ndarray] = None*, *output_dtype=None*, *store_output_in_sbuf: [bool](https://docs.python.org/3/library/functions.html#bool) = False*, *eps: [float](https://docs.python.org/3/library/functions.html#float) = 1e-6*, *skip_gate_proj: [bool](https://docs.python.org/3/library/functions.html#bool) = False*, *use_tkg_gate_up_proj_column_tiling: [bool](https://docs.python.org/3/library/functions.html#bool) = True*, *use_tkg_down_proj_column_tiling: [bool](https://docs.python.org/3/library/functions.html#bool) = True*, *use_tkg_down_proj_optimized_layout: [bool](https://docs.python.org/3/library/functions.html#bool) = False*, *gate_clamp_upper_limit: Optional[[float](https://docs.python.org/3/library/functions.html#float)] = None*, *gate_clamp_lower_limit: Optional[[float](https://docs.python.org/3/library/functions.html#float)] = None*, *up_clamp_upper_limit: Optional[[float](https://docs.python.org/3/library/functions.html#float)] = None*, *up_clamp_lower_limit: Optional[[float](https://docs.python.org/3/library/functions.html#float)] = None*, *force_cte_mode: [bool](https://docs.python.org/3/library/functions.html#bool) = False*)
MLP(Multi-Layer Perceptron) Kernel implementation.

Performs the standard MLP computation with support for both context encoding (CTE) and
token generation (TKG) modes. Automatically selects the appropriate implementation based
on input dimensions and supports various optimizations.

Parameters:

* **hidden_tensor** (`nl.ndarray`) – Input hidden states tensor with shape [B, S, H] or SBUF layout.

* **gate_proj_weights_tensor** (`nl.ndarray`) – Gate projection weight matrix with shape [H, I].

* **up_proj_weights_tensor** (`nl.ndarray`) – Up projection weight matrix with shape [H, I].

* **down_proj_weights_tensor** (`nl.ndarray`) – Down projection weight matrix with shape [I, H].

* **normalization_weights_tensor** (`nl.ndarray`, optional) – Normalization weights with shape [1, H].

* **gate_proj_bias_tensor** (`nl.ndarray`, optional) – Bias tensor for gate projection with shape [1, I].

* **up_proj_bias_tensor** (`nl.ndarray`, optional) – Bias tensor for up projection with shape [1, I].

* **down_proj_bias_tensor** (`nl.ndarray`, optional) – Bias tensor for down projection with shape [1, H].

* **normalization_bias_tensor** (`nl.ndarray`, optional) – Bias tensor for normalization with shape [1, H]. Only applicable for layer normalization.

* **fused_add_tensor** (`nl.ndarray`, optional) – Tensor to fuse for the residual connection.

* **store_fused_add_result** (`bool`) – If True, stores the fused_add output to HBM, and the kernel returns both the fused_add output and the MLP output. Default: False.

* **activation_fn** (`ActFnType`) – Activation function type.

* **normalization_type** (`NormType`) – Type of normalization.

* **quantization_type** (`QuantizationType`) – Quantization type to use (default: QuantizationType.NONE). Supported values are QuantizationType.STATIC and QuantizationType.ROW. Quantization is only supported in TKG mode.

* **gate_w_scale** (`nl.ndarray`, optional) – FP8 dequantization scales for gate weights. Shape is [128, I] for row-wise quantization, [128, 1] for static quantization. Defaults to None.

* **up_w_scale** (`nl.ndarray`, optional) – FP8 dequantization scales for up weights. Shape is [128, I] for row-wise quantization, [128, 1] for static quantization. Defaults to None.

* **down_w_scale** (`nl.ndarray`, optional) – FP8 dequantization scales for down weights. Shape is [128, I] for row-wise quantization, [128, 1] for static quantization. Defaults to None.

* **gate_up_in_scale** (`nl.ndarray`, optional) – FP8 dequantization scales for gate and up input. Used for static quantization with shape [128, 1]. Defaults to None.

* **down_in_scale** (`nl.ndarray`, optional) – FP8 dequantization scales for down input. Used for static quantization with shape [128, 1]. Defaults to None.

* **output_dtype** (`nki.dtype`) – Output tensor data type. Defaults to None; if None, the hidden tensor’s `dtype` is used.

* **store_output_in_sbuf** (`bool`) – If True, stores the output in SBUF instead of HBM, allowing the next layer to read it directly without an additional load operation. This option is only available in TKG mode where output tensor is small enough to fit in SBUF. Default: False.

* **eps** (`float`) – Epsilon value for numerical stability.

* **skip_gate_proj** (`bool`) – Skip gate projection.

* **use_tkg_gate_up_proj_column_tiling** (`bool`) – If True, uses column tiling for the gate and up projection in TKG mode. Default: True.

* **use_tkg_down_proj_column_tiling** (`bool`) – If True, uses column tiling for the down projection in TKG mode. Default: True.

* **use_tkg_down_proj_optimized_layout** (`bool`) – If True, the standard down_weight tensor (`shape [I, H]`) is reinterpreted as `[I, lnc, 128, H // (128 * lnc)]`, then transposed to `[I, lnc, H // (128 * lnc), 128]`. This layout provides unit-stride weight loading, reducing the matrix multiplication initiation interval. Only applied when `use_tkg_down_proj_column_tiling` is False. Default: False.

* **gate_clamp_upper_limit** (`float`, optional) – Upper bound value to clamp on gate projection results, does not perform clamping if the value is set to None.

* **gate_clamp_lower_limit** (`float`, optional) – Lower bound value to clamp on gate projection results, does not perform clamping if the value is set to None.

* **up_clamp_upper_limit** (`float`, optional) – Upper bound value to clamp on up projection results, does not perform clamping if the value is set to None.

* **up_clamp_lower_limit** (`float`, optional) – Lower bound value to clamp on up projection results, does not perform clamping if the value is set to None.

* **force_cte_mode** (`bool`) – If True, forces the use of CTE mode. Default: False.

Returns:
The MLP output tensor(s). HBM output: Tensor with shape [B, S, H]. SBUF output: Shape depends on the mode setting. CTE: Not applicable. TKG when `use_tkg_down_proj_column_tiling` is `True = [BxS, H]`. TKG when `use_tkg_down_proj_column_tiling` is `False = [128(p_max), H/128, BxS`]``. If `store_fused_add_result` is `True`, returns a list containing both the output and the stored fused output.

Return type:
`list[nl.ndarray]`

**Notes**:

* Automatically dispatches to either CTE or TKG implementation based on batch size and sequence length.

* Token generation mode (TKG) is used for small batch/sequence dimensions (`batch_size × sequence_length ≤ 96`), while context encoding (CTE) handles larger inputs.

* Column tiling and tensor layout optimization (`use_tkg_down_proj_optimized_layout`) are valid only in TKG mode.

* FP8 quantization support is available only in TKG mode.

* Supported input data types: `nl.bfloat16`, `nl.float16`, `nl.float32`

## Implementation Details

The kernel implementation includes several key optimizations:

* **Dual Implementation Strategy**: Automatically selects between CTE (Context Encoding) and TKG (Token Generation) implementations based on `batch_size × sequence_length` threshold (currently 96, planned to increase to 128).

* **Normalization Fusion**: Optionally fuses RMSNorm or LayerNorm operations with the MLP computation for improved performance.

* **FP8 Quantization**: Supports FP8 quantization with both static and row-wise dequantization scales. Available only in TKG mode for weights and activations.

* **Flexible Tensor Layouts**: Supports column tiling optimizations and tensor layout optimizations in TKG mode to improve memory access patterns.

* **Activation Function Options**: Supports multiple activation functions, including SiLU (Swish), GELU, and ReLU.

* **Result Clamping**: Provides optional clamping of gate and up projection results with configurable upper and lower bounds.

* **Gate Projection Skipping**: Allows skipping the gate projection computation when `skip_gate_proj` is enabled.

* **Residual Connection Fusion**: Can incorporate residual connections through fused_add_tensor for improved performance.

* **SBUF Output Option**: Provides the option to keep output in SBUF for fusion with subsequent operations (TKG mode only).

* **Bias Addition**: Supports optional bias addition for gate, up, and down projections, as well as for normalization.

* **Optimized Weight Loading**: In TKG mode, `use_tkg_down_proj_optimized_layout` enables unit-stride weight loading to reduce matrix multiplication initiation interval.

* **Multi-Precision Support**: Supports `bfloat16`, `float16`, and `float32` input data types for flexible precision requirements.

## See Also

* [QKV Kernel API Reference](qkv.md)

* [RMSNorm-Quant Kernel API Reference](rmsnorm-quant.md)