# NCC Verification Errors (NCC_EVRF*)

Detailed reference for Neuron Compiler verification errors. These errors occur when the compiler detects unsupported operations, data types, or configurations during verification.

## NCC_EVRF001 - Unsupported Operator

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: An unsupported operator was used.

**Cause**: Kernel uses an operation not supported by Neuron hardware (e.g., `triangular_solve`).

**Resolution**: Use alternative operators. Run `neuronx-cc list-operators --framework XLA` to see supported operators.

### Before (error)

```python
class Model(torch.nn.Module):
    def forward(self, A, b):
        return torch.triangular_solve(b, A)
```

### After (fixed)

```python
class Model(torch.nn.Module):
    def forward(self, A, b):
        # Although slower than triangular_solve, this is mathematically equivalent
        A_inv = torch.inverse(A)
        return A_inv @ b
```

**See also**: NCC_EUOC002 (same resolution)

---

## NCC_EVRF004 - Complex Data Types

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: Complex data types are not supported on the Neuron device.

**Cause**: Using `complex64`, `complex128`, or other complex data types directly on Neuron hardware.

**Resolution**: Offload complex operations to CPU, or emulate complex arithmetic using real/imaginary parts.

### Option 1: Offload to CPU

```python
x = torch.tensor([1+2j, 3+4j], dtype=torch.complex64).to('cpu')
```

> **Note**: Data transfer between CPU and device is expensive. Best used when complex operations are rare.

### Option 2: Emulate with Real/Imaginary Parts

```python
real = x.real
imag = x.imag

# (a + bi) * (c + di) = (ac - bd) + (ad + bc)i
real_out = a_real * b_real - a_imag * b_imag
imag_out = a_real * b_imag + a_imag * b_real
```

---

## NCC_EVRF005 - Unsupported FP8 Types

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler found usage of F8E4M3FNUZ, F8E4M3B11FNUZ, or F8E5M2FNUZ data type which is not supported.

**Cause**: Using non-standard FP8 variants that Neuron hardware does not support.

**Resolution**: Convert to a supported type (float16, bfloat16, or standard FP8 on gen3+).

### Before (error)

```python
input_tensor = torch.randn(1, 10).to(torch.float8_e4m3fnuz)
```

### After (fixed)

```python
input_tensor = torch.randn(1, 10).to(torch.float8_e4m3fnuz)
# Convert to a supported type
input_tensor = input_tensor.to(torch.float16)
```

**Supported dtypes by hardware**:
- gen2 (Trn1/Inf2): fp32, fp16, bf16 (no FP8)
- gen3/gen4 (Trn2/Trn3): fp32, fp16, bf16, fp8_e4m3, fp8_e5m2

---

## NCC_EVRF006 - Unsupported RNG Algorithm

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler encountered a RNGBitGenerator operation using a random number generation algorithm other than RNG_DEFAULT.

**Cause**: Explicitly specifying a non-default RNG algorithm.

**Resolution**: Use standard JAX/PyTorch random APIs without explicitly specifying an RNG algorithm.

---

## NCC_EVRF007 - Instruction Limit Exceeded

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The number of instructions generated exceeds the limit.

**Cause**: Kernel generates more instructions than the hardware can handle in a single NEFF.

**Resolution**: Apply model parallelism to break large computational graphs into smaller subgraphs.

**Strategies**:
- Use pipeline parallelism via neuronx-distributed
- Use tensor parallelism to shard across devices
- Simplify kernel logic or split into multiple kernels

**See also**: NCC_EBVF030, NCC_EXTP004 (same resolution)

---

## NCC_EVRF009 - Activation Memory Exceeded

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The combined memory needed for the model's activation tensors exceeds the high-bandwidth memory limit.

**Cause**: Activation tensors require more memory than available HBM.

**Resolution**: Reduce batch/tensor size, or use pipeline/tensor parallelism.

### Tensor Parallelism Example

