"""MCP tools for collecting a separate macOS native CPU profile."""

import importlib
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from vs_runtime.api.infrastructure import (
    MacOSProfilerEffects,
    collect_macos_profile,
    detect_macos_profiler,
    parse_profile_command,
)
from vs_sim.api import OsThreads, SystemClock

# The same common runtime is staged beside each standalone profiler bundle.
for _common_name in ("_common", "profilers_common"):
    _common_path = Path(__file__).resolve().parent.parent / _common_name
    if (_common_path / "strict_arguments.py").is_file():
        sys.path.insert(0, str(_common_path))
        break

StrictFastMCP = importlib.import_module("strict_arguments").StrictFastMCP


def build_server() -> FastMCP:
    """Build the profiler MCP server."""
    mcp = StrictFastMCP("vibesys-macos_cpu-profiler")

    @mcp.tool()
    def capabilities() -> dict:
        """Report whether Instruments, sample, or no native profiler is usable."""
        capability = detect_macos_profiler()
        return {
            "selected": capability.tool.value,
            "xcode_path": capability.xcode_path,
            "xctrace_path": capability.xctrace_path,
            "sample_path": capability.sample_path,
            "tool_version": capability.tool_version,
            "diagnostics": [item.value for item in capability.diagnostics],
        }

    @mcp.tool()
    def profile(
        command: str,
        output_dir: str = "logs/macos_cpu_profile",
        duration: int = 10,
        warmup: float = 1.0,
    ) -> dict:
        """Profile a diagnostic run. This never supplies a scored benchmark result."""
        result = collect_macos_profile(
            parse_profile_command(command),
            Path(output_dir),
            duration=duration,
            warmup=warmup,
            effects=MacOSProfilerEffects(now=SystemClock().now, sleep=OsThreads().sleep),
        )
        return {
            "status": result.status,
            "tool": result.tool.value,
            "artifact": result.artifact,
            "metadata": result.metadata,
            "target_pid": result.target_pid,
            "diagnostics": [item.value for item in result.diagnostics],
            "summary": result.summary,
        }

    return mcp


if __name__ == "__main__":
    build_server().run(transport="stdio")
