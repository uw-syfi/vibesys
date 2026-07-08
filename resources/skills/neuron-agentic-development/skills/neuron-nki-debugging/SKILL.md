---
name: neuron-nki-debugging
description: |
  This skill guides debugging NKI compilation errors on Neuron hardware. Use when
  encountering "compiler error on device", "debug NKI kernel", "test kernel on trn2/trn3",
  "neuronx-cc compilation failed", "validate kernel on hardware", "run kernel on trainium",
  or asking "how to debug NKI compilation errors on device".
argument-hint: "[kernel file]"
---

# Debugging NKI on Neuron Hardware

This skill provides a workflow for debugging NKI kernel compilation and execution on Trainium/Inferentia hardware.

## Quick Start

Minimal working example to test kernel compilation on device:

```python
import os
import torch
from torch_xla.core import xla_model as xm
import nki
import nki.language as nl
import nki.isa as nisa

os.environ["NEURON_CC_FLAGS"] = "--target trn2 --lnc 1"
os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"

@nki.jit
def add_kernel(a_input, b_input):
    """Element-wise addition kernel."""
    a_tile = nl.ndarray(a_input.shape, dtype=a_input.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=a_tile, src=a_input[0:a_input.shape[0], 0:a_input.shape[1]])

    b_tile = nl.ndarray(b_input.shape, dtype=b_input.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=b_tile, src=b_input[0:b_input.shape[0], 0:b_input.shape[1]])

    c_tile = nl.ndarray(a_input.shape, dtype=a_input.dtype, buffer=nl.sbuf)
    nisa.tensor_tensor(dst=c_tile, data1=a_tile, data2=b_tile, op=nl.add)

    c_output = nl.ndarray(a_input.shape, dtype=a_input.dtype, buffer=nl.shared_hbm)
    nisa.dma_copy(dst=c_output, src=c_tile)
    return c_output

# Get XLA device and run
device = xm.xla_device()
a = torch.ones((4, 3), dtype=torch.float16).to(device=device)
b = torch.ones((4, 3), dtype=torch.float16).to(device=device)

c = add_kernel(a, b)
print(c)  # Forces XLA compilation and execution
```

## Prerequisites

Before running kernels on device, resolve the NKI virtual environment path:

1. Check environment: `echo $NKI_VENV_PATH`
2. If empty, read `.claude/nki-dev-suite.local.md` and extract `nki_venv_path` from YAML frontmatter
3. If still not found, report: "NKI_VENV_PATH not configured. Set the environment variable or create .claude/nki-dev-suite.local.md with nki_venv_path in frontmatter."

Activate before running any device tests:
```bash
source $NKI_VENV_PATH/bin/activate
```

## Platform Detection

Before compilation, detect the current hardware platform:

**Current platform:**
`!neuron-ls | head -3`

### Platform Target Mapping

| Hardware | Instance | Target Flag | Generation |
|----------|----------|-------------|------------|
| Trainium 1 | trn1 | `--target trn1` | gen2 |
| Trainium 1n | trn1n | `--target trn1n` | gen2 |
| Inferentia 2 | inf2 | `--target inf2` | gen2 |
| Trainium 2 | trn2 | `--target trn2` | gen3 |
| Trainium 3 | trn3 | `--target trn3` | gen4 |

Match the `--target` flag and `platform_target` decorator argument to your detected hardware.

## Standard Debugging Workflow

### Step 1: Set Environment Variables

```python
import os

# Standard debugging flags (minimal, fast compilation)
os.environ["NEURON_CC_FLAGS"] = "--target trn2 --lnc 1"
os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"

# Pin to a specific neuron core to avoid conflicts with concurrent sessions
os.environ["NEURON_RT_VISIBLE_CORES"] = "0"
```

| Flag | Purpose |
|------|---------|
| `--target` | Hardware platform (trn1, trn2, trn3, inf2) |
| `--lnc 1` | Single NeuronCore (simplifies debugging) |
| `NEURON_RT_VISIBLE_CORES` | Pin to specific core(s) — prevents contention when multiple agents run concurrently |

