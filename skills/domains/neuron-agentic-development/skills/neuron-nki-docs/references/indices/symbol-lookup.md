# Symbol Lookup

Alphabetical index of all public NKI API symbols.

| Symbol | Module | Category | Summary | File |
|--------|--------|----------|---------|------|
| `abs` | language | Operators (op specifiers) | Op specifier for abs. | [→](../programming/api/api-nki-language-operators.md#nki-language-abs) |
| `abs_max` | language | Operators (op specifiers) | Op specifier for abs_max. | [→](../programming/api/api-nki-language-operators.md#nki-language-abs_max) |
| `abs_min` | language | Operators (op specifiers) | Op specifier for abs_min. | [→](../programming/api/api-nki-language-operators.md#nki-language-abs_min) |
| `activate2` | isa | Scalar & Activation | Perform tensor activation with configurable tensor-scalar operations and optiona | [→](../programming/api/api-nki-isa-scalar.md#nki-isa-activate2) |
| `activation` | isa | Scalar & Activation | Apply an activation function on every element of the input tile using Scalar Eng | [→](../programming/api/api-nki-isa-scalar.md#nki-isa-activation) |
| `activation_reduce` | isa | Scalar & Activation | Perform the same computation as `nisa.activation` and also a reduction along the | [→](../programming/api/api-nki-isa-scalar.md#nki-isa-activation_reduce) |
| `add` | language | Operators (op specifiers) | Op specifier for add. | [→](../programming/api/api-nki-language-operators.md#nki-language-add) |
| `affine_range` | language | Dimensions & Indexing | Create a sequence for fully unrolled loop iteration. | [→](../programming/api/api-nki-language-dims.md#nki-language-affine_range) |
| `affine_select` | isa | Utility & Gather | Select elements between an input tile `on_true_tile` and a scalar value `on_fals | [→](../programming/api/api-nki-isa-utility.md#nki-isa-affine_select) |
| `all` | language | Miscellaneous | Whether all elements along the specified axis (or axes) evaluate to True. | [→](../programming/api/api-nki-language-misc.md#nki-language-all) |
| `all_gather` | collectives | Collective Communication | Perform an all-gather on the given replica group and input/output tensors. | [→](../programming/api/api-nki-collectives.md#nki-collectives-all_gather) |
| `all_gather_v` | collectives | Collective Communication | Perform a variable-length all-gather on the given replica group. | [→](../programming/api/api-nki-collectives.md#nki-collectives-all_gather_v) |
| `all_gather_v_prepare` | collectives | Collective Communication | Begin a split `all_gather_v`. | [→](../programming/api/api-nki-collectives.md#nki-collectives-all_gather_v_prepare) |
| `all_gather_v_trigger` | collectives | Collective Communication | Complete a split `all_gather_v`. | [→](../programming/api/api-nki-collectives.md#nki-collectives-all_gather_v_trigger) |
| `all_reduce` | collectives | Collective Communication | Perform an all-reduce on the given replica group and input/output tensors. | [→](../programming/api/api-nki-collectives.md#nki-collectives-all_reduce) |
| `all_to_all` | collectives | Collective Communication | Perform an all-to-all on the given replica group and input/output tensors. | [→](../programming/api/api-nki-collectives.md#nki-collectives-all_to_all) |
| `all_to_all_v` | collectives | Collective Communication | Executes an all-to-all collective where each rank can send a different number of | [→](../programming/api/api-nki-collectives.md#nki-collectives-all_to_all_v) |
| `all_to_all_v_prepare` | collectives | Collective Communication | Begin a split `all_to_all_v`. | [→](../programming/api/api-nki-collectives.md#nki-collectives-all_to_all_v_prepare) |
| `all_to_all_v_trigger` | collectives | Collective Communication | Complete a split `all_to_all_v`. | [→](../programming/api/api-nki-collectives.md#nki-collectives-all_to_all_v_trigger) |
| `alloc_blocks` | language | Tiled & Blocked Layouts | Allocate new storage organized as a grid of tile blocks. | [→](../programming/api/api-nki-language-layout.md#nki-language-alloc_blocks) |
| `alloc_tiles` | language | Tiled & Blocked Layouts | Allocate independent, uninitialized storage organized as a grid of tiles. | [→](../programming/api/api-nki-language-layout.md#nki-language-alloc_tiles) |
| `ap` | tensor | Tensor Views (NkiTensor methods) | Low-level access pattern override (escape hatch). | [→](../programming/api/api-nki-tensor.md#nki-tensor-ap) |
| `arctan` | language | Operators (op specifiers) | Op specifier for arctan. | [→](../programming/api/api-nki-language-operators.md#nki-language-arctan) |
| `bfloat16` | language | Data Types | 16-bit floating-point number (1S,8E,7M). | [→](../programming/api/api-nki-language-types.md#nki-language-bfloat16) |
| `bitwise_and` | language | Operators (op specifiers) | Op specifier for bitwise_and. | [→](../programming/api/api-nki-language-operators.md#nki-language-bitwise_and) |
| `bitwise_or` | language | Operators (op specifiers) | Op specifier for bitwise_or. | [→](../programming/api/api-nki-language-operators.md#nki-language-bitwise_or) |
| `bitwise_xor` | language | Operators (op specifiers) | Op specifier for bitwise_xor. | [→](../programming/api/api-nki-language-operators.md#nki-language-bitwise_xor) |
| `blocks` | language | Tiled & Blocked Layouts | Create a block view over a tensor where each block contains multiple tiles. | [→](../programming/api/api-nki-language-layout.md#nki-language-blocks) |
| `bn_aggr` | isa | Vector Engine | Aggregate one or multiple `bn_stats` outputs to generate a mean and variance per | [→](../programming/api/api-nki-isa-vector.md#nki-isa-bn_aggr) |
| `bn_stats` | isa | Vector Engine | Compute mean- and variance-related statistics for each partition of an input til | [→](../programming/api/api-nki-isa-vector.md#nki-isa-bn_stats) |
| `bool_` | language | Data Types | Boolean (True or False) stored as a byte. | [→](../programming/api/api-nki-language-types.md#nki-language-bool_) |
| `broadcast` | tensor | Tensor Views (NkiTensor methods) | Expand a size-1 dimension to `size` by repeating elements. | [→](../programming/api/api-nki-tensor.md#nki-tensor-broadcast) |
| `broadcast_to` | language | Miscellaneous | Broadcast a tile to a new shape following numpy broadcasting rules. | [→](../programming/api/api-nki-language-misc.md#nki-language-broadcast_to) |
| `bypass` | language | Operators (op specifiers) | Op specifier for bypass. | [→](../programming/api/api-nki-language-operators.md#nki-language-bypass) |
| `ceil` | language | Operators (op specifiers) | Op specifier for ceil. | [→](../programming/api/api-nki-language-operators.md#nki-language-ceil) |
| `collective_permute` | collectives | Collective Communication | Send and receive data between ranks based on explicitly defined source-target pa | [→](../programming/api/api-nki-collectives.md#nki-collectives-collective_permute) |
| `collective_permute_implicit` | collectives | Collective Communication | Send and receive data between ranks in a ring, where sources and destinations ar | [→](../programming/api/api-nki-collectives.md#nki-collectives-collective_permute_implicit) |
| `collective_permute_implicit_current_processing_rank_id` | collectives | Collective Communication | Returns the rank ID of the data to be processed in the current ring iteration. | [→](../programming/api/api-nki-collectives.md#nki-collectives-collective_permute_implicit_current_processing_rank_id) |
| `collective_permute_implicit_reduce` | collectives | Collective Communication | Perform an implicit collective permute with reduction in a ring, where sources a | [→](../programming/api/api-nki-collectives.md#nki-collectives-collective_permute_implicit_reduce) |
| `copy` | language | Operators (op specifiers) | Op specifier for copy. | [→](../programming/api/api-nki-language-operators.md#nki-language-copy) |
| `core_barrier` | isa | Logical NeuronCore (LNC) | Synchronize execution across multiple NeuronCores by implementing a barrier mech | [→](../programming/api/api-nki-isa-local-collective.md#nki-isa-core_barrier) |
| `cos` | language | Operators (op specifiers) | Op specifier for cos. | [→](../programming/api/api-nki-language-operators.md#nki-language-cos) |
| `device_print` | language | Tensor Creation | Print a message with a string prefix followed by the value of a tile. | [→](../programming/api/api-nki-language-creation.md#nki-language-device_print) |
| `dge_mode` | isa | Miscellaneous | Descriptor Generation Engine mode. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-dge_mode) |
| `divide` | language | Operators (op specifiers) | Op specifier for divide. | [→](../programming/api/api-nki-language-operators.md#nki-language-divide) |
| `dma_compute` | isa | Miscellaneous | Perform math operations using compute logic inside DMA engines with element-wise | [→](../programming/api/api-nki-isa-misc.md#nki-isa-dma_compute) |
| `dma_copy` | isa | Data Movement & Memory | Copy data from `src` to `dst` using DMA engines. | [→](../programming/api/api-nki-isa-memory.md#nki-isa-dma_copy) |
| `dma_engine` | isa | Miscellaneous | DMA transfer engine. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-dma_engine) |
| `dma_transpose` | isa | Data Movement & Memory | Perform a transpose on input `src` using DMA Engine. | [→](../programming/api/api-nki-isa-memory.md#nki-isa-dma_transpose) |
| `dropout` | isa | Miscellaneous | Randomly replace some elements of the input tile `data` with zeros based on inpu | [→](../programming/api/api-nki-isa-misc.md#nki-isa-dropout) |
| `dropout` | language | Miscellaneous | Randomly zeroes some of the elements of the input tile given a probability rate. | [→](../programming/api/api-nki-language-misc.md#nki-language-dropout) |
| `ds` | language | Dimensions & Indexing | A `size`-element window starting at `start`. | [→](../programming/api/api-nki-language-dims.md#nki-language-ds) |
| `dynamic_range` | language | Dimensions & Indexing | Create a sequence for **dynamic** loop iteration. | [→](../programming/api/api-nki-language-dims.md#nki-language-dynamic_range) |
| `empty_like` | language | Tiled & Blocked Layouts | Allocate independent, uninitialized storage with matching geometry. | [→](../programming/api/api-nki-language-layout.md#nki-language-empty_like) |
| `engine` | isa | Miscellaneous | Neuron Device engines. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-engine) |
| `equal` | language | Operators (op specifiers) | Op specifier for equal. | [→](../programming/api/api-nki-language-operators.md#nki-language-equal) |
| `erf` | language | Operators (op specifiers) | Op specifier for erf. | [→](../programming/api/api-nki-language-operators.md#nki-language-erf) |
| `erf_dx` | language | Operators (op specifiers) | Op specifier for erf_dx. | [→](../programming/api/api-nki-language-operators.md#nki-language-erf_dx) |
| `exp` | language | Operators (op specifiers) | Op specifier for exp. | [→](../programming/api/api-nki-language-operators.md#nki-language-exp) |
| `expand_dim` | tensor | Tensor Views (NkiTensor methods) | Insert a new dimension of size 1 at position `dim`. | [→](../programming/api/api-nki-tensor.md#nki-tensor-expand_dim) |
| `expand_dims` | language | Miscellaneous | Expand the shape of a tile. | [→](../programming/api/api-nki-language-misc.md#nki-language-expand_dims) |
| `exponential` | isa | Miscellaneous | Apply exponential function to each element after subtracting a max_value using V | [→](../programming/api/api-nki-isa-misc.md#nki-isa-exponential) |
| `flatten_dims` | tensor | Tensor Views (NkiTensor methods) | Merge a contiguous range of dimensions into one. | [→](../programming/api/api-nki-tensor.md#nki-tensor-flatten_dims) |
| `float16` | language | Data Types | 16-bit floating-point number. | [→](../programming/api/api-nki-language-types.md#nki-language-float16) |
| `float32` | language | Data Types | 32-bit floating-point number. | [→](../programming/api/api-nki-language-types.md#nki-language-float32) |
| `float4_e2m1fn_x4` | language | Data Types | 4x packed float4_e2m1fn elements, custom data type for nki.isa.nc_matmul_mx on N | [→](../programming/api/api-nki-language-types.md#nki-language-float4_e2m1fn_x4) |
| `float8_e4m3` | language | Data Types | 8-bit floating-point number (1S,4E,3M). | [→](../programming/api/api-nki-language-types.md#nki-language-float8_e4m3) |
| `float8_e4m3fn` | language | Data Types | 8-bit floating-point number (1S,4E,3M), Extended range: no inf, NaN represented  | [→](../programming/api/api-nki-language-types.md#nki-language-float8_e4m3fn) |
| `float8_e4m3fn_x4` | language | Data Types | 4x packed float8_e4m3fn elements, custom data type for nki.isa.nc_matmul_mx on N | [→](../programming/api/api-nki-language-types.md#nki-language-float8_e4m3fn_x4) |
| `float8_e5m2` | language | Data Types | 8-bit floating-point number (1S,5E,2M). | [→](../programming/api/api-nki-language-types.md#nki-language-float8_e5m2) |
| `float8_e5m2_x4` | language | Data Types | 4x packed float8_e5m2 elements, custom data type for nki.isa.nc_matmul_mx on Neu | [→](../programming/api/api-nki-language-types.md#nki-language-float8_e5m2_x4) |
| `float8_e8m0fnu` | language | Data Types | 8-bit floating-point exponent type (0S,8E,0M) - unsigned, NaN represented by 0xF | [→](../programming/api/api-nki-language-types.md#nki-language-float8_e8m0fnu) |
| `floor` | language | Operators (op specifiers) | Op specifier for floor. | [→](../programming/api/api-nki-language-operators.md#nki-language-floor) |
| `fmod` | language | Operators (op specifiers) | Op specifier for fmod. | [→](../programming/api/api-nki-language-operators.md#nki-language-fmod) |
| `fori_loop` | language | Dimensions & Indexing | Structured for loop with dynamic bounds. | [→](../programming/api/api-nki-language-dims.md#nki-language-fori_loop) |
| `gather_flattened` | language | Tensor Creation | Gather elements from data tensor using indices after flattening. | [→](../programming/api/api-nki-language-creation.md#nki-language-gather_flattened) |
| `gelu` | language | Operators (op specifiers) | Op specifier for gelu. | [→](../programming/api/api-nki-language-operators.md#nki-language-gelu) |
| `gelu_apprx_sigmoid` | language | Operators (op specifiers) | Op specifier for gelu_apprx_sigmoid. | [→](../programming/api/api-nki-language-operators.md#nki-language-gelu_apprx_sigmoid) |
| `gelu_apprx_sigmoid_dx` | language | Operators (op specifiers) | Op specifier for gelu_apprx_sigmoid_dx. | [→](../programming/api/api-nki-language-operators.md#nki-language-gelu_apprx_sigmoid_dx) |
| `gelu_apprx_tanh` | language | Operators (op specifiers) | Op specifier for gelu_apprx_tanh. | [→](../programming/api/api-nki-language-operators.md#nki-language-gelu_apprx_tanh) |
| `gelu_dx` | language | Operators (op specifiers) | Op specifier for gelu_dx. | [→](../programming/api/api-nki-language-operators.md#nki-language-gelu_dx) |
| `get_nc_version` | isa | Tensor Engine | Returns the nc_version of the current target context. | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-get_nc_version) |
| `get_pattern` | tensor | Tensor Views (NkiTensor methods) | Return the view's access pattern as `[[stride, count], ...]`. | [→](../programming/api/api-nki-tensor.md#nki-tensor-get_pattern) |
| `greater` | language | Operators (op specifiers) | Op specifier for greater. | [→](../programming/api/api-nki-language-operators.md#nki-language-greater) |
| `greater_equal` | language | Operators (op specifiers) | Op specifier for greater_equal. | [→](../programming/api/api-nki-language-operators.md#nki-language-greater_equal) |
| `hbm` | language | Data Movement & Memory | HBM - Alias of private_hbm. | [→](../programming/api/api-nki-language-memory.md#nki-language-hbm) |
| `indirect` | tensor | Tensor Views (NkiTensor methods) | Create an indirect tensor view for Tensor Indirection (TI). | [→](../programming/api/api-nki-tensor.md#nki-tensor-indirect) |
| `int16` | language | Data Types | 16-bit signed integer number. | [→](../programming/api/api-nki-language-types.md#nki-language-int16) |
| `int32` | language | Data Types | 32-bit signed integer number. | [→](../programming/api/api-nki-language-types.md#nki-language-int32) |
| `int64` | language | Data Types | 64-bit signed integer number. | [→](../programming/api/api-nki-language-types.md#nki-language-int64) |
| `int8` | language | Data Types | 8-bit signed integer number. | [→](../programming/api/api-nki-language-types.md#nki-language-int8) |
| `invert` | language | Operators (op specifiers) | Op specifier for invert. | [→](../programming/api/api-nki-language-operators.md#nki-language-invert) |
| `iota` | isa | Utility & Gather | Generate a constant literal pattern into SBUF using GpSimd Engine. | [→](../programming/api/api-nki-isa-utility.md#nki-isa-iota) |
| `is_contiguous` | tensor | Tensor Views (NkiTensor methods) | Return True if the view covers storage contiguously (row-major order). | [→](../programming/api/api-nki-tensor.md#nki-tensor-is_contiguous) |
| `is_indirect` | tensor | Tensor Views (NkiTensor methods) | Return True if this view already uses indirect addressing. | [→](../programming/api/api-nki-tensor.md#nki-tensor-is_indirect) |
| `jit` | nki | Tools | Just-in-time compile a top-level NKI function to run on NeuronDevices. | [→](../programming/api/api-nki-tools.md#nki-jit) |
| `left_shift` | language | Operators (op specifiers) | Op specifier for left_shift. | [→](../programming/api/api-nki-language-operators.md#nki-language-left_shift) |
| `less` | language | Operators (op specifiers) | Op specifier for less. | [→](../programming/api/api-nki-language-operators.md#nki-language-less) |
| `less_equal` | language | Operators (op specifiers) | Op specifier for less_equal. | [→](../programming/api/api-nki-language-operators.md#nki-language-less_equal) |
| `load` | language | Tensor Creation | Load a tensor from device memory (HBM) into on-chip memory (SBUF). | [→](../programming/api/api-nki-language-creation.md#nki-language-load) |
| `load_transpose2d` | language | Tensor Creation | Load a tensor from device memory (HBM) and 2D-transpose the data before storing  | [→](../programming/api/api-nki-language-creation.md#nki-language-load_transpose2d) |
| `local_gather` | isa | Data Movement & Memory | Gather SBUF data in `src_buffer` using `index` on GpSimd Engine. | [→](../programming/api/api-nki-isa-memory.md#nki-isa-local_gather) |
| `log` | language | Operators (op specifiers) | Op specifier for log. | [→](../programming/api/api-nki-language-operators.md#nki-language-log) |
| `log1p` | language | Operators (op specifiers) | Op specifier for log1p. | [→](../programming/api/api-nki-language-operators.md#nki-language-log1p) |
| `logical_and` | language | Operators (op specifiers) | Op specifier for logical_and. | [→](../programming/api/api-nki-language-operators.md#nki-language-logical_and) |
| `logical_not` | language | Operators (op specifiers) | Op specifier for logical_not. | [→](../programming/api/api-nki-language-operators.md#nki-language-logical_not) |
| `logical_or` | language | Operators (op specifiers) | Op specifier for logical_or. | [→](../programming/api/api-nki-language-operators.md#nki-language-logical_or) |
| `logical_xor` | language | Operators (op specifiers) | Op specifier for logical_xor. | [→](../programming/api/api-nki-language-operators.md#nki-language-logical_xor) |
| `matmul` | language | Miscellaneous | x @ y matrix multiplication of x and y. | [→](../programming/api/api-nki-language-misc.md#nki-language-matmul) |
| `matmul_perf_mode` | isa | Miscellaneous | Performance mode for matmul. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-matmul_perf_mode) |
| `max` | language | Miscellaneous | Maximum of elements along the specified axis (or axes) of the input. | [→](../programming/api/api-nki-language-misc.md#nki-language-max) |
| `max8` | isa | Miscellaneous | Find the 8 largest values in each partition of the source tile. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-max8) |
| `maximum` | language | Operators (op specifiers) | Op specifier for maximum. | [→](../programming/api/api-nki-language-operators.md#nki-language-maximum) |
| `mean` | language | Miscellaneous | Arithmetic mean along the specified axis (or axes) of the input. | [→](../programming/api/api-nki-language-misc.md#nki-language-mean) |
| `memset` | isa | Data Movement & Memory | Initialize `dst` by filling it with a compile-time constant `value`, using Vecto | [→](../programming/api/api-nki-isa-memory.md#nki-isa-memset) |
| `min` | language | Miscellaneous | Minimum of elements along the specified axis (or axes) of the input. | [→](../programming/api/api-nki-language-misc.md#nki-language-min) |
| `minimum` | language | Operators (op specifiers) | Op specifier for minimum. | [→](../programming/api/api-nki-language-operators.md#nki-language-minimum) |
| `mish` | language | Operators (op specifiers) | Op specifier for mish. | [→](../programming/api/api-nki-language-operators.md#nki-language-mish) |
| `mod` | language | Operators (op specifiers) | Op specifier for mod. | [→](../programming/api/api-nki-language-operators.md#nki-language-mod) |
| `multiply` | language | Operators (op specifiers) | Op specifier for multiply. | [→](../programming/api/api-nki-language-operators.md#nki-language-multiply) |
| `nc_find_index8` | isa | Tensor Engine | Find indices of the 8 given vals in each partition of the data tensor. | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_find_index8) |
| `nc_match_replace8` | isa | Tensor Engine | Replace first occurrence of each value in `vals` with `imm` in `data` using the  | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_match_replace8) |
| `nc_matmul` | isa | Tensor Engine | Compute `dst = stationary.T @ moving` matrix multiplication using Tensor Engine. | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_matmul) |
| `nc_matmul_mx` | isa | Tensor Engine | Compute matrix multiplication of MXFP8/MXFP4 quantized matrices with integrated  | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_matmul_mx) |
| `nc_n_gather` | isa | Utility & Gather | Gather elements from `data` according to `indices` using GpSimd Engine. | [→](../programming/api/api-nki-isa-utility.md#nki-isa-nc_n_gather) |
| `nc_stream_shuffle` | isa | Tensor Engine | Apply cross-partition data movement within a quadrant of 32 partitions from sour | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_stream_shuffle) |
| `nc_transpose` | isa | Data Movement & Memory | Perform a 2D transpose between the partition axis and the free axis of input `da | [→](../programming/api/api-nki-isa-memory.md#nki-isa-nc_transpose) |
| `nc_version` | isa | Tensor Engine | NeuronCore version. | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-nc_version) |
| `ndarray` | language | Tensor Creation | Create a new tensor of given shape and dtype on the specified buffer. | [→](../programming/api/api-nki-language-creation.md#nki-language-ndarray) |
| `negative` | language | Operators (op specifiers) | Op specifier for negative. | [→](../programming/api/api-nki-language-operators.md#nki-language-negative) |
| `NkiBlockGrid` | language | Tiled & Blocked Layouts | A tensor view navigated in block coordinates. | [→](../programming/api/api-nki-language-layout.md#nki-language-NkiBlockGrid) |
| `NkiGrid` | language | Tiled & Blocked Layouts | Base class for tensor views organized into tiles or blocks. | [→](../programming/api/api-nki-language-layout.md#nki-language-NkiGrid) |
| `NkiTileGrid` | language | Tiled & Blocked Layouts | A tensor view navigated in tile coordinates. | [→](../programming/api/api-nki-language-layout.md#nki-language-NkiTileGrid) |
| `nonzero_with_count` | isa | Miscellaneous | Find indices of nonzero elements in an input tensor and their total count using  | [→](../programming/api/api-nki-isa-misc.md#nki-isa-nonzero_with_count) |
| `not_equal` | language | Operators (op specifiers) | Op specifier for not_equal. | [→](../programming/api/api-nki-language-operators.md#nki-language-not_equal) |
| `num_programs` | language | Dimensions & Indexing | Number of SPMD programs along the given axes in the launch grid. | [→](../programming/api/api-nki-language-dims.md#nki-language-num_programs) |
| `ones` | language | Tensor Creation | Create a new tensor of given shape and dtype on the specified buffer, filled wit | [→](../programming/api/api-nki-language-creation.md#nki-language-ones) |
| `oob_mode` | isa | Miscellaneous | Out-of-bounds access mode. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-oob_mode) |
| `permute` | tensor | Tensor Views (NkiTensor methods) | Reorder tensor dimensions. | [→](../programming/api/api-nki-tensor.md#nki-tensor-permute) |
| `power` | language | Operators (op specifiers) | Op specifier for power. | [→](../programming/api/api-nki-language-operators.md#nki-language-power) |
| `prelu` | language | Operators (op specifiers) | Op specifier for prelu. | [→](../programming/api/api-nki-language-operators.md#nki-language-prelu) |
| `private_hbm` | language | Data Movement & Memory | HBM - Only visible to each individual kernel instance in the SPMD grid. | [→](../programming/api/api-nki-language-memory.md#nki-language-private_hbm) |
| `prod` | language | Miscellaneous | Product of elements along the specified axis (or axes) of the input. | [→](../programming/api/api-nki-language-misc.md#nki-language-prod) |
| `profiler_region_close` | language | Miscellaneous | Mark the end of the most recently opened annotation region. | [→](../programming/api/api-nki-language-misc.md#nki-language-profiler_region_close) |
| `profiler_region_open` | language | Miscellaneous | Mark the start of a named annotation region. | [→](../programming/api/api-nki-language-misc.md#nki-language-profiler_region_open) |
| `program_id` | language | Dimensions & Indexing | Index of the current SPMD program along the given axis in the launch grid. | [→](../programming/api/api-nki-language-dims.md#nki-language-program_id) |
| `program_ndim` | language | Dimensions & Indexing | Number of dimensions in the SPMD launch grid. | [→](../programming/api/api-nki-language-dims.md#nki-language-program_ndim) |
| `psum` | language | Data Movement & Memory | PSUM - Only visible to each individual kernel instance in the SPMD grid. | [→](../programming/api/api-nki-language-memory.md#nki-language-psum) |
| `quantize_mx` | isa | Miscellaneous | Quantize FP16/BF16 data to MXFP8 tensors (both data and scales) using Vector Eng | [→](../programming/api/api-nki-isa-misc.md#nki-isa-quantize_mx) |
| `rand` | language | Tensor Creation | Create a new tensor of given shape and dtype on the specified buffer, filled wit | [→](../programming/api/api-nki-language-creation.md#nki-language-rand) |
| `rand2` | isa | Miscellaneous | Generate pseudo random numbers with uniform distribution using Vector Engine. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-rand2) |
| `rand_get_state` | isa | Miscellaneous | Store the current pseudo random number generator (PRNG) states from the engine. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-rand_get_state) |
| `rand_set_state` | isa | Miscellaneous | Seed the pseudo random number generator (PRNG) inside the engine. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-rand_set_state) |
| `range_select` | isa | Miscellaneous | Select elements from `on_true_tile` based on comparison with bounds using Vector | [→](../programming/api/api-nki-isa-misc.md#nki-isa-range_select) |
| `rank_id` | collectives | Collective Communication | Get the rank ID of the current rank. | [→](../programming/api/api-nki-collectives.md#nki-collectives-rank_id) |
| `rearrange` | tensor | Tensor Views (NkiTensor methods) | Rearrange tensor dimensions using einops-style patterns. | [→](../programming/api/api-nki-tensor.md#nki-tensor-rearrange) |
| `reciprocal` | isa | Miscellaneous | Compute element-wise reciprocal (1.0/x) of the input `data` tile using Vector En | [→](../programming/api/api-nki-isa-misc.md#nki-isa-reciprocal) |
| `reciprocal` | language | Operators (op specifiers) | Op specifier for reciprocal. | [→](../programming/api/api-nki-language-operators.md#nki-language-reciprocal) |
| `reduce_cmd` | isa | Miscellaneous | Engine register reduce commands. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-reduce_cmd) |
| `reduce_scatter` | collectives | Collective Communication | Perform a reduce-scatter on the given replica group and input/output tensors. | [→](../programming/api/api-nki-collectives.md#nki-collectives-reduce_scatter) |
| `register_alloc` | isa | Miscellaneous | Allocate a virtual register and optionally initialize it with a value. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-register_alloc) |
| `register_load` | isa | Miscellaneous | Load a scalar value from memory (HBM or SBUF) into a virtual register. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-register_load) |
| `register_move` | isa | Miscellaneous | Move a value into a virtual register. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-register_move) |
| `register_store` | isa | Miscellaneous | Store the value from a virtual register into memory (HBM/SBUF). | [→](../programming/api/api-nki-isa-misc.md#nki-isa-register_store) |
| `relu` | language | Operators (op specifiers) | Op specifier for relu. | [→](../programming/api/api-nki-language-operators.md#nki-language-relu) |
| `ReplicaGroup` | collectives | Collective Communication | The set of ranks a collective is executed on, as a list of rank-lists. | [→](../programming/api/api-nki-collectives.md#nki-collectives-ReplicaGroup) |
| `reshape` | tensor | Tensor Views (NkiTensor methods) | Reshape the tensor to a new shape without copying data. | [→](../programming/api/api-nki-tensor.md#nki-tensor-reshape) |
| `reshape_dim` | tensor | Tensor Views (NkiTensor methods) | Split a single dimension into multiple dimensions. | [→](../programming/api/api-nki-tensor.md#nki-tensor-reshape_dim) |
| `rg_partition_op` | collectives | Collective Communication | How to map ranks `[0, world_size)` to rank-list ids. | [→](../programming/api/api-nki-collectives.md#nki-collectives-rg_partition_op) |
| `right_shift` | language | Operators (op specifiers) | Op specifier for right_shift. | [→](../programming/api/api-nki-language-operators.md#nki-language-right_shift) |
| `rms_norm` | language | Miscellaneous | Apply Root Mean Square Layer Normalization. | [→](../programming/api/api-nki-language-misc.md#nki-language-rms_norm) |
| `rng` | isa | Miscellaneous | Generate pseudo random numbers using the Vector or GpSimd Engine. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-rng) |
| `rsqrt` | language | Operators (op specifiers) | Op specifier for rsqrt. | [→](../programming/api/api-nki-language-operators.md#nki-language-rsqrt) |
| `sbuf` | language | Data Movement & Memory | State Buffer - Only visible to each individual kernel instance in the SPMD grid. | [→](../programming/api/api-nki-language-memory.md#nki-language-sbuf) |
| `scalar_load` | isa | Miscellaneous | Load a scalar value from a 1x1 tensor. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-scalar_load) |
| `scalar_store` | isa | Miscellaneous | Store a scalar value to a 1x1 tensor in memory. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-scalar_store) |
| `scalar_tensor_tensor` | isa | Tensor Engine | Apply two math operators in sequence using Vector Engine: `(data <op0> operand0) | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-scalar_tensor_tensor) |
| `select` | tensor | Tensor Views (NkiTensor methods) | Select a single element along a dimension, removing it. | [→](../programming/api/api-nki-tensor.md#nki-tensor-select) |
| `select_reduce` | isa | Miscellaneous | Selectively copy elements from either `on_true` or `on_false` to the destination | [→](../programming/api/api-nki-isa-misc.md#nki-isa-select_reduce) |
| `sendrecv` | isa | Logical NeuronCore (LNC) | Perform point-to-point communication between NeuronCores by sending and receivin | [→](../programming/api/api-nki-isa-local-collective.md#nki-isa-sendrecv) |
| `sendrecv_compute` | isa | Logical NeuronCore (LNC) | Perform point-to-point communication between NeuronCores and reduce the local an | [→](../programming/api/api-nki-isa-local-collective.md#nki-isa-sendrecv_compute) |
| `sequence_bounds` | isa | Miscellaneous | Compute the sequence bounds for a given set of segment IDs using GpSIMD Engine. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-sequence_bounds) |
| `sequential_range` | language | Dimensions & Indexing | Create a sequence for fully unrolled loop iteration. | [→](../programming/api/api-nki-language-dims.md#nki-language-sequential_range) |
| `set_rng_seed` | isa | Miscellaneous | Seed the pseudo random number generator (PRNG) inside the Vector Engine. | [→](../programming/api/api-nki-isa-misc.md#nki-isa-set_rng_seed) |
| `shared_constant` | language | Tensor Creation | Create a tensor in shared HBM initialized with constant data. | [→](../programming/api/api-nki-language-creation.md#nki-language-shared_constant) |
| `shared_hbm` | language | Data Movement & Memory | Shared HBM - Visible to all kernel instances in the SPMD grid. | [→](../programming/api/api-nki-language-memory.md#nki-language-shared_hbm) |
| `sigmoid` | language | Operators (op specifiers) | Op specifier for sigmoid. | [→](../programming/api/api-nki-language-operators.md#nki-language-sigmoid) |
| `sign` | language | Operators (op specifiers) | Op specifier for sign. | [→](../programming/api/api-nki-language-operators.md#nki-language-sign) |
| `silu` | language | Operators (op specifiers) | Op specifier for silu. | [→](../programming/api/api-nki-language-operators.md#nki-language-silu) |
| `silu_dx` | language | Operators (op specifiers) | Op specifier for silu_dx. | [→](../programming/api/api-nki-language-operators.md#nki-language-silu_dx) |
| `sin` | language | Operators (op specifiers) | Op specifier for sin. | [→](../programming/api/api-nki-language-operators.md#nki-language-sin) |
| `slice` | tensor | Tensor Views (NkiTensor methods) | Slice along a single dimension. | [→](../programming/api/api-nki-tensor.md#nki-tensor-slice) |
| `softmax` | language | Miscellaneous | Softmax activation function on the input, element-wise. | [→](../programming/api/api-nki-language-misc.md#nki-language-softmax) |
| `softplus` | language | Operators (op specifiers) | Op specifier for softplus. | [→](../programming/api/api-nki-language-operators.md#nki-language-softplus) |
| `sqrt` | language | Operators (op specifiers) | Op specifier for sqrt. | [→](../programming/api/api-nki-language-operators.md#nki-language-sqrt) |
| `square` | language | Operators (op specifiers) | Op specifier for square. | [→](../programming/api/api-nki-language-operators.md#nki-language-square) |
| `squeeze_dim` | tensor | Tensor Views (NkiTensor methods) | Remove a dimension of size 1. | [→](../programming/api/api-nki-tensor.md#nki-tensor-squeeze_dim) |
| `static_range` | language | Dimensions & Indexing | Create a sequence for fully unrolled loop iteration. | [→](../programming/api/api-nki-language-dims.md#nki-language-static_range) |
| `store` | language | Tensor Creation | Store into a tensor on device memory (HBM) from on-chip memory (SBUF). | [→](../programming/api/api-nki-language-creation.md#nki-language-store) |
| `subtract` | language | Operators (op specifiers) | Op specifier for subtract. | [→](../programming/api/api-nki-language-operators.md#nki-language-subtract) |
| `sum` | language | Miscellaneous | Sum of elements along the specified axis (or axes) of the input. | [→](../programming/api/api-nki-language-misc.md#nki-language-sum) |
| `tan` | language | Operators (op specifiers) | Op specifier for tan. | [→](../programming/api/api-nki-language-operators.md#nki-language-tan) |
| `tanh` | language | Operators (op specifiers) | Op specifier for tanh. | [→](../programming/api/api-nki-language-operators.md#nki-language-tanh) |
| `tensor_copy` | isa | Data Movement & Memory | Create a copy of `src` tile within NeuronCore on-chip SRAMs using Vector, Scalar | [→](../programming/api/api-nki-isa-memory.md#nki-isa-tensor_copy) |
| `tensor_copy_predicated` | isa | Tensor Engine | Conditionally copy elements from the `src` tile to the `dst` tile on SBUF / PSUM | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_copy_predicated) |
| `tensor_partition_reduce` | isa | Tensor Engine | Apply a reduction operation across partitions of an input `data` tile using GpSi | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_partition_reduce) |
| `tensor_reduce` | isa | Tensor Engine | Apply a reduction operation to the free axes of an input `data` tile using Vecto | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_reduce) |
| `tensor_scalar` | isa | Tensor Engine | Apply up to two math operators to the input `data` tile by broadcasting scalar/v | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_scalar) |
| `tensor_scalar_cumulative` | isa | Tensor Engine | Perform tensor-scalar arithmetic operation with cumulative reduction using Vecto | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_scalar_cumulative) |
| `tensor_scalar_reduce` | isa | Tensor Engine | Perform the same computation as `nisa.tensor_scalar` with one math operator and  | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_scalar_reduce) |
| `tensor_tensor` | isa | Tensor Engine | Perform an element-wise operation of input two tiles using Vector Engine or GpSi | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_tensor) |
| `tensor_tensor_scan` | isa | Tensor Engine | Perform a scan operation of two input tiles using Vector Engine. | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-tensor_tensor_scan) |
| `tfloat32` | language | Data Types | 32-bit floating-point number (1S,8E,10M). | [→](../programming/api/api-nki-language-types.md#nki-language-tfloat32) |
| `tile_size` | language | Dimensions & Indexing | Hardware tile size constants (pmax, psum_bank_fmax, gemm_stationary_fmax, etc.). | [→](../programming/api/api-nki-language-dims.md#nki-language-tile_size) |
| `tiles` | language | Tiled & Blocked Layouts | Decompose a tensor into a logical grid of fixed-size tiles. | [→](../programming/api/api-nki-language-layout.md#nki-language-tiles) |
| `topk` | isa | Tensor Engine | Find the k largest values and their indices in each 16-partition group using GpS | [→](../programming/api/api-nki-isa-tensor.md#nki-isa-topk) |
| `transpose` | language | Miscellaneous | Transposes a 2D tile between its partition and free dimension. | [→](../programming/api/api-nki-language-misc.md#nki-language-transpose) |
| `trunc` | language | Operators (op specifiers) | Op specifier for trunc. | [→](../programming/api/api-nki-language-operators.md#nki-language-trunc) |
| `uint16` | language | Data Types | 16-bit unsigned integer number. | [→](../programming/api/api-nki-language-types.md#nki-language-uint16) |
| `uint32` | language | Data Types | 32-bit unsigned integer number. | [→](../programming/api/api-nki-language-types.md#nki-language-uint32) |
| `uint64` | language | Data Types | 64-bit unsigned integer number. | [→](../programming/api/api-nki-language-types.md#nki-language-uint64) |
| `uint8` | language | Data Types | 8-bit unsigned integer number. | [→](../programming/api/api-nki-language-types.md#nki-language-uint8) |
| `var` | language | Miscellaneous | Variance along the specified axis (or axes) of the input. | [→](../programming/api/api-nki-language-misc.md#nki-language-var) |
| `vector_select` | tensor | Tensor Views (NkiTensor methods) | Per-partition indirect addressing using a vector of offsets. | [→](../programming/api/api-nki-tensor.md#nki-tensor-vector_select) |
| `view` | tensor | Tensor Views (NkiTensor methods) | Reinterpret the tensor's data as a different dtype. | [→](../programming/api/api-nki-tensor.md#nki-tensor-view) |
| `where` | language | Miscellaneous | Return elements chosen from x or y depending on condition. | [→](../programming/api/api-nki-language-misc.md#nki-language-where) |
| `while_loop` | language | Dimensions & Indexing | Structured while loop with a register condition. | [→](../programming/api/api-nki-language-dims.md#nki-language-while_loop) |
| `zeros` | language | Tensor Creation | Create a new tensor of given shape and dtype on the specified buffer, filled wit | [→](../programming/api/api-nki-language-creation.md#nki-language-zeros) |
| `zeros_like` | language | Miscellaneous | Create a new tensor of zeros with the same shape and type as a given tensor. | [→](../programming/api/api-nki-language-misc.md#nki-language-zeros_like) |
