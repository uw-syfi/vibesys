# About NKI Versions

About NKI Versions
This page provides details on the versions of the Neuron Kernel Interface (NKI) and its ongoing evolution.

## NKI 0.4.0 (Latest, Neuron SDK 2.30.0)

NKI 0.4.0 is the current latest version. It adds new trn3 instructions (`nisa.activate2`, `nl.abs_max`/`nl.abs_min`, `square`/`relu` opcodes), bytes-aware `tile_size` constants (`tile_size.sbuf_fmax_bytes`, `tile_size.sbuf_size_bytes`, `tile_size.sbuf_fmax`, `tile_size.psum_fmax_bytes`), `dma_compute` `oob_mode`, and OCP FP8 (`float8_e4m3fn`) matmul support. Breaking changes: `dma_transpose` enforces rank matching, `neuronxcc.nki` namespace is now a compilation error, and `tensor_copy_dynamic_src/dst` are fully removed.

## NKI 0.3.0 (GA, Neuron SDK 2.29.0)

NKI reached General Availability. Ships with NKI Standard Library (nki-stdlib), CPU Simulator, new `nki.language` convenience APIs, and API improvements for correctness and consistency.

## NKI 0.3.0 (GA) Features

NKI 0.3.0 introduces several major features:

- **NKI Standard Library (nki-stdlib)**: An open-source library providing developer-visible code for all NKI APIs and native language objects (e.g., `NkiTensor`).
- **NKI CPU Simulator**: `nki.simulate(kernel)` executes NKI kernels entirely on CPU without requiring NeuronDevice hardware, enabling local development, debugging, and functional correctness testing on any machine.
- **nki.typing Module**: Type-annotating kernel tensor parameters with `nt.tensor[shape]`.
- **nki.language APIs**: Convenience wrappers around `nki.isa` APIs including `nl.load`, `nl.store`, `nl.copy`, `nl.matmul`, `nl.transpose`, `nl.softmax`, and other high-level operations (experimental).
- **New nki.isa APIs**: `nki.isa.exponential` (Trn3 only) for dedicated exponential instruction.
- **New nki.collectives APIs**: `nki.collectives.all_to_all_v` for variable-length all-to-all collective.
- **Matmul Accumulation**: `nc_matmul` and `nc_matmul_mx` now have an `accumulate` parameter.
- **Address Placement**: `address` parameter added to `nki.language.ndarray` for explicit memory placement.

To learn more about the features in NKI 0.3.0, see the overview documentation here: [About NKI](../programming/api/index.md).

To use NKI, import the `nki.*` namespace in your code and annotate your top-level kernel function with `&#64;nki.jit`.

## NKI 0.2.0 (Beta 2) Features

NKI 0.2.0 introduced a large number of changes to the NKI language, constraining it to the minimum set required to build high performance kernels. It also introduced a new compiler front end with an LL(k) parser that provides parsing and semantic errors up front.

## NKI Beta 1 (Deprecated and Removed)

NKI Beta 1 (`neuronxcc.nki.*` namespace) is no longer supported. NKI 0.3.0 does not include support for the Beta 1 language and APIs. If you have Beta 1 kernels, you must first migrate to NKI 0.2.0 (see the Beta 1 to Beta 2 migration guide), then follow the [NKI 0.3.0 Update Guide](../reference/migration/nki-030-update-guide.md) to update to the current version.

## NKI Support Information

For support with NKI, file a [GitHub issue](https://github.com/aws-neuron/aws-neuron-sdk/issues) and provide us the details of your experience or issue. Other contact details can be found here: [Contact us](../programming/api/index.md#contact-us).