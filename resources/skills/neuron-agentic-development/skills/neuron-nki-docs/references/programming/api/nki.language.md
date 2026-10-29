<a id="nki-language"></a>

# nki.language

The `nki.language` module provides high-level constructs for writing NKI kernels.
It includes tensor creation, indexing, type casting, math operations, and loop constructs
that the NKI compiler translates into efficient hardware instructions.

<a id="nl_creation"></a>

## Creation operations

| Symbol | Summary |
| --- | --- |
| [ndarray](api-nki-language-creation.md#nki-language-ndarray) | Create a new tensor of given shape and dtype on the specified buffer. |
| [zeros](api-nki-language-creation.md#nki-language-zeros) | Create a new tensor of given shape and dtype on the specified buffer, filled with zeros. |
| [ones](api-nki-language-creation.md#nki-language-ones) | Create a new tensor of given shape and dtype on the specified buffer, filled with ones. |
| [zeros_like](api-nki-language-misc.md#nki-language-zeros_like) | Create a new tensor of zeros with the same shape and type as a given tensor. |
| [empty_like](api-nki-language-layout.md#nki-language-empty_like) | Allocate independent, uninitialized storage with matching geometry. |
| [shared_constant](api-nki-language-creation.md#nki-language-shared_constant) | Create a tensor in shared HBM initialized with constant data. |
| [rand](api-nki-language-creation.md#nki-language-rand) | Create a new tensor of given shape and dtype on the specified buffer, filled with random values. |

<a id="nl_tensor_class"></a>

## Tensor class

`NkiTensor` is the tensor type returned by the creation operations
above. Its view methods (`slice`, `select`, `reshape`, `permute`,
`view`, `ap`, etc.) return new `NkiTensor` views that share the same
underlying storage, without copying data.

<a id="nl_tiled_grids"></a>

## Tiled grids

Tiled grids organize an existing tensor into logical tile or block coordinates
without copying its data.

| Symbol | Summary |
| --- | --- |
| [tiles](api-nki-language-layout.md#nki-language-tiles) | Decompose a tensor into a logical grid of fixed-size tiles. |
| [blocks](api-nki-language-layout.md#nki-language-blocks) | Create a block view over a tensor where each block contains multiple tiles. |
| [alloc_tiles](api-nki-language-layout.md#nki-language-alloc_tiles) | Allocate independent, uninitialized storage organized as a grid of tiles. |
| [alloc_blocks](api-nki-language-layout.md#nki-language-alloc_blocks) | Allocate new storage organized as a grid of tile blocks. |

| Symbol | Summary |
| --- | --- |
| [NkiGrid](api-nki-language-layout.md#nki-language-NkiGrid) | Base class for tensor views organized into tiles or blocks. |
| [NkiTileGrid](api-nki-language-layout.md#nki-language-NkiTileGrid) | A tensor view navigated in tile coordinates. |
| [NkiBlockGrid](api-nki-language-layout.md#nki-language-NkiBlockGrid) | A tensor view navigated in block coordinates. |

<a id="nl_tensor_ops"></a>

## Tensor operations

| Symbol | Summary |
| --- | --- |
| [load](api-nki-language-creation.md#nki-language-load) | Load a tensor from device memory (HBM) into on-chip memory (SBUF). |
| [load_transpose2d](api-nki-language-creation.md#nki-language-load_transpose2d) | Load a tensor from device memory (HBM) and 2D-transpose the data before storing into on-chip memory (SBUF). |
| [store](api-nki-language-creation.md#nki-language-store) | Store into a tensor on device memory (HBM) from on-chip memory (SBUF). |
| [copy](api-nki-language-operators.md#nki-language-copy) | Op specifier for copy. |
| [matmul](api-nki-language-misc.md#nki-language-matmul) | x @ y matrix multiplication of x and y. |
| [transpose](api-nki-language-misc.md#nki-language-transpose) | Transposes a 2D tile between its partition and free dimension. |

<a id="nl_operators"></a>

## Operators

<a id="nl_math"></a>

## Math operations

| Symbol | Summary |
| --- | --- |
| [abs](api-nki-language-operators.md#nki-language-abs) | Op specifier for abs. |
| [abs_max](api-nki-language-operators.md#nki-language-abs_max) | Op specifier for abs_max. |
| [abs_min](api-nki-language-operators.md#nki-language-abs_min) | Op specifier for abs_min. |
| [add](api-nki-language-operators.md#nki-language-add) | Op specifier for add. |
| [arctan](api-nki-language-operators.md#nki-language-arctan) | Op specifier for arctan. |
| [ceil](api-nki-language-operators.md#nki-language-ceil) | Op specifier for ceil. |
| [cos](api-nki-language-operators.md#nki-language-cos) | Op specifier for cos. |
| [exp](api-nki-language-operators.md#nki-language-exp) | Op specifier for exp. |
| [floor](api-nki-language-operators.md#nki-language-floor) | Op specifier for floor. |
| [log](api-nki-language-operators.md#nki-language-log) | Op specifier for log. |
| [maximum](api-nki-language-operators.md#nki-language-maximum) | Op specifier for maximum. |
| [minimum](api-nki-language-operators.md#nki-language-minimum) | Op specifier for minimum. |
| [multiply](api-nki-language-operators.md#nki-language-multiply) | Op specifier for multiply. |
| [negative](api-nki-language-operators.md#nki-language-negative) | Op specifier for negative. |
| [power](api-nki-language-operators.md#nki-language-power) | Op specifier for power. |
| [reciprocal](api-nki-language-operators.md#nki-language-reciprocal) | Op specifier for reciprocal. |
| [rsqrt](api-nki-language-operators.md#nki-language-rsqrt) | Op specifier for rsqrt. |
| [sign](api-nki-language-operators.md#nki-language-sign) | Op specifier for sign. |
| [sin](api-nki-language-operators.md#nki-language-sin) | Op specifier for sin. |
| [sqrt](api-nki-language-operators.md#nki-language-sqrt) | Op specifier for sqrt. |
| [square](api-nki-language-operators.md#nki-language-square) | Op specifier for square. |
| [subtract](api-nki-language-operators.md#nki-language-subtract) | Op specifier for subtract. |
| [tan](api-nki-language-operators.md#nki-language-tan) | Op specifier for tan. |
| [tanh](api-nki-language-operators.md#nki-language-tanh) | Op specifier for tanh. |
| [trunc](api-nki-language-operators.md#nki-language-trunc) | Op specifier for trunc. |

<a id="nl_activation_and_backpropagation"></a>

## Activation and Backpropagation functions

| Symbol | Summary |
| --- | --- |
| [relu](api-nki-language-operators.md#nki-language-relu) | Op specifier for relu. |
| [sigmoid](api-nki-language-operators.md#nki-language-sigmoid) | Op specifier for sigmoid. |
| [silu](api-nki-language-operators.md#nki-language-silu) | Op specifier for silu. |
| [silu_dx](api-nki-language-operators.md#nki-language-silu_dx) | Op specifier for silu_dx. |
| [gelu](api-nki-language-operators.md#nki-language-gelu) | Op specifier for gelu. |
| [gelu_dx](api-nki-language-operators.md#nki-language-gelu_dx) | Op specifier for gelu_dx. |
| [gelu_apprx_sigmoid](api-nki-language-operators.md#nki-language-gelu_apprx_sigmoid) | Op specifier for gelu_apprx_sigmoid. |
| [gelu_apprx_sigmoid_dx](api-nki-language-operators.md#nki-language-gelu_apprx_sigmoid_dx) | Op specifier for gelu_apprx_sigmoid_dx. |
| [gelu_apprx_tanh](api-nki-language-operators.md#nki-language-gelu_apprx_tanh) | Op specifier for gelu_apprx_tanh. |
| [mish](api-nki-language-operators.md#nki-language-mish) | Op specifier for mish. |
| [softplus](api-nki-language-operators.md#nki-language-softplus) | Op specifier for softplus. |
| [log1p](api-nki-language-operators.md#nki-language-log1p) | Op specifier for log1p. |
| [softmax](api-nki-language-misc.md#nki-language-softmax) | Softmax activation function on the input, element-wise. |
| [erf](api-nki-language-operators.md#nki-language-erf) | Op specifier for erf. |
| [erf_dx](api-nki-language-operators.md#nki-language-erf_dx) | Op specifier for erf_dx. |

<a id="nl_normalization_and_regularization"></a>

## Normalization and Regularization functions

| Symbol | Summary |
| --- | --- |
| [dropout](api-nki-language-misc.md#nki-language-dropout) | Randomly zeroes some of the elements of the input tile given a probability rate. |
| [rms_norm](api-nki-language-misc.md#nki-language-rms_norm) | Apply Root Mean Square Layer Normalization. |

<a id="nl_reduction"></a>

## Reduction operations

| Symbol | Summary |
| --- | --- |
| [all](api-nki-language-misc.md#nki-language-all) | Whether all elements along the specified axis (or axes) evaluate to True. |
| [max](api-nki-language-misc.md#nki-language-max) | Maximum of elements along the specified axis (or axes) of the input. |
| [mean](api-nki-language-misc.md#nki-language-mean) | Arithmetic mean along the specified axis (or axes) of the input. |
| [min](api-nki-language-misc.md#nki-language-min) | Minimum of elements along the specified axis (or axes) of the input. |
| [prod](api-nki-language-misc.md#nki-language-prod) | Product of elements along the specified axis (or axes) of the input. |
| [sum](api-nki-language-misc.md#nki-language-sum) | Sum of elements along the specified axis (or axes) of the input. |
| [var](api-nki-language-misc.md#nki-language-var) | Variance along the specified axis (or axes) of the input. |

<a id="nl_comparison"></a>

## Comparison operations

| Symbol | Summary |
| --- | --- |
| [equal](api-nki-language-operators.md#nki-language-equal) | Op specifier for equal. |
| [not_equal](api-nki-language-operators.md#nki-language-not_equal) | Op specifier for not_equal. |
| [less](api-nki-language-operators.md#nki-language-less) | Op specifier for less. |
| [less_equal](api-nki-language-operators.md#nki-language-less_equal) | Op specifier for less_equal. |
| [greater](api-nki-language-operators.md#nki-language-greater) | Op specifier for greater. |
| [greater_equal](api-nki-language-operators.md#nki-language-greater_equal) | Op specifier for greater_equal. |

<a id="nl_logical"></a>

## Logical operations

| Symbol | Summary |
| --- | --- |
| [logical_and](api-nki-language-operators.md#nki-language-logical_and) | Op specifier for logical_and. |
| [logical_or](api-nki-language-operators.md#nki-language-logical_or) | Op specifier for logical_or. |
| [logical_xor](api-nki-language-operators.md#nki-language-logical_xor) | Op specifier for logical_xor. |
| [logical_not](api-nki-language-operators.md#nki-language-logical_not) | Op specifier for logical_not. |

<a id="nl_bitwise"></a>

## Bitwise operations

| Symbol | Summary |
| --- | --- |
| [bitwise_and](api-nki-language-operators.md#nki-language-bitwise_and) | Op specifier for bitwise_and. |
| [bitwise_or](api-nki-language-operators.md#nki-language-bitwise_or) | Op specifier for bitwise_or. |
| [bitwise_xor](api-nki-language-operators.md#nki-language-bitwise_xor) | Op specifier for bitwise_xor. |
| [invert](api-nki-language-operators.md#nki-language-invert) | Op specifier for invert. |
| [left_shift](api-nki-language-operators.md#nki-language-left_shift) | Op specifier for left_shift. |
| [right_shift](api-nki-language-operators.md#nki-language-right_shift) | Op specifier for right_shift. |

<a id="nl_tensor_manipulation_operations"></a>

## Tensor manipulation operations

| Symbol | Summary |
| --- | --- |
| [broadcast_to](api-nki-language-misc.md#nki-language-broadcast_to) | Broadcast a tile to a new shape following numpy broadcasting rules. |
| [ds](api-nki-language-dims.md#nki-language-ds) | A `size`-element window starting at `start`. |
| [expand_dims](api-nki-language-misc.md#nki-language-expand_dims) | Expand the shape of a tile. |

<a id="nl_indexing"></a>

## Indexing operations

| Symbol | Summary |
| --- | --- |
| [where](api-nki-language-misc.md#nki-language-where) | Return elements chosen from x or y depending on condition. |
| [gather_flattened](api-nki-language-creation.md#nki-language-gather_flattened) | Gather elements from data tensor using indices after flattening. |

<a id="nl_iterators"></a>

## Iterators

| Symbol | Summary |
| --- | --- |
| [affine_range](api-nki-language-dims.md#nki-language-affine_range) | Create a sequence for fully unrolled loop iteration. |
| [dynamic_range](api-nki-language-dims.md#nki-language-dynamic_range) | Create a sequence for **dynamic** loop iteration. |
| [sequential_range](api-nki-language-dims.md#nki-language-sequential_range) | Create a sequence for fully unrolled loop iteration. |
| [static_range](api-nki-language-dims.md#nki-language-static_range) | Create a sequence for fully unrolled loop iteration. |

<a id="nl_control_flow"></a>

## Control Flow

| Symbol | Summary |
| --- | --- |
| [fori_loop](api-nki-language-dims.md#nki-language-fori_loop) | Structured for loop with dynamic bounds. |
| [while_loop](api-nki-language-dims.md#nki-language-while_loop) | Structured while loop with a register condition. |

<a id="nl_memory_hierarchy"></a>

## Memory Hierarchy

| Symbol | Summary |
| --- | --- |
| [psum](api-nki-language-memory.md#nki-language-psum) | PSUM - Only visible to each individual kernel instance in the SPMD grid. |
| [sbuf](api-nki-language-memory.md#nki-language-sbuf) | State Buffer - Only visible to each individual kernel instance in the SPMD grid. |
| [hbm](api-nki-language-memory.md#nki-language-hbm) | HBM - Alias of private_hbm. |
| [private_hbm](api-nki-language-memory.md#nki-language-private_hbm) | HBM - Only visible to each individual kernel instance in the SPMD grid. |
| [shared_hbm](api-nki-language-memory.md#nki-language-shared_hbm) | Shared HBM - Visible to all kernel instances in the SPMD grid. |

<a id="nl_others"></a>

## Others

| Symbol | Summary |
| --- | --- |
| [device_print](api-nki-language-creation.md#nki-language-device_print) | Print a message with a string prefix followed by the value of a tile. |
| [program_id](api-nki-language-dims.md#nki-language-program_id) | Index of the current SPMD program along the given axis in the launch grid. |
| [num_programs](api-nki-language-dims.md#nki-language-num_programs) | Number of SPMD programs along the given axes in the launch grid. |
| [program_ndim](api-nki-language-dims.md#nki-language-program_ndim) | Number of dimensions in the SPMD launch grid. |

<a id="nl_datatypes"></a>

## Data Types

| Symbol | Summary |
| --- | --- |
| [bool_](api-nki-language-types.md#nki-language-bool_) | Boolean (True or False) stored as a byte. |
| [int8](api-nki-language-types.md#nki-language-int8) | 8-bit signed integer number. |
| [int16](api-nki-language-types.md#nki-language-int16) | 16-bit signed integer number. |
| [int32](api-nki-language-types.md#nki-language-int32) | 32-bit signed integer number. |
| [uint8](api-nki-language-types.md#nki-language-uint8) | 8-bit unsigned integer number. |
| [uint16](api-nki-language-types.md#nki-language-uint16) | 16-bit unsigned integer number. |
| [uint32](api-nki-language-types.md#nki-language-uint32) | 32-bit unsigned integer number. |
| [float16](api-nki-language-types.md#nki-language-float16) | 16-bit floating-point number. |
| [float32](api-nki-language-types.md#nki-language-float32) | 32-bit floating-point number. |
| [bfloat16](api-nki-language-types.md#nki-language-bfloat16) | 16-bit floating-point number (1S,8E,7M). |
| [tfloat32](api-nki-language-types.md#nki-language-tfloat32) | 32-bit floating-point number (1S,8E,10M). |
| [float8_e4m3](api-nki-language-types.md#nki-language-float8_e4m3) | 8-bit floating-point number (1S,4E,3M). |
| [float8_e5m2](api-nki-language-types.md#nki-language-float8_e5m2) | 8-bit floating-point number (1S,5E,2M). |
| [float8_e4m3fn](api-nki-language-types.md#nki-language-float8_e4m3fn) | 8-bit floating-point number (1S,4E,3M), Extended range: no inf, NaN represented by 0bS111'1111. |
| [float8_e8m0fnu](api-nki-language-types.md#nki-language-float8_e8m0fnu) | 8-bit floating-point exponent type (0S,8E,0M) - unsigned, NaN represented by 0xFF. |
| [float8_e5m2_x4](api-nki-language-types.md#nki-language-float8_e5m2_x4) | 4x packed float8_e5m2 elements, custom data type for nki.isa.nc_matmul_mx on NeuronCore-v4. |
| [float8_e4m3fn_x4](api-nki-language-types.md#nki-language-float8_e4m3fn_x4) | 4x packed float8_e4m3fn elements, custom data type for nki.isa.nc_matmul_mx on NeuronCore-v4. |
| [float4_e2m1fn_x4](api-nki-language-types.md#nki-language-float4_e2m1fn_x4) | 4x packed float4_e2m1fn elements, custom data type for nki.isa.nc_matmul_mx on NeuronCore-v4. |

<a id="nl_constants"></a>

## Constants

| [tile_size](api-nki-language-dims.md#nki-language-tile_size) | Hardware tile size constants (pmax, psum_bank_fmax, gemm_stationary_fmax, etc.) |
| --- | --- |

<a id="nl_operator_constants"></a>

## Operator Constants

| Symbol | Summary |
| --- | --- |
| [prelu](api-nki-language-operators.md#nki-language-prelu) | Op specifier for prelu. |
| [bypass](api-nki-language-operators.md#nki-language-bypass) | Op specifier for bypass. |
