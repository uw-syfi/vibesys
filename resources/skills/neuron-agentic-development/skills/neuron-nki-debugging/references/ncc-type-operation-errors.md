# NCC Type and Operation Errors

Detailed reference for Neuron Compiler type, argument, and operation errors. These errors occur when using unsupported data types, configurations, or operators.

## Supported Data Types by Hardware

| Data Type | gen2 (Trn1/Inf2) | gen3 (Trn2) | gen4 (Trn3) |
|-----------|------------------|-------------|-------------|
| float32 | Yes | Yes | Yes |
| float16 | Yes | Yes | Yes |
| bfloat16 | Yes | Yes | Yes |
| int32 | Yes | Yes | Yes |
| int16 | Yes | Yes | Yes |
| int8 | Yes | Yes | Yes |
| fp8_e4m3 | No | Yes | Yes |
| fp8_e5m2 | No | Yes | Yes |

**Unsupported types (all hardware)**:
- complex64, complex128
- float8_e4m3fnuz, float8_e4m3b11fnuz, float8_e5m2fnuz
- float4_e2m1fn

---

## NCC_EARG001 - Unsupported LNC Configuration

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: This error occurs when you attempt to use a Logical Neuron Core (LNC) configuration that is not supported by the target Neuron architecture.

**Cause**: Specifying an LNC count not supported by the target hardware.

### Supported LNC Configurations

| Hardware | Supported LNC Values |
|----------|---------------------|
| Trn1 (gen2) | 1 |
| Inf2 (gen2) | 1, 2 |
| Trn2 (gen3) | 1, 2, 4 |
| Trn3 (gen4) | 1, 2, 4, 8 |

### Understanding LNC

- **Physical Neuron Core**: Actual hardware compute unit on the chip with dedicated compute resources and memory
- **Logical Neuron Core**: Software abstraction grouping multiple physical cores
- **Configuration**: Controlled via `NEURON_LOGICAL_NC_CONFIG` environment variable or `--lnc` flag

### Before (error)

```python
# ERROR: lnc=2 not supported on trn1
traced_model = torch_neuronx.trace(
    model,
    input,
    compiler_args=['--lnc', '2']
)
```

### After (fixed)

```python
# FIXED: use supported LNC value for trn1
traced_model = torch_neuronx.trace(
    model,
    input,
    compiler_args=['--lnc', '1']
)
```

**Resolution**: Check target hardware and use a supported LNC value.

---

## NCC_ESPP004 - Unsupported Data Type for Codegen

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler encountered a data type that is not supported for code generation.

**Cause**: Using experimental or unsupported data types like `float4_e2m1fn`.

**Resolution**: Convert to a supported data type.

### Before (error)

```python
import numpy as np
import jax.numpy as jnp
from jax._src import dtypes
from jax._src.lax import lax as lax_internal

# ERROR: float4_e2m1fn type not supported
dtype = np.dtype(dtypes.float4_e2m1fn)
val = lax_internal._convert_element_type(0, dtype, weak_type=False)
```

### After (fixed)

```python
import jax.numpy as jnp
from jax._src.lax import lax as lax_internal

# FIXED: use supported dtype
dtype = jnp.bfloat16
val = lax_internal._convert_element_type(0, dtype, weak_type=False)
```

---

## NCC_ESPP047 - Unsupported FP8 Data Type

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler found usage of an unsupported 8-bit floating-point data type.

**Cause**: Using unsupported FP8 variants. Note that standard FP8 (fp8_e4m3, fp8_e5m2) is only supported on gen3+ hardware.

**Resolution**: Convert to a supported type for the target hardware.

### Before (error)

```python
class Model(nn.Module):
    def __init__(self):
        super().__init__()
        self.linear1 = nn.Linear(10, 20)
        self.linear2 = nn.Linear(20, 10)

    def forward(self, x):
        x = self.linear1(x)
        x = torch.relu(x)
        x = self.linear2(x)
        return x

# ERROR: unsupported 8-bit floating-point data type
input_tensor = torch.randn(1, 10).to(torch.float8_e4m3fn)
```

### After (fixed)

```python
class Model(nn.Module):
    def __init__(self):
        super().__init__()
        self.linear1 = nn.Linear(10, 20)
        self.linear2 = nn.Linear(20, 10)

    def forward(self, x):
        x = self.linear1(x)
        x = torch.relu(x)
        x = self.linear2(x)
        return x

input_tensor = torch.randn(1, 10).to(torch.float8_e4m3fn)
# FIXED: Convert to a supported type
input_tensor = input_tensor.to(torch.float16)
```

**See also**: NCC_EVRF005 (similar FP8 type errors)

---

## NCC_EHCA005 - Unrecognized Custom Call Target

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler encountered a custom call instruction with a target name that is not recognized.

**Cause**: Using a custom call target name not in the supported list.

**Resolution**: Use a recognized custom call target from the supported list.

### Recognized Custom Call Targets (28 total)

**Activation Functions**:
- `AwsNeuronErf`
- `AwsNeuronGelu`
- `AwsNeuronGeluApprxTanh`
- `AwsNeuronGeluBackward`
- `AwsNeuronSilu`
- `AwsNeuronSiluBackward`

**Normalization**:
- `AwsNeuronRmsNorm`
- `AwsNeuronSoftmax`
- `AwsNeuronSoftmaxBackward`

**Compute Operations**:
- `AwsNeuronCollectiveMatmul`
- `AwsNeuronIntMatmult`
- `AwsNeuronArgMax`
- `AwsNeuronArgMin`
- `AwsNeuronTopK`

