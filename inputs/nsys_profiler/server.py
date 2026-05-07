"""Stdio MCP server exposing nsys profile analyses as MCP tools.

The orchestrator's profiler agent calls these tools rather than shelling
out to ``python analyze_nsys.py …``. The capture step (``nsys profile``
against a live server) stays a shell command — it's too long-running
for stdio MCP.

Launch (typically spawned by the agent runner via ``MCPServerSpec``):

    python nsys_profiler/server.py
    # or, equivalently:
    uv run python nsys_profiler/server.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import types
from pathlib import Path

from mcp.server.fastmcp import FastMCP


_HERE = Path(__file__).resolve().parent

# Import the analysis module by path so this file is usable both from inside
# the workspace (``nsys_profiler/server.py``) and as a host-side helper.
sys.path.insert(0, str(_HERE))
import analyze_nsys  # noqa: E402  (sys.path setup above)


def _capture(fn, **kwargs) -> str:
    """Run an ``analyze_nsys.cmd_*`` with an argparse-like namespace and
    capture stdout as a string.

    The ``cmd_*`` helpers print their results to stdout; we intercept
    and return the buffered text so the MCP client gets a structured
    reply.
    """
    ns = types.SimpleNamespace(**kwargs)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(ns)
    out = buf.getvalue()
    return out or "(no output)"


def build_server() -> FastMCP:
    """Construct the FastMCP instance with nsys analysis tools.

    Exposed separately so unit tests can introspect registered tools
    without spawning a stdio loop.
    """
    mcp = FastMCP("vibeserve-nsys-profiler")

    @mcp.tool()
    def export(report: str) -> str:
        """Export a .nsys-rep file to .sqlite.

        Args:
            report: Path to the .nsys-rep file (or an already-exported
                .sqlite file; no-op in that case).
        """
        return _capture(analyze_nsys.cmd_export, report=report)

    @mcp.tool()
    def tables(report: str) -> str:
        """List non-empty tables in the SQLite export.

        Args:
            report: Path to the .nsys-rep or .sqlite file.
        """
        return _capture(analyze_nsys.cmd_tables, report=report)

    @mcp.tool()
    def kernels(report: str, top: int = 15) -> str:
        """Top GPU kernels by total execution time.

        Args:
            report: Path to the .nsys-rep or .sqlite file.
            top: Number of kernels to show (default 15).
        """
        return _capture(analyze_nsys.cmd_kernels, report=report, top=top)

    @mcp.tool()
    def cpu_overhead(report: str) -> str:
        """CPU-side CUDA runtime overhead, sync stalls, and launch-bound detection."""
        return _capture(analyze_nsys.cmd_cpu_overhead, report=report)

    @mcp.tool()
    def idle_gaps(report: str, top: int = 10) -> str:
        """Largest GPU idle gaps between kernel launches.

        Args:
            report: Path to the .nsys-rep or .sqlite file.
            top: Number of gaps to show (default 10).
        """
        return _capture(analyze_nsys.cmd_idle_gaps, report=report, top=top)

    @mcp.tool()
    def memory(report: str) -> str:
        """Memory copy and allocation operations."""
        return _capture(analyze_nsys.cmd_memory, report=report)

    @mcp.tool()
    def graph_replays(report: str) -> str:
        """CUDA graph replay statistics (empty if no CUDA graphs are active)."""
        return _capture(analyze_nsys.cmd_graph_replays, report=report)

    @mcp.tool()
    def step_timeline(report: str, step: int = 1) -> str:
        """Per-decode-step kernel breakdown (eager mode only).

        Args:
            report: Path to the .nsys-rep or .sqlite file.
            step: Which decode step to analyze (0-indexed, default 1).
        """
        return _capture(analyze_nsys.cmd_step_timeline, report=report, step=step)

    @mcp.tool()
    def query(report: str, sql: str) -> str:
        """Run arbitrary SQL against the nsys SQLite export.

        Args:
            report: Path to the .nsys-rep or .sqlite file.
            sql: SQL statement to execute.
        """
        return _capture(analyze_nsys.cmd_query, report=report, sql=sql)

    @mcp.tool()
    def summary(report: str, top: int = 15, step: int = 1) -> str:
        """All-in-one analysis: kernels + cpu_overhead + idle_gaps + memory + graph_replays + step_timeline."""
        return _capture(
            analyze_nsys.cmd_summary, report=report, top=top, step=step,
        )

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="vibeserve-nsys-mcp",
        description="Stdio MCP server exposing nsys profile analyses.",
    )
    parser.parse_args(argv)
    mcp = build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
