# Compiler Artifacts Mode

Debug mode that preserves compiler outputs for inspection. Use when you need to understand compilation behavior or diagnose performance issues.

## When to Use

- Kernel compiles differently than expected
- Need to inspect compiler log for errors or warnings
- Performance issues requiring compiler-level analysis
- Need the compiled NEFF for profiling

## Debug Flags

```python
import os

os.environ["NEURON_CC_FLAGS"] = (
    "--target trn2 "
    "--lnc 1 "
    "--verbose=info"
)
```

| Flag | Purpose |
|------|---------|
| `--target <platform>` | Target platform (`trn1`, `trn2`, `inf2`) |
| `--lnc <degree>` | Logical NeuronCore config (1 or 2, default 2 on trn2) |
| `--verbose <level>` | Output verbosity: `info`, `warning`, `error`, `critical`, `debug` |

## Finding the Compiler Temp Folder

After compilation, the last ~50 lines of output contain the temp directory path. Look for:

```
/tmp/<username>/neuroncc_compile_workdir/<uuid>/
```

To find recent compilation directories:

```bash
ls -lt /tmp/$USER/neuroncc_compile_workdir/ | head -5
```

## Generated Artifacts

| File | Description |
|------|-------------|
| `*.neff` | Compiled Neuron Executable File Format (binary) |
| `log-neuron-cc.txt` | Detailed compiler log |

### *.neff

The compiled binary executed on Neuron hardware. Use `neuron-explorer` tools to analyze performance.

### log-neuron-cc.txt

Complete compiler log including:
- Compilation phases and timing
- Warnings and diagnostics
- Memory allocation decisions
- Optimization passes applied

## Complete Debug Script

```python
import os
import torch
from torch_xla.core import xla_model as xm
import nki
import nki.language as nl
import nki.isa as nisa

# Debug configuration
os.environ["NEURON_CC_FLAGS"] = (
    "--target trn2 "
    "--lnc 1 "
    "--verbose=info"
)

# Optional: Capture runtime profiles
os.environ['NEURON_RT_INSPECT_ENABLE'] = '1'
os.environ['NEURON_RT_INSPECT_DEVICE_PROFILE'] = '1'
os.environ['NEURON_RT_INSPECT_OUTPUT_DIR'] = './output'
os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"

@nki.jit
def my_kernel(input_tensor):
    # Your kernel implementation
    tile = nl.ndarray(input_tensor.shape, dtype=input_tensor.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=tile, src=input_tensor)
    result = nl.ndarray(input_tensor.shape, dtype=input_tensor.dtype, buffer=nl.sbuf)
    nisa.activation(dst=result, data=tile, op=nl.exp)
    output = nl.ndarray(input_tensor.shape, dtype=input_tensor.dtype, buffer=nl.shared_hbm)
    nisa.dma_copy(dst=output, src=result)
    return output

device = xm.xla_device()
x = torch.randn((64, 128), dtype=torch.float32).to(device=device)

y = my_kernel(x)
print(y)  # Triggers compilation

# After execution, find artifacts:
# ls -lt /tmp/$USER/neuroncc_compile_workdir/ | head -5
```

## Artifact Analysis Workflow

1. Run kernel with debug flags (`--verbose=info`)
2. Examine `log-neuron-cc.txt` for errors and warnings
3. Use `neuron-explorer` on `*.neff` for performance analysis

## Notes

- Debug flags increase compilation time
- Use standard flags (`--target --lnc`) for regular development
- Only enable verbose mode when investigating compiler issues
