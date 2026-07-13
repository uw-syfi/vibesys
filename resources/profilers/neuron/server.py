"""Stdio MCP server exposing neuron-explorer profile analyses as MCP tools.

The orchestrator's profiler agent calls these tools rather than shelling
out to ``python neuron_profiler/analyze_neuron.py …``. The capture step
(``neuron-explorer inspect`` around a live workload) stays a shell command
— it's too long-running for stdio MCP — but every analysis subcommand is
exposed here.

Launch (typically spawned by the agent runner via ``MCPServerSpec``):

    python neuron_profiler/server.py
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
import analyze_neuron  # noqa: E402  (sys.path setup above)


def _capture(fn, **kwargs) -> str:
    ns = types.SimpleNamespace(**kwargs)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(ns)
    return buf.getvalue() or "(no output)"


def build_server() -> FastMCP:
    mcp = FastMCP("vibeserve-neuron-profiler")

    @mcp.tool()
    def capture(workload: str, out_dir: str = "/tmp/neuronprof", timeout: int = 1800) -> str:
        """Run a workload under ``neuron-explorer inspect`` and capture profiles.

        Args:
            workload: Shell command that launches/exercises the Neuron model
                (e.g. a benchmark driver against an already-running server).
            out_dir: Directory to write the NTFF/NEFF profiles into.
            timeout: Max seconds to let the workload run.
        """
        return _capture(
            analyze_neuron.cmd_capture, out_dir=out_dir, workload=workload, timeout=timeout
        )

    @mcp.tool()
    def summary(report: str) -> str:
        """High-level report (engine utilization, top ops, DMA) — start here.

        Args:
            report: The capture out-dir (or a file inside it).
        """
        return _capture(analyze_neuron.cmd_summary, report=report)

    @mcp.tool()
    def summary_json(report: str) -> str:
        """Machine-readable summary (``view --output-format summary-json``)."""
        return _capture(analyze_neuron.cmd_summary_json, report=report)

    @mcp.tool()
    def operators(report: str) -> str:
        """Per-operator / per-instruction breakdown from the device profile."""
        return _capture(analyze_neuron.cmd_operators, report=report)

    @mcp.tool()
    def dma(report: str) -> str:
        """Raw DMA trace (``show-session --show-dma``)."""
        return _capture(analyze_neuron.cmd_show, report=report, dma=True, trace=False)

    @mcp.tool()
    def view(report: str, output_format: str = "summary-text") -> str:
        """Run ``neuron-explorer view`` with an explicit output format.

        Args:
            report: The capture out-dir.
            output_format: One of db, summary-text, summary-json, json,
                perfetto, parquet.
        """
        return _capture(analyze_neuron.cmd_view, report=report, output_format=output_format)

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="vibeserve-neuron-mcp",
        description="Stdio MCP server exposing neuron-explorer profile analyses.",
    )
    parser.parse_args(argv)
    mcp = build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
