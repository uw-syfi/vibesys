# Common Types

## Overview

Enum types used across NKI kernel configurations for specifying output layouts, normalization modes, activation functions, quantization strategies, and MoE (Mixture of Experts) parameters. Use these enums to configure kernel behavior in a type-safe manner.

## Quick Reference

| Enum | Description |
|------|-------------|
| `QKVOutputLayout` | Output tensor layout for QKV projections |
| `NormType` | Normalization type selection (none, RMS, LayerNorm) |
| `ActFnType` | Activation function type (SiLU, GELU, Swish) |
| `RouterActFnType` | Activation type for MoE router TopK kernel |
| `ExpertAffinityScaleMode` | Scaling mode for MoE expert affinity scores |
| `QuantizationType` | Quantization strategy (none, static, row, MX) |
| `GateUpDim` | Index selector for gate/up projection in MLP |

## Import Options

**Default** — inline the source into your kernel file.
Source: `references/nkilib/core/utils/common_types.py`

**If nkilib is installed** in the user's environment:
```python
from nkilib.core.utils.common_types import QKVOutputLayout, NormType, ActFnType
from nkilib.core.utils.common_types import RouterActFnType, ExpertAffinityScaleMode
from nkilib.core.utils.common_types import QuantizationType, GateUpDim
```

## API Documentation

### `QKVOutputLayout`

Specifies the memory layout for QKV (Query/Key/Value) projection outputs.

| Value | Int | Layout Shape | Description |
|-------|-----|-------------|-------------|
| `BSD` | 0 | `(b, s, (n_q_heads + 2 * n_kv_heads) * d_head)` | Batch-Sequence-Dim interleaved layout |
| `NBSd` | 1 | `(num_heads, b, s, d_head)` | Heads-first with sequence-major inner layout |
| `NBdS` | 2 | `(num_heads, b, d_head, s)` | Heads-first with head-dim-major inner layout |

**Example:**
```python
from nkilib.core.utils.common_types import QKVOutputLayout

layout = QKVOutputLayout.NBSd  # Standard heads-first layout
if layout == QKVOutputLayout.BSD:
    # Handle interleaved output
    pass
```

### `NormType`

Specifies the normalization method to apply.

| Value | Int | Description |
|-------|-----|-------------|
| `NO_NORM` | 0 | No normalization applied |
| `RMS_NORM` | 1 | Root Mean Square normalization |
| `LAYER_NORM` | 2 | Layer normalization (mean + variance) |
| `RMS_NORM_SKIP_GAMMA` | 3 | RMS normalization without the gamma scaling parameter |

**Example:**
```python
from nkilib.core.utils.common_types import NormType

norm = NormType.RMS_NORM
if norm == NormType.RMS_NORM_SKIP_GAMMA:
    # Skip gamma multiplication step
    pass
```

### `ActFnType`

Specifies the activation function for MLP/FFN layers.

| Value | Int | Description |
|-------|-----|-------------|
| `SiLU` | 0 | Sigmoid Linear Unit (x * sigmoid(x)) |
| `GELU` | 1 | Gaussian Error Linear Unit |
| `GELU_Tanh_Approx` | 2 | GELU with tanh approximation |
| `Swish` | 3 | Swish activation (same as SiLU with beta=1) |

**Example:**
```python
from nkilib.core.utils.common_types import ActFnType

act_fn = ActFnType.SiLU  # Used in LLaMA-style models
```

### `RouterActFnType`

Specifies the activation type for Mixture-of-Experts (MoE) router TopK kernel.

| Value | Int | Description |
|-------|-----|-------------|
| `SIGMOID` | 0 | Sigmoid activation for routing scores |
| `SOFTMAX` | 1 | Softmax activation for routing scores |

Implements `__str__` returning the lowercase name (e.g., `"sigmoid"`, `"softmax"`).

**Example:**
```python
from nkilib.core.utils.common_types import RouterActFnType

router_act = RouterActFnType.SOFTMAX
print(router_act)  # prints: "softmax"
```

### `ExpertAffinityScaleMode`

Controls when and how expert affinity scores are scaled in MoE routing.

| Value | Int | Description |
|-------|-----|-------------|
| `NO_SCALE` | 0 | No scaling applied to affinity scores |
| `POST_SCALE` | 1 | Scale applied after expert selection |
| `PRE_SCALE` | 2 | Scale applied before expert selection |
| `PRE_SCALE_DELAYED` | 3 | Pre-scaling with delayed application |

**Example:**
```python
from nkilib.core.utils.common_types import ExpertAffinityScaleMode

scale_mode = ExpertAffinityScaleMode.POST_SCALE
```

### `QuantizationType`

Specifies the quantization strategy for weight or activation tensors.

| Value | Int | Description |
|-------|-----|-------------|
| `NONE` | 0 | No quantization (full precision) |
| `STATIC` | 1 | Static quantization with fixed scale factors |
| `ROW` | 2 | Per-row quantization with individual scale factors |
| `MX` | 3 | Microscaling (MX) quantization format |

**Example:**
```python
from nkilib.core.utils.common_types import QuantizationType

quant = QuantizationType.MX  # MX format for gen4 hardware
if quant != QuantizationType.NONE:
    # Apply dequantization logic
    pass
```

### `GateUpDim`

Index selector for the gate and up projections in gated MLP architectures (e.g., SwiGLU).

| Value | Int | Description |
|-------|-----|-------------|
| `GATE` | 0 | Index for the gate projection |
| `UP` | 1 | Index for the up projection |

**Example:**
```python
from nkilib.core.utils.common_types import GateUpDim

# Access gate and up projections from a combined weight tensor
gate_weight = combined_weights[GateUpDim.GATE.value]
up_weight = combined_weights[GateUpDim.UP.value]
```

## Usage Examples

### Pattern 1: Configuring a fused QKV + normalization kernel
```python
from nkilib.core.utils.common_types import QKVOutputLayout, NormType

def launch_qkv_kernel(input_tensor, weights, config):
    config.output_layout = QKVOutputLayout.NBSd
    config.norm_type = NormType.RMS_NORM
    # ... launch kernel with config
```

### Pattern 2: Selecting MoE router parameters
```python
from nkilib.core.utils.common_types import RouterActFnType, ExpertAffinityScaleMode

router_config = {
    "activation": RouterActFnType.SIGMOID,
    "scale_mode": ExpertAffinityScaleMode.POST_SCALE,
    "top_k": 2,
}
```

### Pattern 3: Quantization-aware kernel dispatch
```python
from nkilib.core.utils.common_types import QuantizationType

def get_matmul_kernel(quant_type):
    if quant_type == QuantizationType.MX:
        return mx_quantized_matmul
    elif quant_type == QuantizationType.ROW:
        return row_quantized_matmul
    else:
        return standard_matmul
```

## Dependencies

None. This module only depends on Python's `enum.Enum` standard library.

## Source

See `references/nkilib/core/utils/common_types.py` for the full implementation.