**Utility Operations**:
- `AwsNeuronDropoutMaskV1`
- `AwsNeuronCustomNativeKernel`
- `AwsNeuronCustomOp`
- `AwsNeuronDevicePrint`

**Resize Operations**:
- `ResizeNearest`
- `ResizeBilinear`
- `ResizeNearestGrad`

**Sharding and Communication**:
- `AwsNeuronLNCShardingConstraint`
- `AwsNeuronTransferWithStaticRing`

**Module Markers**:
- `AwsNeuronModuleMarkerStart-Forward`
- `AwsNeuronModuleMarkerStart-Backward`
- `AwsNeuronModuleMarkerEnd-Forward`
- `AwsNeuronModuleMarkerEnd-Backward`
- `NeuronBoundaryMarker-Start`
- `NeuronBoundaryMarker-End`

### Before (error)

```python
def lowering(ctx, x_val):
    result_type = ir.RankedTensorType(x_val.type)
    # ERROR: unrecognized target name
    return hlo.CustomCallOp(
        [result_type],
        [x_val],
        call_target_name="UNRECOGNIZED_TARGET",
        has_side_effect=ir.BoolAttr.get(False),
    ).results
```

### After (fixed)

```python
def lowering(ctx, x_val):
    result_type = ir.RankedTensorType(x_val.type)
    # FIXED: use recognized target
    return hlo.CustomCallOp(
        [result_type],
        [x_val],
        call_target_name="AwsNeuronSilu",
        has_side_effect=ir.BoolAttr.get(False),
        backend_config=ir.StringAttr.get(""),
        api_version=ir.IntegerAttr.get(ir.IntegerType.get_signless(32), 2),
    ).results
```

**See also**: NCC_EVRF015 (same error, same resolution)

---

## NCC_ESFH002 - 64-Bit Constant Conversion Error

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The compiler encountered a unsigned 64-bit integer constant with a value that cannot be safely converted to 32-bit representation.

**Cause**: Neuron hardware operates on 32-bit or narrower data types. 64-bit constants exceeding the 32-bit range cannot be safely converted.

**Resolution**: Use uint32 for constants when possible and restructure code to avoid large constants.

### 32-Bit Integer Limits

| Type | Min | Max |
|------|-----|-----|
| int32 | -2,147,483,648 | 2,147,483,647 |
| uint32 | 0 | 4,294,967,295 |

### Before (error)

```python
@jax.jit
def foo():
    x = jnp.array([1, 2, 3], dtype=jnp.uint64)
    # ERROR: large constant exceeds uint32 max (4,294,967,295)
    large_constant = jnp.uint64(5_000_000_000)
    return x + large_constant
```

### After (fixed)

```python
@jax.jit
def test():
    x = jnp.array([1, 2, 3], dtype=jnp.uint32)
    # FIXED: use uint32 (value must fit in 32-bit range)
    constant = jnp.uint32(1_000_000_000)  # Within uint32 range
    return x + constant
```

**Note**: If you need to work with values > 4.29 billion, consider:
- Using multiple 32-bit operations
- Representing values in a different scale
- Offloading to CPU for 64-bit arithmetic

---

## NCC_EUOC002 - Unsupported Operator

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: An unsupported operator was used.

**Cause**: Using an operator that Neuron hardware does not support.

**Resolution**: Use alternative operators. List supported operators with `neuronx-cc list-operators --framework XLA`.

### Before (error)

```python
class Model(torch.nn.Module):
    def forward(self, A, b):
        # ERROR: triangular_solve not supported
        return torch.triangular_solve(b, A)
```

### After (fixed)

```python
class Model(torch.nn.Module):
    def forward(self, A, b):
        # FIXED: Use mathematically equivalent alternative
        # Note: torch.inverse + matmul is slower but supported
        A_inv = torch.inverse(A)
        return A_inv @ b
```

### Common Unsupported Operators and Alternatives

| Unsupported | Alternative |
|-------------|-------------|
| `triangular_solve` | `inverse` + matrix multiply |
| Complex FFT | Split into real/imaginary parts |
| Some custom CUDA kernels | Rewrite using supported ops |

**See also**: NCC_EVRF001 (same error message and resolution)

---

## NCC_EXSP001 - Expansion Error

**Hardware**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

**Error message**: The combined memory needed for the model's activation tensors exceeds the high-bandwidth memory limit.

**Cause**: During tensor expansion phase, memory requirements exceed HBM limits.

**Resolution**: Same strategies as memory errors:
1. Reduce batch/tensor size
2. Use pipeline/tensor parallelism via neuronx-distributed

### Tensor Parallelism Example

```python
from neuronx_distributed.parallel_layers import ColumnParallelLinear
from neuronx_distributed import parallel_state

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

**See also**: NCC_EOOM001, NCC_EOOM002 for detailed memory strategies

---

## Quick Reference

| Error Code | Category | Summary | Quick Fix |
|------------|----------|---------|-----------|
| EARG001 | Configuration | Unsupported LNC config | Use supported LNC for target hardware |
| ESPP004 | Data Type | Unsupported dtype for codegen | Use fp32/fp16/bf16 |
| ESPP047 | Data Type | Unsupported FP8 type | Convert to float16 or check gen3+ |
| EHCA005 | Custom Call | Unrecognized target | Use supported custom call target |
| ESFH002 | Constants | 64-bit constant overflow | Use uint32 constants |
| EUOC002 | Operator | Unsupported operator | Use alternative operator |
| EXSP001 | Memory | Expansion memory exceeded | Reduce size, use parallelism |

## Related References

- `compiler-error-codes.md` - Quick reference index for all NCC_* errors
- `ncc-verification-errors.md` - Verification errors (EVRF*)
- `ncc-memory-resource-errors.md` - Memory and resource limit errors
