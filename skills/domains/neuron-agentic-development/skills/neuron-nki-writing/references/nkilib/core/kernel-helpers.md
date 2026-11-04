# Kernel Helpers

## Overview

Kernel helpers provide commonly-used utility functions for NKI kernels: ceiling/floor division, alignment, activation function mapping, SPMD sharding info, data type utilities, and generic list reduction. These are foundational building blocks used throughout the nkilib codebase.

## When to Use

**Always use:**
- `div_ceil(n, d)` — for any tile count computation. Never write `(n + d - 1) // d` inline. Used in 60+ call sites across 20+ production kernels.
- `kernel_assert()` — for all input validation. Never use Python `assert` in NKI kernels.

**Use when needed:**
- `get_ceil_aligned_size()` / `get_floor_aligned_size()` — when allocating buffers that must be aligned to hardware boundaries
- `is_launched_as_spmd()` / `get_program_sharding_info()` — for SPMD-aware kernels that shard across NeuronCores
- `get_max_positive_value_for_dtype()` — when computing softmax masks or clamping to dtype range

## Quick Reference

| Function | Signature | Description |
|----------|-----------|-------------|
| `is_hbm_buffer` | `(tensor: nl.ndarray) -> bool` | Check if tensor buffer is HBM |
| `get_ceil_quotient` | `(numerator, denominator) -> int` | Ceiling division |
| `div_ceil` | `(n, d) -> int` | Ceiling division (alias) |
| `get_ceil_aligned_size` | `(size, alignment_multiple) -> int` | Round up to alignment boundary |
| `get_floor_quotient` | `(numerator, denominator) -> int` | Floor division |
| `get_floor_aligned_size` | `(size, alignment_multiple) -> int` | Round down to alignment boundary |
| `get_nl_act_fn_from_type` | `(act_fn: ActFnType) -> function` | Map enum to NKI activation function |
| `is_launched_as_spmd` | `() -> bool` | Check if running in SPMD mode |
| `get_program_sharding_info` | `() -> Tuple[int, int, int]` | Get (grid_ndim, n_prgs, prg_id) |
| `get_verified_program_sharding_info` | `(kernel_name, allowed_ndims, max_sharding) -> Tuple` | Get sharding info with validation |
| `is_rms_normalization` | `(norm_type: NormType) -> bool` | Check if norm type is RMS |
| `normalization_uses_weights` | `(norm_type: NormType) -> bool` | Check if norm uses weight params |
| `get_max_positive_value_for_dtype` | `(dtype) -> float` | Max positive value for FP8 types |
| `reduce` | `(op, input, initial_value) -> result` | Reduce list with mul/add/min/max |

**Constants:**
| Constant | Value | Description |
|----------|-------|-------------|
| `NUM_HW_PSUM_BANKS` | `8` | Number of hardware PSUM banks |
| `PSUM_BANK_SIZE` | `2048` | Size of each PSUM bank |

## Import Options

**Default** — inline the source into your kernel file.
Source: `references/nkilib/core/utils/kernel_helpers.py`

**If nkilib is installed** in the user's environment:
```python
from nkilib.core.utils.kernel_helpers import (
    is_hbm_buffer,
    get_ceil_quotient,
    div_ceil,
    get_ceil_aligned_size,
    get_floor_quotient,
    get_floor_aligned_size,
    get_program_sharding_info,
    reduce,
)
```

## API Documentation

### `is_hbm_buffer(tensor: nl.ndarray) -> bool`

Check if the tensor's buffer is any HBM type (hbm, shared_hbm, or private_hbm).

**Args:**
- `tensor` (`nl.ndarray`): NKI tensor to check

**Returns:** `True` if the tensor buffer is `nl.hbm`, `nl.shared_hbm`, or `nl.private_hbm`.

```python
hbm_tensor = nl.ndarray((128, 512), dtype=nl.bfloat16, buffer=nl.shared_hbm)
is_hbm_buffer(hbm_tensor)  # True

sbuf_tensor = nl.ndarray((128, 512), dtype=nl.bfloat16, buffer=nl.sbuf)
is_hbm_buffer(sbuf_tensor)  # False
```

---

### `get_ceil_quotient(numerator: int, denominator: int) -> int`

Compute ceiling division using integer arithmetic.

**Args:**
- `numerator` (`int`): Dividend
- `denominator` (`int`): Divisor (must be non-zero)

**Returns:** `(numerator + denominator - 1) // denominator`

```python
get_ceil_quotient(300, 128)  # 3
get_ceil_quotient(256, 128)  # 2
```

---

### `div_ceil(n, d) -> int`

Alias for `get_ceil_quotient`. Commonly used for tile count calculations.

```python
num_tiles = div_ceil(seq_len, tile_size)
```

---

### `get_ceil_aligned_size(size: int, alignment_multiple: int) -> int`

Round `size` up to the nearest multiple of `alignment_multiple`.

**Args:**
- `size` (`int`): Value to align
- `alignment_multiple` (`int`): Alignment boundary

