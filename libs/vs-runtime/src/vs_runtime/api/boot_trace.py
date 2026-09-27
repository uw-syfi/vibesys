"""Public boot-trace facade over the runtime-owned implementation."""

from vs_runtime._boot_trace import (
    BOOT_TRACE_ENV,
    LAUNCH_START_ENV,
    child_env,
    drain_log_lines,
    mark_launch,
    span,
    trace_enabled,
    traced,
)

__all__ = [
    "BOOT_TRACE_ENV",
    "LAUNCH_START_ENV",
    "child_env",
    "drain_log_lines",
    "mark_launch",
    "span",
    "trace_enabled",
    "traced",
]