```python
class ParallelSelfAttention(transformers.models.bert.modeling_bert.BertSelfAttention):
    def __init__(self, config, position_embedding_type=None):
        super().__init__(config, position_embedding_type)

        self.query = ColumnParallelLinear(
            config.hidden_size,
            self.all_head_size,
            gather_output=False
        )
        self.key = ColumnParallelLinear(
            config.hidden_size,
            self.all_head_size,
            gather_output=False
        )
        self.value = ColumnParallelLinear(
            config.hidden_size,
            self.all_head_size,
            gather_output=False
        )
        # Shard attention heads across tensor parallel ranks
        tp_size = parallel_state.get_tensor_parallel_size()
        self.num_attention_heads = self.num_attention_heads // tp_size
        self.all_head_size = self.all_head_size // tp_size
```

**See also**: NCC_EOOM001, NCC_EOOM002 (memory errors)

---

## NCC_EVRF010 - Simultaneous Input and Kernel Dilation

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler encountered simultaneous use of input and kernel dilation, which is not supported.

**Cause**: Convolution operation uses both `lhs_dilation` (input) and `rhs_dilation` (kernel) with values > 1.

**Resolution**: Use only input dilation OR kernel dilation, not both.

### Before (error)

```python
result = lax.conv_general_dilated(
    x,
    kernel,
    window_strides=(1, 1),
    padding=((2, 2), (2, 2)),
    lhs_dilation=(2, 2),  # input dilation
    rhs_dilation=(2, 2),  # kernel dilation - ERROR: both set
    dimension_numbers=('NHWC', 'HWIO', 'NHWC')
)
```

### After (fixed)

```python
result = lax.conv_general_dilated(
    x,
    kernel,
    window_strides=(1, 1),
    padding=((2, 2), (2, 2)),
    lhs_dilation=(1, 1),  # no input dilation
    rhs_dilation=(2, 2),  # kernel dilation only
    dimension_numbers=('NHWC', 'HWIO', 'NHWC')
)
```

**Alternative**: Apply dilation manually in separate steps.

---

## NCC_EVRF011 - Strided Convolution with Dilated Input

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler encountered strided convolution combined with dilated input, which is not supported.

**Cause**: Convolution uses both `window_strides > 1` and `lhs_dilation > 1` simultaneously.

**Resolution**: Remove either stride or input dilation.

### Before (error)

```python
result = lax.conv_general_dilated(
    x,
    kernel,
    window_strides=(2, 2),  # strided convolution
    padding=((2, 2), (2, 2)),
    lhs_dilation=(2, 2),    # dilated input - ERROR: both set
    rhs_dilation=(1, 1),
    dimension_numbers=('NHWC', 'HWIO', 'NHWC')
)
```

### After (fixed)

```python
result = lax.conv_general_dilated(
    x,
    kernel,
    window_strides=(2, 2),
    padding=((2, 2), (2, 2)),
    lhs_dilation=(1, 1),  # remove input dilation
    rhs_dilation=(1, 1),
    dimension_numbers=('NHWC', 'HWIO', 'NHWC')
)
```

**Alternative**: Apply upsampling and downsampling in separate steps.

---

## NCC_EVRF013 - TopK Integer Inputs

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: TopK does not support int32 or int64 input tensors.

**Cause**: Calling `torch.topk` on integer-typed tensors.

**Resolution**: Cast input tensor to float before TopK operation.

### Before (error)

```python
def forward(self, x):
    # x is an integer tensor
    k = 5
    values, indices = torch.topk(x, k=k, dim=-1)  # ERROR: integer dtype
    return values, indices
```

### After (fixed)

```python
def forward(self, x):
    x = x.float()  # Cast to float
    k = 5
    values, indices = torch.topk(x, k=k, dim=-1)
    return values, indices
```

---

## NCC_EVRF015 - Unrecognized Custom Call Target

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler encountered a custom call instruction with a target name that is not recognized.

**Cause**: Using a custom call target name not in the supported list.

**Resolution**: Use a recognized custom call target name.

**Recognized Custom Call Targets**:

