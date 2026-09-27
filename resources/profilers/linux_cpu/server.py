"""MCP tools for collecting a separate Linux native CPU profile."""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from vs_runtime.api.infrastructure import (
    collect_linux_profile,
    detect_linux_profiler,
    parse_profile_command,
    summarize_linux_profile,
)


def build_server() -> FastMCP:
    """Build the profiler MCP server."""
    mcp = FastMCP("vibesys-linux-cpu-profiler")

    @mcp.tool()
    def capabilities() -> dict:
        """Report Linux perf availability and host profiling restrictions."""
        capability = detect_linux_profiler()
        return {
            "selected": capability.tool.value,
            "perf_path": capability.perf_path,
            "perf_version": capability.perf_version,
            "perf_event_paranoid": capability.perf_event_paranoid,
            "kptr_restrict": capability.kptr_restrict,
            "diagnostics": [item.value for item in capability.diagnostics],
        }

    @mcp.tool()
    def profile(
        command: str,
        output_dir: str = "logs/linux_cpu_profile",
        timeout: int | None = None,
        frequency: int = 99,
        call_graph: str = "fp",
    ) -> dict:
        """Profile a diagnostic run. This never supplies a scored benchmark result."""
        result = collect_linux_profile(
            parse_profile_command(command),
            Path(output_dir),
            timeout=timeout,
            frequency=frequency,
            call_graph=call_graph,
        )
        return {
            "status": result.status,
            "tool": result.tool.value,
            "output_dir": result.output_dir,
            "stat_artifact": result.stat_artifact,
            "record_artifact": result.record_artifact,
            "report_artifact": result.report_artifact,
            "metadata": result.metadata,
            "diagnostics": [item.value for item in result.diagnostics],
            "counters": list(result.counters),
            "hot_symbols": list(result.hot_symbols),
            "summary": result.summary,
        }

    @mcp.tool()
    def summary(output_dir: str = "logs/linux_cpu_profile") -> dict:
        """Summarize a previously collected Linux CPU profile directory."""
        return summarize_linux_profile(Path(output_dir))

    return mcp


if __name__ == "__main__":
    build_server().run(transport="stdio")
