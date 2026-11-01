# Neuron Kernel Interface (NKI) release notes

Neuron Kernel Interface (NKI) release notes

## Neuron Kernel Interface (NKI) (GA) [2.29]

Date: 2026

NKI 0.3.0 moves NKI to General Availability with a new open-source NKI Standard Library (nki-stdlib), a built-in CPU Simulator, `nki.language` APIs, and several API improvements for correctness and consistency.

* new features:

NKI Standard Library (nki-stdlib) — open-source, developer-visible code for all NKI APIs and native language objects

* NKI CPU Simulator — `nki.simulate(kernel)` executes NKI kernels on CPU without NeuronDevice hardware (experimental)

* `nki.typing` module — type-annotate kernel tensor parameters with `nt.tensor[shape]`

* `nki.language` convenience APIs (experimental) — `nl.load`, `nl.store`, `nl.copy`, `nl.matmul`, `nl.transpose`, `nl.softmax`

* new `nki.isa` APIs:

`nki.isa.exponential` — dedicated exponential instruction (Trn3/NeuronCore-v4 only)

* new `nki.collectives` APIs:

`nki.collectives.all_to_all_v` — variable-length all-to-all collective

* changes to existing APIs:

`nki.isa.nc_matmul` and `nki.isa.nc_matmul_mx` — new `accumulate` parameter for controlling overwrite vs accumulation on PSUM

* `nki.language.ndarray` — new `address` parameter for explicit memory placement

* `nki.isa.dma_copy` — no longer supports reading directly from PSUM; `dst_rmw_op` and `unique_indices` parameters removed (use `nisa.dma_compute` instead); enforces type matching with `dge_mode=hwdge`

* `nki.isa.dma_compute` — `scales` and `reduce_op` parameter positions swapped; `unique_indices` parameter added

* `nki.isa.memset` — `value` must match destination dtype; x4 packed types enforce `value=0`

* `nki.isa.tensor_reduce` — fixed incorrect axis handling for 3D/4D tensors

* `nki.isa.sendrecv` — `use_gpsimd_dma` replaced by `dma_engine` enum

* `nki.isa.affine_select` — `offset` parameter moved to keyword argument

* `nki.isa.register_move` — `imm` parameter renamed to `src`, now accepts `VirtualRegister`

* `nki.jit` — `platform_target` parameter removed (use `NEURON_PLATFORM_TARGET_OVERRIDE` env var); `mode` parameter deprecated and ignored

* Output tensors must use `buffer=nl.shared_hbm`

* Integer enum constants no longer supported (use named enum members)

* String buffer names no longer supported (use buffer objects like `nl.sbuf`, `nl.psum`)

* `nki.isa.tensor_copy_dynamic_src` / `nki.isa.tensor_copy_dynamic_dst` deprecated (use `nisa.tensor_copy()` with `.ap()` and `scalar_offset`)

* default value changes:

`nki.isa.iota` — `offset` now optional with default `0`

* `nki.isa.core_barrier` — `engine` default changed from `unknown` to `gpsimd`

* `nki.language.num_programs` — `axes` default changed from `None` to `0`

* `nki.language.program_id` — `axis` now has default value of `0`

* `nki.language.ndarray` — `buffer` default changed from `None` to `nl.sbuf`

* `nki.language.zeros` — `buffer` default changed from `None` to `nl.sbuf`

* `nki.language.sequential_range` — `stop` and `step` now have default values (`None` and `1`)

* language restrictions:

Keyword-only argument separator (`*`) not supported in kernel function signatures

* `is` / `is not` operators not supported; use `==` / `!=` instead

* `list` not supported as kernel argument type; use tuples instead

* Collectives — `num_channels` removed from `collective_permute_implicit_current_processing_rank_id`

## Neuron Kernel Interface (NKI) (Beta) [2.27]

Date: 12/25/2025

* new `nki.language` APIs:

`nki.language.device_print`

* new `nki.isa` APIs:

`nki.isa.dma_compute`

* `nki.isa.nki.isa.quantize_mx`

* `nki.isa.nki.isa.nc_matmul`

* `nki.isa.nki.isa.nc_n_gather` [used to be `nl.gather_flattened` with free partition limited to 512]

* `nki.isa.rand2`

* `nki.isa.rand_set_state`

* `nki.isa.rand_get_state`

* `nki.isa.set_rng_seed`

* `nki.isa.rng`

* new `dtypes`:

`nki.language.float8_e5m2_x4`

* `nki.language.float4_e2m1fn_x4`

* `nki.language.float8_e4m3fn_x4`

* changes to existing APIs:

