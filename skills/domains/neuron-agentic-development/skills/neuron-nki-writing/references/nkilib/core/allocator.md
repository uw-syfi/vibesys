# SbufManager (Allocator)

## Overview

SbufManager is a user-space stack/heap allocator for SBUF memory on NeuronCore. It manages a contiguous SBUF region with a stack growing upward and a heap growing downward, supporting scoped allocation with automatic cleanup, multi-buffer interleaving, and detailed allocation logging.

## When to Use

Adopt SbufManager when:
- **4+ SBUF tensors** are allocated in the kernel — centralized allocation prevents address conflicts
- **Sub-functions share SBUF space** — pass the allocator as a parameter for composable allocation across call sites
- **Multi-buffering / double-buffering** — `open_scope(interleave_degree=2)` + `increment_section()` enables ping-pong buffers
- **Scoped lifetimes** — `open_scope()`/`close_scope()` automatically reclaim stack space when a phase completes

**Skip when**: the kernel is simple with 1-3 SBUF tensors allocated at the top level.

Used in 23 production kernels including attention, QKV projection, MLP, MoE, normalization, and output projection. It is the most widely used allocator in the TKG kernel family.

## Quick Reference

| Method / Function | Signature | Description |
|-------------------|-----------|-------------|
| `SbufManager.__init__` | `(sb_lower_bound, sb_upper_bound, logger=None, use_auto_alloc=False, default_stack_alloc=True)` | Create allocator for an SBUF region |
| `open_scope` | `(interleave_degree=1, name="")` | Push a new stack scope |
| `close_scope` | `()` | Pop scope and free its stack allocations |
| `increment_section` | `()` | Advance multi-buffer section (modular within scope) |
| `alloc` | `(shape, dtype, buffer=nl.sbuf, name=None, base_partition=0, align=None)` | Allocate on default (stack or heap) |
| `alloc_stack` | `(shape, dtype, buffer=nl.sbuf, name=None, base_partition=0, align=None)` | Allocate on stack (auto-freed with scope) |
| `alloc_heap` | `(shape, dtype, buffer=nl.sbuf, name=None, base_partition=0, align=None)` | Allocate on heap (manual free) |
| `pop_heap` | `()` | Free most recent heap allocation |
| `get_total_space` | `() -> int` | Total managed SBUF bytes |
| `get_free_space` | `() -> int` | Available SBUF bytes |
| `get_used_space` | `() -> int` | Used SBUF bytes |
| `get_stack_curr_addr` | `() -> int` | Current stack pointer |
| `get_heap_curr_addr` | `() -> int` | Current heap pointer |
| `align_stack_curr_addr` | `(align=32)` | Align stack pointer to boundary |
| `set_name_prefix` | `(prefix)` | Set prefix for tensor names |
| `get_name_prefix` | `() -> str` | Get current name prefix |
| `flush_logs` | `()` | Print buffered allocation tree |
| `create_auto_alloc_manager` | `(logger=None) -> SbufManager` | Create auto-alloc manager (function) |
| `sizeinbytes` | `(dtype) -> int` | Bytes per element for dtype |
| `align_to` | `(value, alignment) -> int` | Align value up to boundary |
| `num_elts` | `(shape) -> int` | Product of shape elements |

## Import Options

**Default** — inline the source into your kernel file.
Source: `references/nkilib/core/utils/allocator.py`

**If nkilib is installed** in the user's environment:
```python
from nkilib.core.utils.allocator import SbufManager, create_auto_alloc_manager
```

## API Documentation

### `SbufManager.__init__(sb_lower_bound, sb_upper_bound, logger=None, use_auto_alloc=False, default_stack_alloc=True)`

Create an SBUF memory manager.

**Args:**
- `sb_lower_bound` (`int`): Lower bound of available SBUF region (stack starts here)
- `sb_upper_bound` (`int`): Upper bound of available SBUF region (heap starts here)
- `logger` (`Logger`, optional): Logger instance; creates default "SBM" logger if None
- `use_auto_alloc` (`bool`): If True, skip address assignment (let compiler allocate)
- `default_stack_alloc` (`bool`): If True, `alloc()` uses stack; if False, uses heap

```python
sbm = SbufManager(0, 128 * 1024)  # 128KB SBUF region
```

---

### `open_scope(interleave_degree=1, name="")`

Push a new stack scope. All stack allocations within this scope are freed when `close_scope()` is called.

**Args:**
- `interleave_degree` (`int`): Number of multi-buffer sections (default: 1 = no interleaving)
- `name` (`str`): Optional scope name for debug logging

```python
sbm.open_scope(interleave_degree=2, name="kv_loop")
```

---

### `close_scope()`

Pop the current scope and free all stack allocations made within it. When the last scope closes, allocation statistics are printed.

---

### `increment_section()`

Advance the multi-buffer section counter. When `cur_section_id` reaches `interleave_degree`, it resets to 0 and the stack pointer returns to the scope's starting address. This enables circular buffer patterns in loops.

```python
sbm.open_scope(interleave_degree=2)
for iteration in range(4):
    buf = sbm.alloc_stack((128, 512), dtype=nl.bfloat16)
    sbm.increment_section()
    # iteration 0: addr=0,   section -> 1
    # iteration 1: addr=1024, section -> 0 (reset)
    # iteration 2: addr=0,   section -> 1 (reuses iter 0 address)
    # iteration 3: addr=1024, section -> 0 (reuses iter 1 address)
sbm.close_scope()
```