| Category | Targets |
|----------|---------|
| Activation | `AwsNeuronErf`, `AwsNeuronGelu`, `AwsNeuronGeluApprxTanh`, `AwsNeuronGeluBackward`, `AwsNeuronSilu`, `AwsNeuronSiluBackward` |
| Normalization | `AwsNeuronRmsNorm`, `AwsNeuronSoftmax`, `AwsNeuronSoftmaxBackward` |
| Compute | `AwsNeuronCollectiveMatmul`, `AwsNeuronIntMatmult`, `AwsNeuronArgMax`, `AwsNeuronArgMin`, `AwsNeuronTopK` |
| Utility | `AwsNeuronDropoutMaskV1`, `AwsNeuronCustomNativeKernel`, `AwsNeuronCustomOp`, `AwsNeuronDevicePrint` |
| Resize | `ResizeNearest`, `ResizeBilinear`, `ResizeNearestGrad` |
| Sharding | `AwsNeuronLNCShardingConstraint`, `AwsNeuronTransferWithStaticRing` |
| Markers | `AwsNeuronModuleMarkerStart-Forward`, `AwsNeuronModuleMarkerStart-Backward`, `AwsNeuronModuleMarkerEnd-Forward`, `AwsNeuronModuleMarkerEnd-Backward`, `NeuronBoundaryMarker-Start`, `NeuronBoundaryMarker-End` |

### Before (error)

```python
def lowering(ctx, x_val):
    result_type = ir.RankedTensorType(x_val.type)
    return hlo.CustomCallOp(
        [result_type],
        [x_val],
        call_target_name="UNRECOGNIZED_TARGET",  # ERROR
        has_side_effect=ir.BoolAttr.get(False),
    ).results
```

### After (fixed)

```python
def lowering(ctx, x_val):
    result_type = ir.RankedTensorType(x_val.type)
    return hlo.CustomCallOp(
        [result_type],
        [x_val],
        call_target_name="AwsNeuronSilu",  # Valid target
        has_side_effect=ir.BoolAttr.get(False),
        backend_config=ir.StringAttr.get(""),
        api_version=ir.IntegerAttr.get(ir.IntegerType.get_signless(32), 2),
    ).results
```

**See also**: NCC_EHCA005 (same error, same resolution)

---

## NCC_EVRF016 - Scatter-Reduce Integer/Boolean Types

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The scatter-reduce operation cannot perform reduction logic if the data being scattered or the destination tensor is using an integer or boolean data type.

**Cause**: Hardware scatter-reduce instructions only support floating-point arithmetic. Using integer or boolean types triggers this error.

**Resolution**: Cast input and source tensors to floating-point types (e.g., `torch.float32` or `torch.bfloat16`).

### Before (error)

```python
def forward(self, input_tensor, indices_tensor, src_tensor):
    output = input_tensor.clone()
    output.scatter_reduce_(
        dim=1,
        index=indices_tensor,
        src=src_tensor,
        reduce='sum',
    )
    return output

# ERROR: using integer dtype with scatter-reduce
input_tensor = torch.zeros(BATCH_SIZE, DIM_SIZE, dtype=torch.int32)
```

### After (fixed)

```python
def forward(self, input_tensor, indices_tensor, src_tensor):
    output = input_tensor.clone()
    output.scatter_reduce_(
        dim=1,
        index=indices_tensor,
        src=src_tensor,
        reduce='sum',
    )
    return output

# FIXED: changed to float32
input_tensor = torch.zeros(BATCH_SIZE, DIM_SIZE, dtype=torch.float32)
```

---

## NCC_EVRF017 - Reduce-Window Base Dilation

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler encountered a reduce-window operation with base dilation (input dilation) greater than 1, which is not supported.

**Cause**: Using `base_dilation` parameter with values > 1 in reduce-window operations.

**Resolution**: Set all `base_dilation` values to 1.

### Before (error)

```python
result = lax.reduce_window(
    x, -jnp.inf, lax.max,
    window_dimensions=(1, 1, 1, 1),
    window_strides=(1, 1, 1, 1),
    padding='VALID',
    base_dilation=(1, 2, 1, 1)  # ERROR: dilation > 1
)
```

### After (fixed)

```python
result = lax.reduce_window(
    x, -jnp.inf, lax.max,
    window_dimensions=(1, 1, 1, 1),
    window_strides=(1, 1, 1, 1),
    padding='VALID',
    base_dilation=(1, 1, 1, 1)  # FIXED: all values are 1
)
```

