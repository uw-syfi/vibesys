# QKV Kernel API Reference

QKV Kernel API Reference
This topic provides the API reference for the `QKV` kernel. The kernel performs Query-Key-Value projection with optional normalization fusion.

The kernel supports:

* Optional RMSNorm/LayerNorm fusion

* Multiple output tensor layouts

* Residual connections from previous MLP and attention outputs

* Automatic selection between TKG and CTE implementations based on batch_size * seqlen threshold

* Optional RoPE (Rotary Position Embedding) fusion

## Background

The `QKV` kernel is a critical component in transformer architectures, responsible for projecting the input hidden states into query, key, and value representations. This kernel optimizes the projection operation by fusing it with optional normalization and supporting various output layouts to accommodate different transformer implementations.

> **Note**
>
> Note
> 
> 
> This kernel automatically selects between TKG (Token Generation) and CTE (Context Encoding) implementations based on sequence length threshold (currently 96), ensuring optimal performance across different use cases. CTE is used for longer sequences, while TKG is optimized for shorter sequences.

## API Reference

**Source code for this kernel API can be found at**: [aws-neuron/nki-library](https://github.com/aws-neuron/nki-library)

### qkv

nkilib.core.qkv.qkv(*input: nl.ndarray*, *fused_qkv_weights: nl.ndarray*, *output_layout: QKVOutputLayout = QKVOutputLayout.BSD*, *bias: Optional[nl.ndarray] = None*, *fused_residual_add: Optional[[bool](https://docs.python.org/3/library/functions.html#bool)] = False*, *mlp_prev: Optional[nl.ndarray] = None*, *attention_prev: Optional[nl.ndarray] = None*, *fused_norm_type: NormType = NormType.NO_NORM*, *gamma_norm_weights: Optional[nl.ndarray] = None*, *layer_norm_bias: Optional[nl.ndarray] = None*, *norm_eps: Optional[[float](https://docs.python.org/3/library/functions.html#float)] = 1e-6*, *hidden_actual: Optional[[int](https://docs.python.org/3/library/functions.html#int)] = None*, *fused_rope: Optional[[bool](https://docs.python.org/3/library/functions.html#bool)] = False*, *cos_cache: Optional[nl.ndarray] = None*, *sin_cache: Optional[nl.ndarray] = None*, *d_head: Optional[[int](https://docs.python.org/3/library/functions.html#int)] = None*, *num_q_heads: Optional[[int](https://docs.python.org/3/library/functions.html#int)] = None*, *num_kv_heads: Optional[[int](https://docs.python.org/3/library/functions.html#int)] = None*, *store_output_in_sbuf: [bool](https://docs.python.org/3/library/functions.html#bool) = False*, *sbm: Optional[SbufManager] = None*, *use_auto_allocation: [bool](https://docs.python.org/3/library/functions.html#bool) = False*, *load_input_with_DMA_transpose: [bool](https://docs.python.org/3/library/functions.html#bool) = True*)
QKV (Query, Key, Value) projection kernel with multiple optional fused operations.

Performs matrix multiplication between hidden states and fused QKV weights matrix with optional
fused operations including residual addition, normalization, bias addition, and RoPE rotation.
Automatically selects between TKG and CTE implementations based on sequence length threshold.

Parameters:

* **input** (`nl.ndarray`) – Input hidden states tensor. Shape: [B, S, H] where B=batch, S=sequence_length, H=hidden_dim.

* **fused_qkv_weights** (`nl.ndarray`) – Fused QKV weight matrix. Shape: [H, I] where I=fused_qkv_dim=(num_q_heads + 2*num_kv_heads)*d_head.

* **output_layout** (`QKVOutputLayout`) – Output tensor layout. QKVOutputLayout.BSD=[B, S, I] or QKVOutputLayout.NBSd=[num_heads, B, S, d_head]. Default: QKVOutputLayout.BSD.

* **bias** (`nl.ndarray`, optional) – Bias tensor to add to QKV projection output. Shape: [1, I].

* **fused_residual_add** (`bool`, optional) – Whether to perform residual addition: input = input + mlp_prev + attention_prev. Default: False.

* **mlp_prev** (`nl.ndarray`, optional) – Previous MLP output tensor for residual addition. Shape: [B, S, H].

* **attention_prev** (`nl.ndarray`, optional) – Previous attention output tensor for residual addition. Shape: [B, S, H].

* **fused_norm_type** (`NormType`) – Type of normalization (NO_NORM, RMS_NORM, RMS_NORM_SKIP_GAMMA, LAYER_NORM). Default: NormType.NO_NORM.

* **gamma_norm_weights** (`nl.ndarray`, optional) – Normalization gamma/scale weights. Shape: [1, H]. Required for RMS_NORM and LAYER_NORM.

* **layer_norm_bias** (`nl.ndarray`, optional) – Layer normalization beta/bias weights. Shape: [1, H]. Only for LAYER_NORM.

* **norm_eps** (`float`, optional) – Epsilon value for numerical stability in normalization. Default: 1e-6.

* **hidden_actual** (`int`, optional) – Actual hidden dimension for padded tensors (if H contains padding).

* **fused_rope** (`bool`, optional) – Whether to apply RoPE rotation to Query and Key heads after QKV projection. Default: False.

* **cos_cache** (`nl.ndarray`, optional) – Cosine cache for RoPE. Shape: [B, S, d_head]. Required if fused_rope=True.

* **sin_cache** (`nl.ndarray`, optional) – Sine cache for RoPE. Shape: [B, S, d_head]. Required if fused_rope=True.

* **d_head** (`int`, optional) – Dimension per attention head. Required for QKVOutputLayout.NBSd and RoPE.

* **num_q_heads** (`int`, optional) – Number of query heads. Required for RoPE.

* **num_kv_heads** (`int`, optional) – Number of key/value heads. Required for RoPE.

* **store_output_in_sbuf** (`bool`) – Whether to store output in SBUF (currently unsupported, must be False). Default: False.

* **sbm** (`SbufManager`, optional) – Optional SBUF manager for memory allocation control with pre-specified bounds for SBUF usage.

* **use_auto_allocation** (`bool`) – Whether to use automatic SBUF allocation. Default: False.

* **load_input_with_DMA_transpose** (`bool`) – Whether to use DMA transpose optimization. Default: True.

Returns:
QKV projection output tensor with shape determined by output_layout.

Return type:
`nl.ndarray`

**Raises**:

* **ValueError** – Raised when contract dimension mismatch occurs between `input` and `fused_qkv_weights`.

* **AssertionError** – Raised when required parameters for fused operations are missing or have incorrect shapes.

## Implementation Details

The kernel implementation includes several key optimizations:

* **Automatic Implementation Selection**: The kernel automatically selects between TKG (Token Generation) and CTE (Context Encoding) implementations based on sequence length threshold (currently 96). Some features like RoPE fusion and loading input with DMA transpose are only available in CTE mode. TKG mode only supports automatic allocation at the moment.

* **Fused Operations Support**:

**Residual Addition**: Fuses `input` + `mlp_prev` + `attention_prev`

* **Normalization**: Supports RMSNorm, LayerNorm, and `RMS_NORM_SKIP_GAMMA`

* **Bias Addition**: Adds bias to QKV projection output

* **RoPE Fusion**: Applies Rotary Position Embedding to Query and Key heads

* **Flexible Output Layouts**: Supports BSD (`[B, S, I]`) and NBSd (`[num_heads, B, S, d_head`]) output tensor layouts.

* **Memory Management**:

Optional SBUF manager for controlled memory allocation

* DMA transpose optimization for weight loading

* Automatic or manual SBUF allocation modes

* **Hardware Compatibility**: Supports bf16, fp16, and fp32 data types (fp32 inputs are internally converted to bf16).

* **Constraints**:

H must be ≤ 24576 and divisible by 128

* I must be ≤ 4096

* For NBSd output: d_head must equal 128