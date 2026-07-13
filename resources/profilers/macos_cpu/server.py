"""MCP tools for collecting a separate macOS native CPU profile."""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from vibe_serve.macos_cpu_profiler import collect, detect_capability, parse_command


def build_server() -> FastMCP:
    mcp = FastMCP("vibeserve-macos_cpu-profiler")

    @mcp.tool()
    def capabilities() -> dict:
        """Report whether Instruments, sample, or no native profiler is usable."""
        capability = detect_capability()
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
        result = collect(parse_command(command), Path(output_dir), duration=duration, warmup=warmup)
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
