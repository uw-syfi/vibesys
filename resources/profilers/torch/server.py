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


def _capture(fn, **kwargs) -> str:  # noqa: ANN001, ANN003  # tracked: #288
    ns = types.SimpleNamespace(**kwargs)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(ns)
    except SystemExit as exc:  # certify/gemm_shapes/roofline reject a non-trace report this way
        return f"error: {exc}"
    out = buf.getvalue()
    return out or "(no output)"


def build_server() -> FastMCP:
    """Construct the FastMCP instance with torch-profiler analysis tools."""
    mcp = FastMCP("vibesys-torch-profiler")

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
        """All-in-one: certify (raw traces only) + overhead + kernels + operators + memory.

        Args:
            report: Path to prof.json, or a raw *.pt.trace.json(.gz) trace.
            top: Number of kernels/operators per section (default 15).
        """
        return _capture(analyze_torch_profile.cmd_summary, report=report, top=top)

    @mcp.tool()
    def certify(trace: str) -> str:
        """Structural validity check on a raw trace before trusting it.

        Checks GPU kernel and cpu_op counts, record_shapes coverage, step/
        annotation markers, GPU busy fraction in the capture window, and
        whether kernels ran inside a hipGraphLaunch/cudaGraphLaunch replay
        (which degrades per-op attribution). Prints PASS/WARN/FAIL with a
        concrete re-capture instruction per failure.

        Args:
            trace: Path to a *.pt.trace.json(.gz) Kineto/Chrome trace.
        """
        return _capture(analyze_torch_profile.cmd_certify, trace=trace)

    @mcp.tool()
    def gemm_shapes(trace: str, top: int = 20, out: str | None = None) -> str:
        """Extract (M, N, K, dtype) GEMM demand from a raw trace, ranked by GPU time.

        Reads aten::mm/addmm/bmm/baddbmm/linear/matmul/_scaled_mm "Input Dims",
        weighted by call count and the GPU time of the kernel(s) each call
        launched (via cpu_op -> kernel correlation), deduplicated by shape.

        Args:
            trace: Path to a *.pt.trace.json(.gz) Kineto/Chrome trace.
            top: Number of shapes to show (default 20).
            out: Optional path to also write the ranked shapes as JSON, e.g.
                as input to hipBLASLt/AITER GEMM tuning.
        """
        return _capture(analyze_torch_profile.cmd_gemm_shapes, trace=trace, top=top, out=out)

    @mcp.tool()
    def roofline(
        trace: str,
        device: str | None = None,
        peak_tflops: float | None = None,
        peak_gbps: float | None = None,
        top: int = 20,
    ) -> str:
        """Achieved TFLOP/s, GB/s, arithmetic intensity, and bound class per GEMM/attention op.

        Args:
            trace: Path to a *.pt.trace.json(.gz) Kineto/Chrome trace.
            device: Known device key (mi210, mi300x, mi300a, mi325x, mi355x,
                h100). Auto-detected from the trace's deviceProperties when
                omitted.
            peak_tflops: Explicit dense peak TFLOP/s; overrides device.
            peak_gbps: Explicit peak HBM bandwidth in GB/s; overrides device.
            top: Number of ops to show (default 20).
        """
        return _capture(
            analyze_torch_profile.cmd_roofline,
            trace=trace,
            device=device,
            peak_tflops=peak_tflops,
            peak_gbps=peak_gbps,
            top=top,
        )

    return mcp


def main(argv: list[str] | None = None) -> None:  # noqa: D103  # tracked: #288
    parser = argparse.ArgumentParser(
        prog="vibesys-torch-mcp",
        description="Stdio MCP server exposing torch.profiler analyses.",
    )
    parser.parse_args(argv)
    mcp = build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
