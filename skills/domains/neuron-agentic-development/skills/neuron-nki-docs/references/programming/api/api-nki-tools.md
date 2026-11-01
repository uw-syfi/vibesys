# nki — Tools

> **Module**: nki
> **Functions**: 1

## Overview

Tools instructions.

## Functions

### nki.jit {#nki-jit}

`nki.jit(fn, compile_config, specialization_config, **kwargs)`

**Signature:**
```python
nki.jit(fn=None, compile_config=None, specialization_config=None, **kwargs)
```

Just-in-time compile a top-level NKI function to run on NeuronDevices.

The returned callable detects the current framework and compiles the function as a
custom operator. It detects the current framework by inspecting its arguments:

- `torch.Tensor`: uses PyTorch integration.
- `jax.Array`: uses JAX integration.
- `np.ndarray`: compiles and executes standalone kernel, without a framework.

You might need to explicitly set the target platform using the
`NEURON_PLATFORM_TARGET_OVERRIDE` environment variable. Supported values:

- `trn1|inf2|gen2`
- `trn2|gen3`
- `trn3|gen4`

The LNC (Logical NeuronCore) degree can be set at the callsite using bracket syntax:
`kernel[lnc](args)`. The default is LNC=1. The LNC value must match the
`NEURON_LOGICAL_NC_CONFIG` environment variable set for the Neuron Runtime.
Mismatching the two will cause a runtime error. For example, if
`NEURON_LOGICAL_NC_CONFIG=1`, the kernel must be launched with `kernel[1](...)`
or `kernel(...)`.

Per-kernel config is passed through two typed objects:

- `compile_config` -- a `nki.compiler.CompileConfig` of options that
  impact downstream compiler behavior (`address_rotation`,
  `non_ssa_legalization`, `dead_code_elim`, `barrier_check`).
- `specialization_config` -- a `nki.compiler.SpecializationConfig` of
  options that tune the specialization phase (`allow_nested_config_override`).

e.g.::

    from nki.compiler import CompileConfig, SpecializationConfig

    @nki.jit(compile_config=CompileConfig(non_ssa_legalization=True))
    def my_kernel(...): ...

LNC is set at the callsite with bracket syntax (`kernel[2](...)`), not here.

`SpecializationConfig.allow_nested_config_override` -- when this kernel is used as the
top-level compiled kernel:

* default (`False`): a nested sub-kernel whose config disagrees raises an error.
* `True`: warn instead, and inline the sub-kernel under this kernel's config.

Useful for composing @nki.jit kernels whose config you cannot edit.

Returns a `Kernel` instance wrapping the decorated function.

---