**Alternative**: Apply manual dilation before reduce-window if needed.

---

## NCC_EVRF018 - Reduce-Window Window Dilation

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler encountered a reduce-window operation with window dilation greater than 1, which is not supported.

**Cause**: Using `window_dilation` parameter with values > 1 in reduce-window operations.

**Resolution**: Set all `window_dilation` values to 1.

### Before (error)

```python
result = lax.reduce_window(
    jnp.ones((1, 4, 4, 1)), -jnp.inf, lax.max,
    window_dimensions=(1, 2, 2, 1),
    window_strides=(1, 1, 1, 1),
    padding='VALID',
    window_dilation=(1, 2, 2, 1)  # ERROR: dilation > 1
)
```

### After (fixed)

```python
result = lax.reduce_window(
    jnp.ones((1, 4, 4, 1)), -jnp.inf, lax.max,
    window_dimensions=(1, 2, 2, 1),
    window_strides=(1, 1, 1, 1),
    padding='VALID',
    window_dilation=(1, 1, 1, 1)  # FIXED: all values are 1
)
```

---

## NCC_EVRF019 - Reduce-Window Wrong Operand Count

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler encountered a reduce-window operation with more or less than 2 operands. Support for reduce_window is available for exactly one input tensor and one initial value for reduction.

**Cause**: Passing multiple input tensors or initial values to reduce-window (e.g., tuple of inputs).

**Resolution**: Split multi-operand reduce-window into multiple single-operand operations.

### Before (error)

```python
# ERROR: 4 operands (2 inputs + 2 init values)
lax.reduce_window(
    (x, x),                    # tuple of two input tensors
    (-jnp.inf, jnp.inf),       # tuple of two initial values
    lambda a, b: (jnp.maximum(a[0], b[0]), jnp.minimum(a[1], b[1])),
    window_dimensions=(1, 2, 2, 1),
    window_strides=(1, 2, 2, 1),
    padding='VALID'
)
```

### After (fixed)

```python
# FIXED: Split into separate operations
# Max pooling
max_pool = lax.reduce_window(
    x,          # single input tensor
    -jnp.inf,   # single initial value
    lax.max,
    window_dimensions=(1, 2, 2, 1),
    window_strides=(1, 2, 2, 1),
    padding='VALID'
)

# Min pooling
min_pool = lax.reduce_window(
    x,          # single input tensor
    jnp.inf,    # single initial value
    lax.min,
    window_dimensions=(1, 2, 2, 1),
    window_strides=(1, 2, 2, 1),
    padding='VALID'
)
```

---

## NCC_EVRF022 - Shift-Right-Arithmetic on Non-32-Bit

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: Shift-right-arithmetic operation on non 32-bit inputs is not supported. Cast the first argument's data type to be S32, U32, or F32.

**Cause**: Using `bitwise_right_shift` with a first argument that is not 32-bit.

**Resolution**: Cast the first argument to a 32-bit type (int32, uint32, or float32).

### Before (error)

```python
def forward(self, input, other):
    return torch.bitwise_right_shift(input, other)

# ERROR: first argument must be 32-bit
input = torch.tensor([16, 32, 64], dtype=torch.int16)
other = torch.tensor([1, 2, 3], dtype=torch.int16)
```

### After (fixed)

```python
def forward(self, input, other):
    return torch.bitwise_right_shift(input, other)

# FIXED: first argument is now 32-bit
input = torch.tensor([16, 32, 64], dtype=torch.int32)
other = torch.tensor([1, 2, 3], dtype=torch.int16)  # second can be non-32-bit
```

---

## NCC_EVRF024 - Output Tensor Size Limit

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The output tensor size limit of 4GB was exceeded.

**Cause**: A single output tensor exceeds the 4GB hardware limit.

**Resolution**: Reduce batch/tensor size, or use tensor parallelism.

### Tensor Parallelism Example

