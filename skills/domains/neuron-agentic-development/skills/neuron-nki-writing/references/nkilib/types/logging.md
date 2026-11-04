# Logging

## Overview

Lightweight logging system for NKI kernels with environment-based configuration and hierarchical tree-style output. Use `get_logger()` for standard debug/info/warn/error logging, and `TreeLogger` for structured, tree-formatted allocation or hierarchy logs.

## Quick Reference

| Name | Type | Description |
|------|------|-------------|
| `LogLevel` | Enum | Log severity levels: DEBUG, INFO, WARN, ERROR, OFF |
| `Logger` | Class | Core logger with level-filtered `debug`/`info`/`warn`/`error` methods |
| `get_logger(name, level)` | Function | Factory function that creates a logger respecting env var overrides |
| `logger` | Instance | Pre-configured global logger instance for quick use |
| `LogEntry` | Dataclass | Buffered log entry for tree-style printing |
| `TreeLogger` | Class | Buffers log entries and prints them in tree format |

## Import Options

**Default** — inline the source into your kernel file.
Source: `references/nkilib/core/utils/logging.py`

**If nkilib is installed** in the user's environment:
```python
from nkilib.core.utils.logging import get_logger, Logger, LogLevel, logger
from nkilib.core.utils.tree_logger import TreeLogger, LogEntry
```

## API Documentation

### `LogLevel` (Enum)

Log severity levels controlling which messages are emitted.

| Value | Int | Description |
|-------|-----|-------------|
| `DEBUG` | 0 | Detailed diagnostic information |
| `INFO` | 1 | General operational information (default level) |
| `WARN` | 2 | Warning conditions |
| `ERROR` | 3 | Error conditions |
| `OFF` | 999 | Suppress all log output |

**Static Method:**

#### `LogLevel.from_string(level: str) -> LogLevel`
Converts a string name (e.g., `"DEBUG"`, `"INFO"`) to the corresponding `LogLevel` enum value. Raises `KeyError` for invalid strings.

---

### `Logger`

Core logging class. Extends `nl.NKIObject` for NKI compatibility.

#### `Logger(name: str, level: LogLevel = LogLevel.INFO)`

**Args:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | (required) | Logger name, displayed in `[name]` prefix |
| `level` | `LogLevel` | `LogLevel.INFO` | Minimum severity level to emit |

#### `Logger.debug(msg: str)`
Log a message at DEBUG level. Output format: `[DEBUG] [name] msg`

#### `Logger.info(msg: str)`
Log a message at INFO level. Output format: `[INFO] [name] msg`

#### `Logger.warn(msg: str)`
Log a message at WARN level. Output format: `[WARN] [name] msg`

#### `Logger.error(msg: str)`
Log a message at ERROR level. Output format: `[ERROR] [name] msg`

#### `Logger.is_enabled_for(level: LogLevel) -> bool`
Check if a given level would be logged. Useful to guard expensive message construction.

```python
if my_logger.is_enabled_for(LogLevel.DEBUG):
    my_logger.debug(f"Expensive computation: {compute_debug_info()}")
```

---

### `get_logger(name: str, level: LogLevel = LogLevel.INFO) -> Logger`

Factory function that creates a `Logger` with environment-variable-aware level resolution.

**Priority Order (highest to lowest):**
1. `NKILIB_LOG_LEVEL_<name>` -- Per-logger env var override
2. `NKILIB_LOG_LEVEL` -- Global env var override
3. `level` parameter -- Code-specified default
4. `LogLevel.INFO` -- System default

**Args:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | (required) | Logger name for output prefix and env var matching |
| `level` | `LogLevel` | `LogLevel.INFO` | Code-level default if no env overrides are found |

**Returns:** Configured `Logger` instance.

**Environment Variables:**
- `NKILIB_LOG_LEVEL=<LEVEL>` -- Set default level for all loggers (e.g., `NKILIB_LOG_LEVEL=DEBUG`)
- `NKILIB_LOG_LEVEL_<name>=<LEVEL>` -- Override a specific logger (e.g., `NKILIB_LOG_LEVEL_SBM=DEBUG`)

