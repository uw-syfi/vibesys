# NKI Compiler Flags Reference

Standard compiler flags for debugging NKI kernels on Neuron hardware.

## Environment Variable

All compiler flags are passed via the `NEURON_CC_FLAGS` environment variable:

```python
import os
os.environ["NEURON_CC_FLAGS"] = "--target trn2 --lnc 1"
```

## Core Flags

| Flag | Values | Required | Description |
|------|--------|----------|-------------|
| `--target` | trn1, trn1n, trn2, trn3, inf2 | Yes | Target hardware platform |
| `--lnc` | 1, 2 | Recommended | Logical NeuronCore count |
| `--verbose` | info, warning, error, debug | No | Logging verbosity level |

### --target

Specifies the target Neuron hardware platform. Must match the `platform_target` in your `@nki.jit` decorator.

| Target | Hardware | Generation | FP8 Support |
|--------|----------|------------|-------------|
| `trn1` | Trainium 1 | gen2 | No |
| `trn1n` | Trainium 1n | gen2 | No |
| `inf2` | Inferentia 2 | gen2 | No |
| `trn2` | Trainium 2 | gen3 | Yes |
| `trn3` | Trainium 3 | gen4 | Yes |

### --lnc (Logical NeuronCore)

Controls how many NeuronCores the kernel is sharded across.

| Value | Use Case |
|-------|----------|
| `1` | Single-core debugging (recommended for development) |
| `2` | Multi-core execution (default on trn2/trn3) |

**Recommendation:** Use `--lnc 1` during debugging for simpler error messages and faster compilation.

### --verbose

Controls compiler output verbosity.

| Level | Output | Use When |
|-------|--------|----------|
| `info` | Progress messages | Standard debugging |
| `warning` | Diagnostic warnings | Default behavior |
| `error` | Compilation errors only | Minimal output |
| `debug` | Extensive internal info | Deep debugging |

## Standard Debugging Configuration

```python
# Minimal flags for standard debugging
os.environ["NEURON_CC_FLAGS"] = "--target trn2 --lnc 1"
```

This configuration:
- Targets Trainium 2 hardware (gen3)
- Uses single NeuronCore for simpler debugging
- Uses default verbosity (warning)

## Platform-Specific Configurations

### Trainium 1 (gen2)

```python
os.environ["NEURON_CC_FLAGS"] = "--target trn1 --lnc 1"
os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn1"

@nki.jit
def kernel(...):
    ...
```

### Trainium 2 (gen3)

```python
os.environ["NEURON_CC_FLAGS"] = "--target trn2 --lnc 1"
os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"

@nki.jit
def kernel(...):
    ...
```

### Trainium 3 (gen4)

```python
os.environ["NEURON_CC_FLAGS"] = "--target trn3 --lnc 1"
os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn3"

@nki.jit
def kernel(...):
    ...
```

### Inferentia 2 (gen2)

```python
os.environ["NEURON_CC_FLAGS"] = "--target inf2 --lnc 1"
os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "inf2"

@nki.jit
def kernel(...):
    ...
```

## Verbose Debugging

For more detailed compiler output during troubleshooting:

```python
os.environ["NEURON_CC_FLAGS"] = "--target trn2 --lnc 1 --verbose=info"
```

## Common Flag Combinations

| Scenario | Flags |
|----------|-------|
| Basic debugging | `--target trn2 --lnc 1` |
| Verbose debugging | `--target trn2 --lnc 1 --verbose=info` |
| Multi-core test | `--target trn2 --lnc 2` |
| Production build | `--target trn2` (uses platform defaults) |

## Notes

- The `--target` flag MUST match the `platform_target` parameter in the `@nki.jit` decorator
- Use `--lnc 1` during development for simpler error diagnosis
- For advanced debugging with intermediate artifacts, see `compiler-artifacts.md`
- Flags set via `NEURON_CC_FLAGS` apply to all neuronx-cc compilations in the process