See `references/compiler-flags.md` for complete flag reference.

### Step 2: Apply Matching Decorator

```python
@nki.jit  # Must match --target and NEURON_PLATFORM_TARGET_OVERRIDE
def my_kernel(input_tensor):
    ...
```

The `platform_target` environment variable MUST match the `--target` in NEURON_CC_FLAGS.


### Step 3: Create Test Script

```python
import os
import torch
from torch_xla.core import xla_model as xm
import nki

os.environ["NEURON_CC_FLAGS"] = "--target trn2 --lnc 1"
os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"

@nki.jit
def kernel(input_tensor):
    # Your kernel implementation
    ...
    return output_tensor

# XLA device execution pattern
device = xm.xla_device()
input_data = torch.randn((128, 512), dtype=torch.float32).to(device=device)

output = kernel(input_data)
print(output)  # Forces XLA compilation - triggers actual compilation
```

### Step 4: Run and Observe

```bash
source $NKI_VENV_PATH/bin/activate
python your_test_script.py
```

Compilation errors appear in the console output. The `print()` statement forces XLA compilation, which triggers the neuronx-cc compiler.

### Step 5: Validate Numerically

Compare device output against a **CPU-computed** reference using multiple complementary checks — no single metric catches all issues:

- **atol / rtol** (`torch.allclose`): Per-element pass/fail gate
- **Maximum absolute difference**: Worst-case outlier check
- **Norm of the difference tensor**: Detects widespread small drift
- **Cosine similarity**: Catches directional errors in high-dimensional outputs

**Important: Compute references on CPU, not on the XLA device.** Every XLA graph compiled on-device generates a separate NEFF file. Running reference operations (e.g., `torch.matmul`, `torch.softmax`) on the XLA device creates extra NEFFs, making it hard to identify which NEFF belongs to the NKI kernel during profiling.

```python
# CORRECT: Reference computed on CPU — only the NKI kernel generates a NEFF
cpu_input = input_data.cpu()
reference_output = reference_implementation(cpu_input)
device_output = output.cpu()

# Use dtype-appropriate tolerances
assert torch.allclose(device_output, reference_output, rtol=1e-5, atol=1e-8)
```

```python
# WRONG: Reference computed on device — generates an extra NEFF
reference_output = reference_implementation(input_data)  # Compiles to separate NEFF!
```

**For complex kernels where the final output is wrong:** decompose the kernel into logical stages and examine intermediate tensors at each boundary. Store intermediates to HBM temporarily, compare each against the matching reference stage, and binary-search for the stage that introduces the error. Once the failing stage is identified, test it with minimal input shapes (e.g., a single 128x128 tile) to isolate whether the issue is in the core logic or in tiling/boundary handling. Remove the debug stores once the issue is resolved.

## Compiler Artifacts Mode

For advanced debugging that preserves compiler outputs for inspection, use when you need to understand detailed compilation behavior. 

**When to use:** "compiler artifacts", "compiler flags", "inspect compiler log"

See `references/compiler-artifacts.md` for:
- Compiler debug flag configuration (`--verbose`, `--target`, `--lnc`)
- Finding the compiler temp folder
- Understanding generated artifacts (`*.neff`, `log-neuron-cc.txt`)

## Error Resolution

### Error Categories

| Error Pattern | Category | Reference |
|--------------|----------|-----------|
| `NCC_EVRF*` | Verification error | See `references/ncc-verification-errors.md` |
| `NCC_EOOM*` | Out of memory | See `references/ncc-memory-resource-errors.md` |
| `NCC_E*` (other) | Type/operation error | See `references/ncc-type-operation-errors.md` |

### Quick Reference

See `references/compiler-error-codes.md` for the complete index of all 28 NCC_* error codes.

### Common Error Quick Fixes