**Example:**
```python
from nkilib.core.utils.logging import get_logger, LogLevel

# Basic usage - respects NKILIB_LOG_LEVEL env var
log = get_logger("MyKernel")
log.info("Starting kernel")

# With explicit code-level default
log = get_logger("MyKernel", level=LogLevel.DEBUG)
log.debug("Detailed info")  # Shows unless env var overrides to higher level
```

---

### `logger` (global instance)

A pre-configured global `Logger` instance created via `get_logger("")`. Provided for backward compatibility and quick use.

```python
from nkilib.core.utils.logging import logger

logger.info("Quick message without creating a named logger")
```

---

### `LogEntry` (Dataclass)

Buffered log entry used by `TreeLogger` for tree-style output. Extends `nl.NKIObject`.

**Fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `msg` | `str` | (required) | Log message text |
| `depth` | `int` | (required) | Nesting depth in the tree (0 = root) |
| `is_stack` | `bool` | (required) | `True` for stack allocations, `False` for heap |
| `is_scope_boundary` | `bool` | `False` | `True` for scope open/close entries |

---

### `TreeLogger`

Buffers log entries and prints them in a tree-formatted structure with box-drawing characters. Extends `nl.NKIObject`. Useful for visualizing hierarchical allocation or structural information.

#### `TreeLogger(name: str, logger: Logger)`

**Args:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Name displayed in the tree header |
| `logger` | `Logger` | Parent logger instance used for the header line |

#### `TreeLogger.log(msg: str, depth: int, is_scope_boundary: bool = False)`
Add a log entry to the buffer at the specified depth.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `msg` | `str` | (required) | Message text |
| `depth` | `int` | (required) | Tree nesting depth (0 = root level) |
| `is_scope_boundary` | `bool` | `False` | Mark as scope boundary entry |

#### `TreeLogger.flush()`
Print all buffered entries in tree format, then clear the buffer. Output format:

```
[INFO] [name] Allocations:
    root_entry
    ├── child_1
    │   ├── grandchild_a
    │   └── grandchild_b
    └── child_2
```

**Example:**
```python
from nkilib.core.utils.logging import get_logger
from nkilib.core.utils.tree_logger import TreeLogger

log = get_logger("Allocator")
tree = TreeLogger("SBUF", log)

tree.log("Buffer pool", depth=0)
tree.log("weights: 128x512 float16", depth=1)
tree.log("activations: 128x1024 float16", depth=1)
tree.log("scratch: 128x256 float32", depth=1)
tree.flush()
# Output:
# [INFO] [Allocator] [SBUF] Allocations:
#     Buffer pool
#     ├── weights: 128x512 float16
#     ├── activations: 128x1024 float16
#     └── scratch: 128x256 float32
```

## Usage Examples

### Pattern 1: Named logger with environment override
```python
from nkilib.core.utils.logging import get_logger, LogLevel

# In code: default to INFO
log = get_logger("QKV", level=LogLevel.INFO)
log.info("Processing QKV projection")
log.debug("This won't show at INFO level")

# At runtime, override with: NKILIB_LOG_LEVEL_QKV=DEBUG
# Now debug messages will appear
```

### Pattern 2: Guard expensive debug messages
```python
from nkilib.core.utils.logging import get_logger, LogLevel

log = get_logger("MatMul")
if log.is_enabled_for(LogLevel.DEBUG):
    # Only compute debug string if DEBUG is enabled
    log.debug(f"Tile shapes: {[t.shape for t in tiles]}")
```

### Pattern 3: Tree logger for allocation visualization
```python
from nkilib.core.utils.logging import get_logger
from nkilib.core.utils.tree_logger import TreeLogger

alloc_log = get_logger("Alloc")
tree = TreeLogger("MemLayout", alloc_log)

tree.log("SBUF Allocations", depth=0)
tree.log("Layer 0", depth=1)
tree.log("weights: [128, 512] fp16 @ 0x0000", depth=2)
tree.log("bias: [128, 1] fp16 @ 0x1000", depth=2)
tree.log("Layer 1", depth=1)
tree.log("weights: [128, 256] fp16 @ 0x2000", depth=2)
tree.flush()
```

## Dependencies

- **`nki.language.NKIObject`** -- Base class for NKI-compatible objects (both `Logger` and `TreeLogger` extend this).
- **Python standard library**: `os`, `sys`, `enum.Enum`, `dataclasses.dataclass`.

## Source

See `references/nkilib/core/utils/logging.py` for the full implementation.