several `nki.language` APIs have been removed in NKI Beta 2

* all nki.isa APIs have `dst` as an input param

* all nki.isa APIs removed `dtype` and `mask` support

* `nki.isa.memset` — removed `shape` positional arg , since we have `dst`

* `nki.isa.affine_select` — instead of `pred`, we now take `pattern` and `cmp_op` params

* `nki.isa.iota` — `expr` replaced with `pattern` and `offset`

* `nki.isa.nc_stream_shuffle` - `src` and `dst` order changed

* docs improvements:

restructured NKI Documentation to align with workflows

* added [Trainium3 Architecture Guide for NKI](../architecture/trainium3_arch.md)

* added [About Neuron Kernel Interface (NKI)](../programming/api/index.md)

* added [NKI Environment Setup Guide](../programming/setup-env.md)

* added [Get Started with NKI](../programming/quickstart-implement-run-kernel.md)

* added [NKI Language Guide](../programming/nki-language-guide.md)

* added [About the NKI Compiler](../programming/nki-compiler.md)

* added [About NKI Beta Versions](../optimization/nki-beta-versions.md)

* added [MXFP Matrix Multiplication with NKI](../optimization/mxfp-matmul.md)

* updated [Matrix Multiplication Tutorial](../programming/tutorials/matrix_multiplication.md)

* updated [Profile a NKI Kernel](../optimization/use-neuron-profile.md)

* updated [NKI APIs](../programming/api/index.md)

* updated [NKI Library docs](../programming/api/index.md)

* removed NKI Error Guide

* known issues:

`nki.isa.nki.isa.nc_matmul` - `is_moving_onezero` was incorrectly named `is_moving_zero` in this release

* NKI ISA semantic checks are not available with Beta 2, workaround is to reference the API docs

* NKI Collectives are not available with Beta 2

* `nki.benchmark` and `nki.profile` are not available with Beta 2

## Neuron Kernel Interface (NKI) (Beta) [2.26]

Date: 09/18/2025

* new `nki.language` APIs:

`nki.language.gelu_apprx_sigmoid` - Gaussian Error Linear Unit activation function with sigmoid approximation.

* `nki.language.tile_size.total_available_sbuf_size` to get total available SBUF size

* new `nki.isa` APIs:

`nki.isa.select_reduce` - selectively copy elements with max reduction

* `nki.isa.sequence_bounds` - compute sequence bounds of segment IDs

* `nki.isa.dma_transpose`

`axes` param to define 4D transpose for some supported cases

* `dge_mode` to specify Descriptor Generation Engine (DGE).

* `nl.gelu_apprx_sigmoid` op support on `nki.isa.activation`

* fixes / improvements:

`nki.language.store` supports PSUM buffer with extra additional copy inserted.

* docs/tutorial improvements:

`nki.isa.dma_transpose` API doc and example

* `nki.simulate_kernel` example improvement

* use `nl.fp32.min` in tutorial code instead of a magic number

* better error reporting:

indirect indexing on transpose

* mask expressions

## Neuron Kernel Interface (NKI) (Beta) [2.24]

Date: 06/24/2025

* `sqrt` valid data range extended for accuracy improvement with wider numerical values support.

* `nki.language.gather_flattened` new API

* `nki.isa.nc_match_replace8` additional param `dst_idx`

* improved docs/examples on `nki.isa.nc_match_replace8`, `nki.isa.nc_stream_shuffle`

* improved error messages

## Neuron Kernel Interface (NKI) (Beta) [2.23]

Date: 05/20/2025

* `nki.isa.range_select` (for trn2) new instruction

* `abs`, `power` ops supported on to nki.isa tensor instruction

* `abs` op supported on `nki.isa.activation` instruction

* GpSIMD engine support added to `add`, `multiply` in 32bit integer to nki.isa tensor operations

* `nki.isa.tensor_copy_predicated` support for reversing predicate.

* `nki.isa.tensor_copy_dynamic_src`, `tensor_copy_dynamic_dst` engine selection.

* `nki.isa.dma_copy` additional support with `dge_mode`, `oob_mode`, and in-place add `rmw_op`.

* `+=, -=, /=, *=` operators now work consistently across loop types, PSUM, and SBUF,

* fixed simulation for instructions: `nki.language.rand`, `random_seed`, `nki.isa.dropout`

* fixed simulation masking behavior

