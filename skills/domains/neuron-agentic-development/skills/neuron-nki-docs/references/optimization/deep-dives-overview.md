# NKI Deep Dives

NKI Deep Dives
This section provides in-depth technical documentation and guides for advanced users of the Neuron Kernel Interface (NKI). These deep dives offer detailed explanations of NKI concepts, programming patterns, and best practices to help you maximize the performance and capabilities of your NKI code on AWS Neuron devices.

## Optimizing a NKI Kernel

[Profiling a NKI Kernel with Neuron Explorer](use-neuron-profile.md)

[NKI Performance Optimizations](nki_perf_guide.md#nki-perf-guide)

## Advanced NKI Programming

[MXFP4/8 Matrix Multiplication Guide](mxfp-matmul.md)
Perform matrix multiplication using MXFP8 data types in NKI kernels, including data layout, quantization, and tiling strategies.

[NKI Compiler](../programming/nki-compiler.md#nki-compiler-about)
Learn about the NKI Compiler.

[NKI Access Patterns](../programming/nki-aps.md#nki-aps)
Learn about Access Patterns (AP) to directly specify how the Trainium hardware accesses tensors.

[NKI Block Dimension Migration Guide](../reference/migration/nki_block_dimension_migration_guide.md)
Migrate NKI kernels to use block dimensions for improved performance and resource utilization on Trainium devices.

## Additional NKI Information

[NKI Beta Versions](nki-beta-versions.md)

[NKI Beta Migration Guide](../reference/migration/nki-migration-guide.md)