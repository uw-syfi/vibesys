# nki.language

nki.language

## Creation operations


| [ ndarray ](generated/nki.language.ndarray.md#nki.language.ndarray) | Create a new tensor of given shape and dtype on the specified buffer. |
| --- | --- |
| [ zeros ](generated/nki.language.zeros.md#nki.language.zeros) | Create a new tensor of given shape and dtype on the specified buffer, filled with zeros. |


## Tensor manipulation operations


| [ ds ](generated/nki.language.ds.md#nki.language.ds) | Construct a dynamic slice for simple tensor indexing. |
| --- | --- |


## Iterators

In NKI 0.3.0, all range iterators are unified and have identical effect. Use standard Python `range` for all loops. The legacy `nl.affine_range`, `nl.sequential_range`, and `nl.static_range` are retained as aliases but have no distinct behavior.

| [ static_range ](generated/nki.language.static_range.md#nki.language.static_range) | Legacy alias for `range`. |
| --- | --- |
| [ affine_range ](generated/nki.language.affine_range.md#nki.language.affine_range) | Legacy alias for `range`. |
| [ sequential_range ](generated/nki.language.sequential_range.md#nki.language.sequential_range) | Legacy alias for `range`. |


## Memory Hierarchy


| [ psum ](generated/nki.language.psum.md#nki.language.psum) | PSUM - Only visible to each individual kernel instance in the SPMD grid |
| --- | --- |
| [ sbuf ](generated/nki.language.sbuf.md#nki.language.sbuf) | State Buffer - Only visible to each individual kernel instance in the SPMD grid |
| [ hbm ](generated/nki.language.hbm.md#nki.language.hbm) | HBM - Alias of private_hbm |
| [ private_hbm ](generated/nki.language.private_hbm.md#nki.language.private_hbm) | HBM - Only visible to each individual kernel instance in the SPMD grid |
| [ shared_hbm ](generated/nki.language.shared_hbm.md#nki.language.shared_hbm) | Shared HBM - Visible to all kernel instances in the SPMD grid |


## Others


| [ program_id ](generated/nki.language.program_id.md#nki.language.program_id) | Index of the current SPMD program along the given axis in the launch grid. |
| --- | --- |
| [ num_programs ](generated/nki.language.num_programs.md#nki.language.num_programs) | Number of SPMD programs along the given axes in the launch grid. |
| [ program_ndim ](generated/nki.language.program_ndim.md#nki.language.program_ndim) | Number of dimensions in the SPMD launch grid. |
| [ device_print ](generated/nki.language.device_print.md#nki.language.device_print) | Print a message with a string print_prefix followed by the value of a tile tensor . |


## Data Types


| [ bool_ ](generated/nki.language.bool_.md#nki.language.bool_) | Boolean (True or False) stored as a byte |
| --- | --- |
| [ int8 ](generated/nki.language.int8.md#nki.language.int8) | 8-bit signed integer number |
| [ int16 ](generated/nki.language.int16.md#nki.language.int16) | 16-bit signed integer number |
| [ int32 ](generated/nki.language.int32.md#nki.language.int32) | 32-bit signed integer number |
| [ uint8 ](generated/nki.language.uint8.md#nki.language.uint8) | 8-bit unsigned integer number |
| [ uint16 ](generated/nki.language.uint16.md#nki.language.uint16) | 16-bit unsigned integer number |
| [ uint32 ](generated/nki.language.uint32.md#nki.language.uint32) | 32-bit unsigned integer number |
| [ float16 ](generated/nki.language.float16.md#nki.language.float16) | 16-bit floating-point number |
| [ float32 ](generated/nki.language.float32.md#nki.language.float32) | 32-bit floating-point number |
| [ bfloat16 ](generated/nki.language.bfloat16.md#nki.language.bfloat16) | 16-bit floating-point number (1S,8E,7M) |
| [ tfloat32 ](generated/nki.language.tfloat32.md#nki.language.tfloat32) | 32-bit floating-point number (1S,8E,10M) |
| [ float8_e4m3 ](generated/nki.language.float8_e4m3.md#nki.language.float8_e4m3) | 8-bit floating-point number (1S,4E,3M) |
| [ float8_e5m2 ](generated/nki.language.float8_e5m2.md#nki.language.float8_e5m2) | 8-bit floating-point number (1S,5E,2M) |
| [ float8_e5m2_x4 ](generated/nki.language.float8_e5m2_x4.md#nki.language.float8_e5m2_x4) | 4x packed float8_e5m2 elements, custom data type for nki.isa.nc_matmul_mx on NeuronCore-v4 |
| [ float8_e4m3fn_x4 ](generated/nki.language.float8_e4m3fn_x4.md#nki.language.float8_e4m3fn_x4) | 4x packed float8_e4m3fn elements, custom data type for nki.isa.nc_matmul_mx on NeuronCore-v4 |
| [ float4_e2m1fn_x4 ](generated/nki.language.float4_e2m1fn_x4.md#nki.language.float4_e2m1fn_x4) | 4x packed float4_e2m1fn elements, custom data type for nki.isa.nc_matmul_mx on NeuronCore-v4 |


## Constants


| [ tile_size ](generated/nki.language.tile_size.md#nki.language.tile_size) | Tile size constants. |
| --- | --- |