**Returns:** Smallest multiple of `alignment_multiple` >= `size`.

```python
get_ceil_aligned_size(100, 64)  # 128
get_ceil_aligned_size(128, 64)  # 128
```

---

### `get_floor_quotient(numerator: int, denominator: int) -> int`

Floor division (standard Python `//`).

**Returns:** `numerator // denominator`

---

### `get_floor_aligned_size(size: int, alignment_multiple: int) -> int`

Round `size` down to the nearest multiple of `alignment_multiple`.

**Returns:** Largest multiple of `alignment_multiple` <= `size`.

```python
get_floor_aligned_size(100, 64)  # 64
```

---

### `get_nl_act_fn_from_type(act_fn: ActFnType) -> function`

Map an `ActFnType` enum to the corresponding `nki.language` activation function.

**Args:**
- `act_fn` (`ActFnType`): One of `SiLU`, `GELU`, `GELU_Tanh_Approx`, `Swish`

**Returns:** NKI function (`nl.silu`, `nl.gelu`, `nl.gelu_apprx_tanh`, `nl.gelu_apprx_sigmoid`)

**Constraints:** Asserts on unsupported types.

---

### `is_launched_as_spmd() -> bool`

Check if the kernel is running in SPMD (multi-core) mode.

**Returns:** `True` if `program_ndim != 0` and `num_programs(axis=0) > 1`.

---

### `get_program_sharding_info() -> Tuple[int, int, int]`

Get program grid information for SPMD execution.

**Returns:** `(grid_ndim, n_prgs, prg_id)` -- returns `(0, 1, 0)` for non-SPMD.

```python
grid_ndim, n_prgs, prg_id = get_program_sharding_info()
# SPMD with 8 cores: (1, 8, <0-7>)
# Non-SPMD:          (0, 1, 0)
```

---

### `get_verified_program_sharding_info(kernel_name="", allowed_ndims=None, max_sharding=None) -> Tuple[int, int, int]`

Same as `get_program_sharding_info` with optional validation.

**Args:**
- `kernel_name` (`str`): Kernel name for error messages
- `allowed_ndims` (`Tuple[int, ...]`, optional): Allowed grid dimensions
- `max_sharding` (`int`, optional): Maximum sharding degree

---

### `is_rms_normalization(norm_type: NormType) -> bool`

Check if the normalization type is RMS-based (`RMS_NORM` or `RMS_NORM_SKIP_GAMMA`).

---

### `normalization_uses_weights(norm_type: NormType) -> bool`

Check if the normalization type uses weight parameters (`RMS_NORM` or `LAYER_NORM`).

---

### `get_max_positive_value_for_dtype(dtype) -> float`

Get maximum positive representable value for FP8 data types.

**Args:**
- `dtype`: `nl.float8_e4m3` or `nl.float8_e5m2`

**Returns:** `240.0` for e4m3, `57344.0` for e5m2, `None` for other types.

---

### `reduce(op='mul', input: List = None, initial_value=None) -> result`

Perform a reduction operation over a list.

**Args:**
- `op` (`str`): One of `'mul'`, `'add'`, `'min'`, `'max'`
- `input` (`List`): Values to reduce
- `initial_value`: Starting accumulator value

**Returns:** Reduced value.

**Constraints:** Both `input` and `initial_value` must be non-None. `op` must be one of the supported operations.

```python
reduce(op='mul', input=[2, 3, 4], initial_value=1)  # 24
reduce(op='add', input=[10, 20, 30], initial_value=0)  # 60
```

## Usage Examples

### Pattern 1: Computing tile counts and aligned sizes

```python
from nkilib.core.utils.kernel_helpers import div_ceil, get_ceil_aligned_size

seq_len = 1000
tile_size = 128

num_tiles = div_ceil(seq_len, tile_size)  # 8
aligned_seq = get_ceil_aligned_size(seq_len, tile_size)  # 1024
```

### Pattern 2: SPMD multi-core sharding

```python
import nki.language as nl
from nkilib.core.utils.kernel_helpers import get_program_sharding_info, div_ceil

grid_ndim, n_prgs, prg_id = get_program_sharding_info()

# Shard batch dimension across cores
batch_size = 32
batches_per_core = div_ceil(batch_size, n_prgs)
my_batch_start = prg_id * batches_per_core
my_batch_end = min(my_batch_start + batches_per_core, batch_size)
```

### Pattern 3: Computing total elements with reduce

```python
from nkilib.core.utils.kernel_helpers import reduce

shape = [128, 4, 512]
total_elements = reduce(op='mul', input=shape, initial_value=1)  # 262144
```

## Dependencies

- **kernel_assert** (`nkilib.core.utils.kernel_assert`): Used for input validation
- **common_types** (`nkilib.core.utils.common_types`): Provides `ActFnType` and `NormType` enums
- **nki.language** (`nl`): NKI language API for activation functions and program info

## Source

See `references/nkilib/core/utils/kernel_helpers.py` for the full implementation.
