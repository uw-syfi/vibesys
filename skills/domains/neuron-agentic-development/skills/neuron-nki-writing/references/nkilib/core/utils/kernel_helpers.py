# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
General kernel helper functions and utilities for NKI kernels.

This module provides common utility functions used across various NKI kernel
implementations, including mathematical helpers, activation function mappings,
program sharding information, data type utilities, and reduction operations.
These utilities are designed to be reusable across different kernel types
and provide consistent behavior for common operations.
"""

from typing import List, Optional, Tuple

import nki.isa as nisa
import nki.language as nl

from .common_types import ActFnType, NormType
from .kernel_assert import kernel_assert

# TODO: Get this constant from the NKI API once it is available
NUM_HW_PSUM_BANKS = 8
PSUM_BANK_SIZE = 2048

#
# Local constants and data structures
_max_pos_range_map = {
    nl.float8_e4m3: 240.0,
    nl.float8_e5m2: 57344.0,
}

_act_fn_map = {
    ActFnType.SiLU: nl.silu,
    ActFnType.GELU: nl.gelu,
    ActFnType.GELU_Tanh_Approx: nl.gelu_apprx_tanh,
    ActFnType.Swish: nl.gelu_apprx_sigmoid,
}


# def is_sbuf_tensor(t):
#   """Checks whether the input nt.tensor or tensor view is in SBUF. """
#   return isinstance(t, NeuronSBTensor) or (hasattr(t, '_tensor') and isinstance(t._tensor, NeuronSBTensor))

# def is_psum_tensor(t):
#   """Checks whether the input nt.tensor or tensor view is in PSUM. """
#   return isinstance(t, NeuronPSUMTensor) or (hasattr(t, '_tensor') and isinstance(t._tensor, NeuronPSUMTensor))


def get_ceil_quotient(numerator: int, denominator: int) -> int:
    """
    Compute ceiling division of numerator by denominator.

    Returns the smallest integer greater than or equal to numerator/denominator.

    Args:
        numerator (int): The dividend value.
        denominator (int): The divisor value (must be non-zero).

    Returns:
        int: Ceiling of numerator divided by denominator.

    Notes:
        - Uses integer arithmetic to avoid floating-point precision issues
        - Commonly used for calculating number of tiles needed

    Pseudocode:
        result = (numerator + denominator - 1) // denominator
        return result
    """
    return (numerator + denominator - 1) // denominator


def get_ceil_aligned_size(size: int, alignment_multiple: int) -> int:
    """
    Compute size aligned to next multiple of alignment_multiple.

    Rounds size up to the nearest multiple of alignment_multiple.

    Args:
        size (int): The size value to align.
        alignment_multiple (int): The alignment boundary.

    Returns:
        int: Size aligned to next multiple of alignment_multiple.

    Notes:
        - Used for memory alignment requirements
        - Result is always >= size

    Pseudocode:
        quotient = get_ceil_quotient(size, alignment_multiple)
        result = quotient * alignment_multiple
        return result
    """
    return get_ceil_quotient(size, alignment_multiple) * alignment_multiple


def get_floor_quotient(numerator: int, denominator: int) -> int:
    """
    Compute floor division of numerator by denominator.

    Returns the largest integer less than or equal to numerator/denominator.

    Args:
        numerator (int): The dividend value.
        denominator (int): The divisor value (must be non-zero).

    Returns:
        int: Floor of numerator divided by denominator.

    Notes:
        - Standard integer division in Python
        - Used for calculating complete tiles

    Pseudocode:
        result = numerator // denominator
        return result
    """
    return numerator // denominator


def get_floor_aligned_size(size: int, alignment_multiple: int) -> int:
    """
    Compute size aligned to previous multiple of alignment_multiple.

    Rounds size down to the nearest multiple of alignment_multiple.

    Args:
        size (int): The size value to align.
        alignment_multiple (int): The alignment boundary.

    Returns:
        int: Size aligned to previous multiple of alignment_multiple.

    Notes:
        - Used for memory alignment requirements
        - Result is always <= size

    Pseudocode:
        quotient = get_floor_quotient(size, alignment_multiple)
        result = quotient * alignment_multiple
        return result
    """
    return get_floor_quotient(size, alignment_multiple) * alignment_multiple


def is_hbm_buffer(tensor: nl.ndarray) -> bool:
    """Check if tensor buffer is any HBM type (hbm, shared_hbm, private_hbm)."""
    return tensor.buffer in (nl.hbm, nl.shared_hbm, nl.private_hbm)


def get_nl_act_fn_from_type(act_fn: ActFnType):
    """
    Convert ActFnType enum to NKI language activation function.

    Maps activation function type enum to corresponding nl.* function.

    Args:
        act_fn (ActFnType): Activation function type enum.

    Returns:
        function: Corresponding NKI language activation function.

    Notes:
        - Supports SiLU, GELU, GELU_Tanh_Approx, and Swish
        - Raises assertion error for unsupported types

    Pseudocode:
        if act_fn == SiLU:
            return nl.silu
        elif act_fn == GELU:
            return nl.gelu
        elif act_fn == GELU_Tanh_Approx:
            return nl.gelu_apprx_tanh
        elif act_fn == Swish:
            return nl.gelu_apprx_sigmoid
        else:
            raise error
    """
    kernel_assert(isinstance(act_fn, ActFnType), f"Unsupported activation function type: {act_fn}")
    if act_fn == ActFnType.SiLU:
        return nl.silu
    elif act_fn == ActFnType.GELU:
        return nl.gelu
    elif act_fn == ActFnType.GELU_Tanh_Approx:
        return nl.gelu_apprx_tanh
    elif act_fn == ActFnType.Swish:
        return nl.gelu_apprx_sigmoid


def is_launched_as_spmd() -> bool:
    """
    Check if kernel is launched in SPMD (Single Program Multiple Data) mode.

    Determines if the kernel is running with multiple programs on axis 0.

    Args:
        None

    Returns:
        bool: True if launched as SPMD, False otherwise.

    Notes:
        - SPMD mode enables multi-core parallelism
        - Checks both program_ndim and num_programs

    Pseudocode:
        if program_ndim != 0 and num_programs(axis=0) > 1:
            return True
        else:
            return False
    """
    return nl.program_ndim() != 0 and nl.num_programs(axes=0) > 1


def is_rms_normalization(norm_type: NormType) -> bool:
    """
    Check if normalization type is RMS normalization.

    Determines if the normalization type is RMS_NORM or RMS_NORM_SKIP_GAMMA.

    Args:
        norm_type (NormType): Normalization type enum.

    Returns:
        bool: True if RMS normalization, False otherwise.

    Notes:
        - RMS_NORM_SKIP_GAMMA is a variant that skips gamma scaling
        - Both are considered RMS normalization

    Pseudocode:
        if norm_type == RMS_NORM or norm_type == RMS_NORM_SKIP_GAMMA:
            return True
        else:
            return False
    """
    return norm_type == NormType.RMS_NORM or norm_type == NormType.RMS_NORM_SKIP_GAMMA


def normalization_uses_weights(norm_type: NormType) -> bool:
    """
    Check if normalization type uses weight parameters.

    Determines if the normalization requires weight/gamma parameters.

    Args:
        norm_type (NormType): Normalization type enum.

    Returns:
        bool: True if normalization uses weights, False otherwise.

    Notes:
        - RMS_NORM and LAYER_NORM both use weight parameters
        - RMS_NORM_SKIP_GAMMA does not use weights

    Pseudocode:
        if norm_type == RMS_NORM or norm_type == LAYER_NORM:
            return True
        else:
            return False
    """
    return norm_type == NormType.RMS_NORM or norm_type == NormType.LAYER_NORM


def get_program_sharding_info() -> Tuple[int, int, int]:
    """
    Get program sharding information for current execution.

    Retrieves grid dimensionality, number of programs, and program ID.

    Args:
        None

    Returns:
        Tuple[int, int, int]: (grid_ndim, n_prgs, prg_id)
            - grid_ndim: Number of dimensions in program grid
            - n_prgs: Total number of programs on axis 0
            - prg_id: Current program ID on axis 0

    Notes:
        - Returns (0, 1, 0) for non-SPMD execution
        - Used for multi-core sharding strategies

    Pseudocode:
        grid_ndim = program_ndim()
        if grid_ndim != 0:
            n_prgs = num_programs(axis=0)
            prg_id = program_id(axis=0)
        else:
            n_prgs = 1
            prg_id = 0
        return (grid_ndim, n_prgs, prg_id)
    """
    grid_ndim = nl.program_ndim()
    n_prgs, prg_id = (nl.num_programs(axes=0), nl.program_id(axis=0)) if grid_ndim != 0 else (1, 0)
    return grid_ndim, n_prgs, prg_id


def get_verified_program_sharding_info(
    kernel_name: str = "",
    allowed_ndims: Optional[Tuple[int, ...]] = None,
    max_sharding: Optional[int] = None,
) -> Tuple[int, int, int]:
    """
    Get and optionally verify program sharding information.

    Retrieves sharding info and performs optional validation checks.

    Args:
        kernel_name (str): Name of kernel for error messages (optional).
        allowed_ndims (Optional[Tuple[int, ...]]): Allowed grid dimensions (optional).
        max_sharding (Optional[int]): Maximum sharding degree (optional).

    Returns:
        Tuple[int, int, int]: (grid_ndim, n_prgs, prg_id)

    Notes:
        - Currently performs minimal validation
        - Intended for future validation enhancements

    Pseudocode:
        grid_ndim, n_prgs, prg_id = get_program_sharding_info()
        # Optional validation checks
        return (grid_ndim, n_prgs, prg_id)
    """
    grid_ndim, n_prgs, prg_id = get_program_sharding_info()
    ndim_check = allowed_ndims is None or (grid_ndim == allowed_ndims[0] if len(allowed_ndims) == 1 else False)
    return grid_ndim, n_prgs, prg_id


def div_ceil(n, d):
    """
    Compute ceiling division (alias for get_ceil_quotient).

    Returns the smallest integer greater than or equal to n/d.

    Args:
        n (int): Numerator.
        d (int): Denominator.

    Returns:
        int: Ceiling of n divided by d.

    Notes:
        - Alias for get_ceil_quotient for backward compatibility
        - Commonly used for tile count calculations

    Pseudocode:
        result = (n + d - 1) // d
        return result
    """
    return (n + d - 1) // d


def get_max_positive_value_for_dtype(dtype) -> float:
    """
    Get maximum positive value for given data type.

    Returns the maximum representable positive value for FP8 types.

    Args:
        dtype: Data type (nl.float8_e4m3 or nl.float8_e5m2).

    Returns:
        float: Maximum positive value, or None if dtype not in map.

    Notes:
        - float8_e4m3: max = 240.0
        - float8_e5m2: max = 57344.0
        - Returns None for unsupported types

    Pseudocode:
        if dtype == "float8e4":
            dtype = nl.float8_e4m3
        result = lookup dtype in _max_pos_range_map
        return result
    """
    if str(dtype) == "float8e4":
        dtype = nl.float8_e4m3
    result = _max_pos_range_map.get(dtype, None)
    return result


def reduce(op='mul', input: List = None, initial_value=None):
    """
    Perform reduction operation on a list of values.

    Applies a reduction operation (multiply, add, min, or max) across all elements
    in the input list, starting from an initial value.

    Args:
        op (str): Reduction operation - 'mul', 'add', 'min', or 'max'.
        input (List): List of values to reduce.
        initial_value: Starting value for the reduction.

    Returns:
        result: Reduced value after applying operation to all input elements.

    Notes:
        - Supports 'mul', 'add', 'min', and 'max' operations
        - Requires both input and initial_value to be non-None
        - Used for computing products, sums, minimums, or maximums of values

    Pseudocode:
        validate initial_value is not None
        validate input is not None
        validate op in ['mul', 'add', 'max', 'min']
        result = initial_value
        for value in input:
            if op == 'mul':
                result = result * value
            elif op == 'add':
                result = result + value
            elif op == 'min':
                result = min(result, value)
            elif op == 'max':
                result = max(result, value)
        return result
    """
    kernel_assert(initial_value is not None, f"initial_value need to set")
    kernel_assert(input is not None, f"input need to be set")
    kernel_assert(op in ['mul', 'add', 'max', 'min'], f"only op {op} is supported")
    for value in input:
        if op == 'mul':
            initial_value = initial_value * value
        elif op == 'add':
            initial_value = initial_value + value
        elif op == 'min':
            initial_value = min(initial_value, value)
        elif op == 'max':
            initial_value = max(initial_value, value)
    return initial_value


def resolve_dtype_to_nki(dtype):
    """
    Get NKI dtype from the dtype of some other package.

    Args:
        dtype: Data type.

    Returns:
        dtype: The same dtype in NKI.

    Notes:
        float8_e4m3fn gets resolved to float8_e4m3 if using gen3 or older

    Pseudocode:
        result = lookup nki dtype in from string representation
        return result
    """
    dtype_str = str(dtype)
    if dtype_str == 'bool':
        return nl.bool
    elif dtype_str == 'int8':
        return nl.int8
    elif dtype_str == 'int16':
        return nl.int16
    elif dtype_str == 'int32':
        return nl.int32
    elif dtype_str == 'uint8':
        return nl.uint8
    elif dtype_str == 'uint16':
        return nl.uint16
    elif dtype_str == 'uint32':
        return nl.uint32
    elif dtype_str == 'float16':
        return nl.float16
    elif dtype_str == 'float32':
        return nl.float32
    elif dtype_str == 'bfloat16':
        return nl.bfloat16
    elif dtype_str == 'float8_e4m3':
        return nl.float8_e4m3
    elif dtype_str in ['float8_e4m3fn', 'float8e4']:
        # TODO: switch to nisa.get_nc_version() >= nki.isa.nc_version.gen4 after the comparison support
        return nl.float8_e4m3fn if nisa.get_nc_version() == nisa.nc_version.gen4 else nl.float8_e4m3
    elif dtype_str in ['float8_e5m2', 'float8e5']:
        return nl.float8_e5m2
    elif dtype_str == str(nl.float4_e2m1fn_x4):
        return nl.float4_e2m1fn_x4
    elif dtype_str == str(nl.float8_e4m3fn_x4):
        return nl.float8_e4m3fn_x4
    elif dtype_str == str(nl.float8_e5m2_x4):
        return nl.float8_e5m2_x4
    kernel_assert(False, f'Unrecognized dtype {dtype_str}')
