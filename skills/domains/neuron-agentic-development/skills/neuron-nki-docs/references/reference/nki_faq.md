# NKI FAQ

NKI FAQ

## When should I use NKI?

NKI enables customers to self serve, onboard novel deep learning
architectures, and implement operators currently unsupported by
traditional ML Framework operators. With NKI, customers can experiment
with models and operators and can create unique differentiation.
Additionally, in cases where the compiler’s optimizations are too
generalized for a developers’ particular use case, NKI enables customers
to program directly against the Neuron primitives and therefore optimize
performance of existing operators that are not being compiled
efficiently.

## Which AWS chips does NKI support?

NKI supports all families of chips included in AWS custom-built machine
learning accelerators, Trainium and Inferentia. This includes the second generation chips and beyond,
available in the following instance types: Inf2, Trn1, Trn1n and Trn2.

## Which compute engines are supported?

The following AWS Trainium and Inferentia compute engines are
supported: Tensor Engine, Vector Engine, Scalar Engine, and GpSimd Engine.
For more details, see the NeuronDevice Architecture Guide,
and refer to [nki.isa](../programming/api/nki.isa.md) APIs to identify which engines are utilized for each instruction.

## How do I launch a NKI kernel onto a logical NeuronCore with Trainium2 from NKI?

A logical NeuronCore (LNC) can consist of multiple physical NeuronCores. In the current Neuron release, an LNC on Trainium2 can have up to two physical NeuronCores (subject to future changes).

For more details on NeuronCore configurations, see
[Logical NeuronCore configurations](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/about-neuron/arch/neuron-features/logical-neuroncore-config.html#logical-neuroncore-config).

In NKI, users can launch a NKI kernel onto multiple physical NeuronCores within a logical NeuronCore using single program, multiple data (SPMD) grids.

For a step-by-step guide, refer to the tutorial here:
SPMD Tensor addition with multiple NeuronCores.

## What ML Frameworks support NKI kernels?

NKI is integrated with [PyTorch](../programming/framework_custom_op.md#nki-framework-custom-op-pytorch) and [JAX](../programming/framework_custom_op.md#nki-framework-custom-op-jax)
frameworks. For more details, see the [NKI Kernel as a Framework Custom Operator](../programming/framework_custom_op.md#nki-framework-custom-op).

## What Neuron software does not currently support NKI?

NKI does not currently support integration with
Neuron Custom C++ Operators, Transformers NeuronX, and Neuron Collective Communication.

## Where can I find NKI sample kernels?

NKI hosts an open source sample repository
[nki-samples](https://github.com/aws-neuron/nki-samples) which
includes a set of reference kernels and tutorial kernels built by the
Neuron team and external contributors. For more information, see nki_kernels and NKI tutorials.

## What should I do if I have trouble resolving a kernel compilation error?

Refer to NKI Error Manual for a detailed guidance on how
to resolve some of the common NKI compilation errors.

If you encounter compilation errors from Neuron Compiler that you cannot understand or
resolve, you may check out NKI sample [GitHub issues](https://github.com/aws-neuron/nki-samples/issues)
and open an issue if no similar issues exist.

## How can I debug numerical issues in NKI kernels?

We encourage NKI programmers to build kernels incrementally and verify output of small operators one at a time.
NKI also provides a CPU simulation mode that supports printing of kernel intermediate tensor values to the console.
See nki.simulate for a code example.

## How can I optimize my NKI kernel?

To learn how to optimize your NKI kernel, see the [NKI Performance Optimizations](../optimization/nki_perf_guide.md#nki-perf-guide).

## Does NKI support entire Neuron instruction set?

Neuron will iteratively add support for the Neuron
instruction set through adding more [nki.isa](../programming/api/nki.isa.md) (Instruction Set
Architecture) APIs in upcoming Neuron releases.

## Will NKI APIs guarantee backwards compatibility?

The [NKI APIs](../programming/api/index.md) follow the Neuron Software Maintenance policy for Neuron APIs.
For more information, see the
[SDK Maintenance Policy](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/about-neuron/sdk-policy.html).