* Added warning when the block dimension is used for SBUF and PSUM tensors, see: [NKI Block Dimension Migration Guide](migration/nki_block_dimension_migration_guide.md#nki-block-dimension-migration-guide)

## Neuron Kernel Interface (NKI) (Beta) [2.22]

Date: 04/03/2025

* New modules and APIs:

`nki.profile`

* `nki.isa` new APIs:

`tensor_copy_dynamic_dst`

* `tensor_copy_predicated`

* `max8`, `nc_find_index8`, `nc_match_replace8`

* `nc_stream_shuffle`

* `nki.language` new APIs: `mod`, `fmod`, `reciprocal`, `broadcast_to`, `empty_like`

* Improvements:

`nki.isa.nc_matmul` now supports PE tiling feature

* `nki.isa.activation` updated to support reduce operation and `reduce` commands

* `nki.isa.engine` enum

* `engine` parameter added to more `nki.isa` APIs that support engine selection (ie, `tensor_scalar`, `tensor_tensor`, `memset`)

* Documentation for `nki.kernels` have been moved to the GitHub: [https://aws-neuron.github.io/nki-samples](https://aws-neuron.github.io/nki-samples).
The source code can be viewed at [aws-neuron/nki-samples](https://github.com/aws-neuron/nki-samples).

These kernels are still shipped as part of Neuron package in `neuronxcc.nki.kernels` module

* Documentation updates:

Kernels public repository [https://aws-neuron.github.io/nki-samples](https://aws-neuron.github.io/nki-samples)

* Updated [profiling guide](../optimization/use-neuron-profile.md) to use `nki.profile` instead of `nki.benchmark`

* NKI ISA Activation functions table now have [valid input data ranges](../programming/api/nki.api.shared.md#tbl-act-func) listed

* NKI ISA Supported Math operators now have [supported engine](../programming/api/nki.api.shared.md#tbl-aluop) listed

* Clarify `+=` syntax support/limitation

## Neuron Kernel Interface (NKI) (Beta) [2.21]

Date: 12/16/2024

* New modules and APIs:

`nki.compiler` module with Allocation Control and Kernel decorators,
see guide for more info.

* `nki.isa`: new APIs (`activation_reduce`, `tensor_partition_reduce`,
`scalar_tensor_tensor`, `tensor_scalar_reduce`, `tensor_copy`,
`tensor_copy_dynamic_src`, `dma_copy`), new activation functions(`identity`,
`silu`, `silu_dx`), and target query APIs (`nc_version`, `get_nc_version`).

* `nki.language`: new APIs (`shared_identity_matrix`, `tan`,
`silu`, `silu_dx`, `left_shift`, `right_shift`, `ds`, `spmd_dim`, `nc`).

* New `datatype <nl_datatypes>`: `float8_e5m2`

* New `kernels` (`allocated_fused_self_attn_for_SD_small_head_size`,
`allocated_fused_rms_norm_qkv`) added, kernels moved to public repository.

* Improvements:

Semantic analysis checks for nki.isa APIs to validate supported ops, dtypes, and tile shapes.

* Standardized naming conventions with keyword arguments for common optional parameters.

* Transition from function calls to kernel decorators (`jit`,
`benchmark`, `baremetal`, `simulate_kernel`).

* Documentation updates:

Tutorial for [SPMD usage with multiple Neuron Cores on Trn2](../programming/tutorials/spmd_multiple_nc_tensor_addition.md)

## Neuron Kernel Interface (NKI) (Beta)

Date: 12/03/2024

* NKI support for Trainium2, including full integration with Neuron Compiler.
Users can directly shard NKI kernels across multiple Neuron Cores from an SPMD launch grid.
See [tutorial](../programming/tutorials/spmd_multiple_nc_tensor_addition.md) for more info.
See [Trainium2 Architecture Guide](../architecture/trainium2_arch.md) for an initial version of the architecture specification
(more details to come in future releases).

* New calling convention in NKI kernels, where kernel output tensors are explicitly returned from the kernel instead
of pass-by-reference. See any [NKI tutorial](../programming/api/index.md) for code examples.

## Neuron Kernel Interface (NKI) (Beta) [2.20]

Date: 09/16/2024

* This release includes the beta launch of the Neuron Kernel Interface (NKI) (Beta).
NKI is a programming interface enabling developers to build optimized compute kernels
on top of Trainium and Inferentia. NKI empowers developers to enhance deep learning models
with new capabilities, performance optimizations, and scientific innovation.
It natively integrates with PyTorch and JAX, providing a Python-based programming environment
with Triton-like syntax and tile-level semantics offering a familiar programming experience
for developers. Additionally, to enable bare-metal access precisely programming the instructions
used by the chip, this release includes a set of NKI APIs (`nki.isa`) that directly emit
Neuron Instruction Set Architecture (ISA) instructions in NKI kernels.