---

### `alloc(shape, dtype, buffer=nl.sbuf, name=None, base_partition=0, align=None)`

Allocate on default target (stack or heap based on `default_stack_alloc`).

**Args:**
- `shape` (`tuple`): Tensor shape; first dim is partition, rest are free dims
- `dtype`: Data type (`nl.bfloat16`, `nl.float32`, etc.)
- `buffer`: Buffer type (only `nl.sbuf` supported)
- `name` (`str`, optional): Unique tensor name
- `base_partition` (`int`): Base partition index (default: 0)
- `align` (`int`, optional): Alignment in bytes

**Returns:** `nl.ndarray` allocated in SBUF.

---

### `alloc_stack(shape, dtype, buffer=nl.sbuf, name=None, base_partition=0, align=None)`

Allocate on the stack. Requires an open scope. Freed automatically when scope closes.

**Args:** Same as `alloc()`.

**Returns:** `nl.ndarray` allocated on the stack.

**Constraints:**
- Must have an open scope
- `buffer` must be `nl.sbuf`
- Must have sufficient free space (stack_addr + size <= heap_addr)

```python
sbm.open_scope(name="main")
weights = sbm.alloc_stack((128, 512), dtype=nl.bfloat16, name="weights")
sbm.close_scope()  # weights freed
```

---

### `alloc_heap(shape, dtype, buffer=nl.sbuf, name=None, base_partition=0, align=None)`

Allocate on the heap (grows downward from upper bound). Must be freed manually with `pop_heap()`.

**Args:** Same as `alloc()`.

**Returns:** `nl.ndarray` allocated on the heap.

---

### `pop_heap()`

Free the most recently allocated heap tensor (LIFO order).

**Constraints:** Heap must not be empty.

---

### `get_total_space() -> int`

Return total managed SBUF size in bytes.

---

### `get_free_space() -> int`

Return available SBUF bytes between stack and heap pointers.

---

### `get_used_space() -> int`

Return used SBUF bytes (total - free).

---

### `create_auto_alloc_manager(logger=None) -> SbufManager`

Factory function to create a SbufManager initialized with the full available SBUF space, using auto-allocation mode (compiler assigns addresses).

```python
sbm = create_auto_alloc_manager()
```

---

### `sizeinbytes(dtype) -> int`

Return byte size per element for a given NKI data type.

| dtype | Size |
|-------|------|
| `nl.float32`, `nl.int32`, `nl.uint32` | 4 |
| `nl.float8_e4m3fn_x4`, `nl.float8_e5m2_x4` | 4 |
| `nl.bfloat16`, `nl.float16`, `nl.uint16`, `nl.int16` | 2 |
| `nl.float4_e2m1fn_x4` | 2 |
| `nl.int8`, `nl.uint8`, `float8_e4m3`, `float8_e5m2`, `float8e4`, `float8e5` | 1 |

---

### `align_to(value, alignment) -> int`

Round `value` up to the next multiple of `alignment`.

---

### `num_elts(shape) -> int`

Return the product of all elements in `shape`.

## Usage Examples

### Pattern 1: Basic scoped allocation

```python
import nki.language as nl
from nkilib.core.utils.allocator import SbufManager

sbm = SbufManager(0, 128 * 1024)

sbm.open_scope(name="main_loop")

# Allocate working buffers
input_tile = sbm.alloc_stack((128, 512), dtype=nl.bfloat16, name="input")
output_tile = sbm.alloc_stack((128, 512), dtype=nl.bfloat16, name="output")

# ... perform computation ...

sbm.close_scope()  # Both tensors freed
```

### Pattern 2: Double-buffering with interleave

```python
import nki.language as nl
from nkilib.core.utils.allocator import SbufManager

sbm = SbufManager(0, 128 * 1024)

sbm.open_scope(name="outer")

# Double-buffered loop
sbm.open_scope(interleave_degree=2, name="double_buf")
for tile_idx in range(num_tiles):
    buf = sbm.alloc_stack((128, 512), dtype=nl.bfloat16, name="kv_tile")
    # Load into buf, compute...
    sbm.increment_section()  # Alternate between 2 physical buffers
sbm.close_scope()

sbm.close_scope()
```

### Pattern 3: Mixed stack + heap allocation

```python
import nki.language as nl
from nkilib.core.utils.allocator import SbufManager

sbm = SbufManager(0, 128 * 1024)

# Heap allocation for long-lived tensors
persistent = sbm.alloc_heap((128, 1024), dtype=nl.float32, name="persistent")

sbm.open_scope(name="compute")
# Stack allocation for temporary buffers
temp = sbm.alloc_stack((128, 256), dtype=nl.bfloat16, name="temp")
# ... compute using persistent and temp ...
sbm.close_scope()  # temp freed, persistent still alive

sbm.pop_heap()  # Manually free persistent
```

## Dependencies

- **kernel_assert** (`nkilib.core.utils.kernel_assert`): Runtime assertion checks
- **logging** (`nkilib.core.utils.logging`): `Logger` and `get_logger` for allocation logging
- **tree_logger** (`nkilib.core.utils.tree_logger`): `TreeLogger` for hierarchical allocation tree output
- **nki.language** (`nl`): NKI tensor allocation and buffer types

## Source

See `references/nkilib/core/utils/allocator.py` for the full implementation.
