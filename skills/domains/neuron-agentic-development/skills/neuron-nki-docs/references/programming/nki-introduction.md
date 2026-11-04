# Neuron Kernel Interface (NKI) Documentation

Neuron Kernel Interface (NKI) Documentation

> **Note**
>
> NKI Versions
> 
> 
> NKI is now Generally Available (GA) with NKI 0.3.0 as the current version. Read more about [NKI versions](../optimization/nki-beta-versions.md).

The Neuron Kernel Interface (NKI) is a Python-embedded Domain Specific Language (DSL) that gives developers direct access to Neuron’s Instruction Set Architecture (NISA). NKI provides the ease-of-programming offered by tile-level operations and full access to the Neuron Instruct Set Architecture within a familiar pythonic programming environment. It provides the flexibility to implement architecture-specific optimizations rapidly, at a speed difficult to achieve in higher-level DSLs and frameworks. This has enabled developers to achieve optimal performance across a wide spectrum of machine learning models on Trainium, including Transformers, Mixture-of-Experts, State Space Models, and more.

In addition to directly exposing NISA, NKI provides easy-to-use APIs for controlling instruction scheduling, memory management across the memory hierarchy, software pipelining, and other optimization techniques. The APIs are carefully designed to help simplify the code while providing more control and flexibility to developers. This gives developers fine-grained tuning optimizations that work in concert with the capabilities provided by the compiler.

NKI currently supports multiple NeuronDevice generations:

* Trainium/Inferentia2, available on AWS `trn1`, `trn1n` and `inf2` instances

* Trainium2, available on AWS `trn2` instances and UltraServers

* Trainium3, available on AWS `trn3` instances and UltraServers

Explore the comprehensive guides below to learn how to implement and optimize your kernels for AWS Neuron accelerators:

[About NKI](api/index.md#nki-about-home)
Learn about Neuron Kernel Interface (NKI) and core concepts essential for working with it.

[NKI Language Guide](nki-language-guide.md#nki-language-guide)
Developer guide for NKI’s Pythonic language syntax.

[NKI Compiler Documentation](nki-compiler.md)
Documentation for the NKI compiler and its integration with the Neuron Compiler.

[NKI Library Documentation](api/index.md#nkl-home)
API documentation for the set of pre-built kernels in the NKI Library.

## Writing NKI Kernels

[Getting Started with NKI](quickstart-implement-run-kernel.md#quickstart-run-nki-kernel)

[NKI Tutorials](api/index.md)

## Optimizing NKI Kernels

[Profiling a NKI Kernel with Neuron Explorer](../optimization/use-neuron-profile.md)

[NKI Performance Optimizations](../optimization/nki_perf_guide.md#nki-perf-guide)