| Error Code | Category | Quick Fix |
|------------|----------|-----------|
| `NCC_EVRF001` | Unsupported operator | Use alternative operator from `neuronx-cc list-operators` |
| `NCC_EOOM001` | Memory exceeded | Reduce batch size, use tensor/pipeline parallelism |
| `NCC_EVRF007` | Instruction limit | Apply model parallelism |
| `NCC_EVRF005` | Unsupported FP8 type | Convert to float16/bfloat16 or use gen3+ hardware |
| `NCC_EARG001` | LNC configuration | Use supported LNC count for target hardware |
| `NCC_EVRF024` | Output tensor > 4GB | Reduce tensor size or use tensor parallelism |

## Profiling (Optional)

To capture execution traces for profiling:

```python
# Add before running kernel
os.environ['NEURON_RT_INSPECT_ENABLE'] = '1'
os.environ['NEURON_RT_INSPECT_DEVICE_PROFILE'] = '1'
os.environ['NEURON_RT_INSPECT_OUTPUT_DIR'] = './output'
```

This captures NEFF (compiled binary) and NTFF (execution trace) files in the output directory.

## Complete Example

```python
import os
import torch
from torch_xla.core import xla_model as xm
import nki
import nki.language as nl
import nki.isa as nisa

# Standard debugging configuration
os.environ["NEURON_CC_FLAGS"] = "--target trn2 --lnc 1"

# Optional: Enable profiling
os.environ['NEURON_RT_INSPECT_ENABLE'] = '1'
os.environ['NEURON_RT_INSPECT_DEVICE_PROFILE'] = '1'
os.environ['NEURON_RT_INSPECT_OUTPUT_DIR'] = './output'
os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"

@nki.jit
def softmax_kernel(input_tensor):
    """Simple softmax along last dimension."""
    # Load input tile
    tile = nl.ndarray(input_tensor.shape, dtype=input_tensor.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=tile, src=input_tensor)

    # Compute softmax
    exp_tile = nl.ndarray(input_tensor.shape, dtype=input_tensor.dtype, buffer=nl.sbuf)
    nisa.activation(dst=exp_tile, data=tile, op=nl.exp)

    sum_tile = nl.ndarray((input_tensor.shape[0], 1), dtype=input_tensor.dtype, buffer=nl.sbuf)
    nisa.tensor_reduce(dst=sum_tile, data=exp_tile, op=nl.add, axis=(1,))

    recip_sum = nl.ndarray((input_tensor.shape[0], 1), dtype=input_tensor.dtype, buffer=nl.sbuf)
    nisa.reciprocal(dst=recip_sum, data=sum_tile)

    result = nl.ndarray(input_tensor.shape, dtype=input_tensor.dtype, buffer=nl.sbuf)
    nisa.tensor_scalar(dst=result, data=exp_tile, op0=nl.multiply, operand0=recip_sum)

    # Store output
    output = nl.ndarray(input_tensor.shape, dtype=input_tensor.dtype, buffer=nl.shared_hbm)
    nisa.dma_copy(dst=output, src=result)
    return output

# Test execution
device = xm.xla_device()
x = torch.randn((64, 128), dtype=torch.float32).to(device=device)

y = softmax_kernel(x)
print(y)  # Triggers compilation

# Validate against PyTorch reference
reference = torch.softmax(x.cpu(), dim=-1)
assert torch.allclose(y.cpu(), reference, rtol=1e-4, atol=1e-6)
print("Validation passed!")
```

## Configuration

**Required settings:**

| Setting | Source | Description |
|---------|--------|-------------|
| `nki_venv_path` | `.claude/nki-dev-suite.local.md` or `NKI_VENV_PATH` | Python venv with neuronx packages |

**Related skills:**

| Skill | Use When |
|-------|----------|
| `/neuron-nki-profiling` | Profile kernel performance |
| `/neuron-nki-docs` | Look up API documentation and error codes |

