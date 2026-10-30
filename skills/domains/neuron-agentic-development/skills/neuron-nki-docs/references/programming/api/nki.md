# nki

The `nki` module provides the top-level entry points for compiling and running NKI kernels.
Use the `jit` decorator to compile a kernel for NeuronDevices, or `simulate` to
run a kernel in the CPU simulator for debugging.

<a id="nki_decorators"></a>

| Symbol | Summary |
| --- | --- |
| [jit](api-nki-tools.md#nki-jit) | Just-in-time compile a top-level NKI function to run on NeuronDevices. |