```python
class ParallelSelfAttention(transformers.models.bert.modeling_bert.BertSelfAttention):
    def __init__(self, config, position_embedding_type=None):
        super().__init__(config, position_embedding_type)

        self.query = ColumnParallelLinear(
            config.hidden_size,
            self.all_head_size,
            gather_output=False
        )
        self.key = ColumnParallelLinear(
            config.hidden_size,
            self.all_head_size,
            gather_output=False
        )
        self.value = ColumnParallelLinear(
            config.hidden_size,
            self.all_head_size,
            gather_output=False
        )
        # Shard attention heads across tensor parallel ranks
        tp_size = parallel_state.get_tensor_parallel_size()
        self.num_attention_heads = self.num_attention_heads // tp_size
        self.all_head_size = self.all_head_size // tp_size
```

---

## NCC_EVRF031 - Scatter Out-of-Bounds

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler encountered a scatter out-of-bounds error. The indices created via iota instruction contain values that are beyond the size of the operand dimension.

**Cause**: Iota-generated indices exceed the operand dimension bounds.

**Resolution**: Ensure iota size matches the operand dimension size.

### Before (error)

```python
# operand has size 3 in dimension 0
operand = jnp.zeros((3, 4), dtype=jnp.float32)

# iota generates indices [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
indices = lax.iota(jnp.int32, 10)  # ERROR: size 10 > operand dimension 3
indices = indices.reshape(10, 1)

updates = jnp.ones((10, 4), dtype=jnp.float32)  # 10 updates but only 3 rows

result = lax.scatter(
    operand,
    indices,  # indices [0, 10) but operand only allows [0, 3)
    updates,
    lax.ScatterDimensionNumbers(
        update_window_dims=(1,),
        inserted_window_dims=(0,),
        scatter_dims_to_operand_dims=(0,)
    )
)
```

### After (fixed)

```python
N = 3
D = 4
operand = jnp.zeros((N, D), dtype=jnp.float32)

# FIXED: match iota size to operand dimension
indices = lax.iota(jnp.int32, N)  # size N matches operand dimension
indices = indices.reshape(N, 1)

# FIXED: updates size matches operand dimension
updates = jnp.ones((N, D), dtype=jnp.float32)

result = lax.scatter(
    operand,
    indices,  # FIXED: indices now in valid range [0, 3)
    updates,
    lax.ScatterDimensionNumbers(
        update_window_dims=(1,),
        inserted_window_dims=(0,),
        scatter_dims_to_operand_dims=(0,)
    )
)
```

---

## Quick Reference

| Error Code | Summary | Quick Fix |
|------------|---------|-----------|
| EVRF001 | Unsupported operator | Use alternative operator, check `neuronx-cc list-operators` |
| EVRF004 | Complex data types | Offload to CPU or emulate with real/imag parts |
| EVRF005 | Unsupported FP8 types | Convert to float16/bfloat16 |
| EVRF006 | Unsupported RNG algorithm | Use default RNG |
| EVRF007 | Instruction limit exceeded | Apply model parallelism |
| EVRF009 | Activation memory exceeded | Reduce batch size or use parallelism |
| EVRF010 | Simultaneous dilation | Use input OR kernel dilation, not both |
| EVRF011 | Strided + dilated input | Remove stride or input dilation |
| EVRF013 | TopK integer inputs | Cast to float before TopK |
| EVRF015 | Unrecognized custom call | Use supported custom call target |
| EVRF016 | Scatter-reduce int/bool | Cast to float types |
| EVRF017 | Reduce-window base dilation | Set base_dilation to (1,1,1,1) |
| EVRF018 | Reduce-window window dilation | Set window_dilation to (1,1,1,1) |
| EVRF019 | Reduce-window wrong operands | Split into single-operand operations |
| EVRF022 | Shift-right non-32-bit | Cast first argument to 32-bit |
| EVRF024 | Output tensor > 4GB | Reduce tensor size or use parallelism |
| EVRF031 | Scatter out-of-bounds | Match iota size to operand dimension |

## Related References

- `compiler-error-codes.md` - Quick reference index for all NCC_* errors
- `ncc-memory-resource-errors.md` - Memory and resource limit errors
- `ncc-type-operation-errors.md` - Type and operation errors
