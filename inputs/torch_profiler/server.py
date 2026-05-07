"""Stdio MCP server exposing torch.profiler analyses as MCP tools.

The orchestrator's profiler agent calls these tools to analyze a
captured ``prof.json``. The *capture* itself (``capture`` /
``capture-server`` subcommands of ``analyze_torch_profile.py``) stays a
shell command — it loads the model and runs a benchmark loop, which is
too long-running for stdio MCP.

Launch:

    python torch_profiler/server.py
    # or
    uv run python torch_profiler/server.py
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

sys.path.insert(0, str(_HERE))
import analyze_torch_profile  # noqa: E402


def _capture(fn, **kwargs) -> str:
    ns = types.SimpleNamespace(**kwargs)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(ns)
    out = buf.getvalue()
    return out or "(no output)"


def build_server() -> FastMCP:
    """Construct the FastMCP instance with torch-profiler analysis tools."""
    mcp = FastMCP("vibeserve-torch-profiler")

    @mcp.tool()
    def tables(report: str) -> str:
        """Overview of a captured prof.json (event categories + totals).

        Args:
            report: Path to the prof.json file.
        """
        return _capture(analyze_torch_profile.cmd_tables, report=report)

    @mcp.tool()
    def kernels(report: str, top: int = 15) -> str:
        """Top GPU kernels by self-CUDA time.

        Args:
            report: Path to the prof.json file.
            top: Number of kernels to show (default 15).
        """
        return _capture(analyze_torch_profile.cmd_kernels, report=report, top=top)

    @mcp.tool()
    def operators(report: str, top: int = 15) -> str:
        """Top operators (aten::*, torch::*) by self-CPU time.

        Args:
            report: Path to the prof.json file.
            top: Number of operators to show (default 15).
        """
        return _capture(analyze_torch_profile.cmd_operators, report=report, top=top)

    @mcp.tool()
    def cpu_overhead(report: str) -> str:
        """CPU vs GPU time ratio — detects launch-bound / compute-bound.

        Args:
            report: Path to the prof.json file.
        """
        return _capture(analyze_torch_profile.cmd_cpu_overhead, report=report)

    @mcp.tool()
    def memory(report: str) -> str:
        """Memory allocation / transfer events."""
        return _capture(analyze_torch_profile.cmd_memory, report=report)

    @mcp.tool()
    def summary(report: str, top: int = 15) -> str:
        """All-in-one: overhead + kernels + operators + memory.

        Args:
            report: Path to the prof.json file.
            top: Number of kernels/operators per section (default 15).
        """
        return _capture(analyze_torch_profile.cmd_summary, report=report, top=top)

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="vibeserve-torch-mcp",
        description="Stdio MCP server exposing torch.profiler analyses.",
    )
    parser.parse_args(argv)
    mcp = build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
