"""Stdio MCP server exposing the rocprof analysis toolkit as MCP tools.

The orchestrator's profiler agent calls these tools rather than shelling
out to ``python analyze_rocprof.py …`` / ``counters.py …`` / etc. Long-running
capture (rocprofv3 itself, and ``compute.py profile``) stays a shell
command — it drives a live server under load, which is too long-running
for stdio MCP.

Launch (typically spawned by the agent runner via ``MCPServerSpec``):

    python rocprof_profiler/server.py
    # or, equivalently:
    uv run python rocprof_profiler/server.py
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

# Import the analysis modules by path so this file is usable both from
# inside the workspace (``rocprof_profiler/server.py``) and as a host-side
# helper. None of these modules import each other, so import order is
# irrelevant.
sys.path.insert(0, str(_HERE))
import analyze_rocprof  # noqa: E402  (sys.path setup above)
import att  # noqa: E402
import compute  # noqa: E402
import counters  # noqa: E402
import kernel_bench  # noqa: E402


def _capture(fn, **kwargs) -> str:  # noqa: ANN001, ANN003  # tracked: #288
    """Run a ``cmd_*`` with an argparse-like namespace and capture stdout.

    The ``cmd_*`` helpers print their results to stdout; we intercept and
    return the buffered text so the MCP client gets a structured reply.
    Several ``cmd_*`` functions reject bad input via ``sys.exit(message)``
    rather than an exception — turn that into an ``error: …`` string
    instead of letting it kill the server process.
    """
    ns = types.SimpleNamespace(**kwargs)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(ns)
    except SystemExit as exc:
        return f"error: {exc}"
    out = buf.getvalue()
    return out or "(no output)"


def build_server() -> FastMCP:  # noqa: C901  # tracked: #288
    """Construct the FastMCP instance with rocprof analysis tools.

    Exposed separately so unit tests can introspect registered tools
    without spawning a stdio loop.
    """
    mcp = FastMCP("vibesys-rocprof-profiler")

    # -- analyze_rocprof.py: rocprofv3 system trace ------------------------

    @mcp.tool()
    def files(report: str) -> str:
        """Discover rocprofv3 output files, process captures, agents, row counts.

        Args:
            report: rocprofv3 output directory, or a specific trace/stats/json/db file.
        """
        return _capture(analyze_rocprof.cmd_files, report=report)

    @mcp.tool()
    def kernels(report: str, top: int = 15) -> str:
        """Top GPU kernels by total execution time, with a library-family column.

        Args:
            report: rocprofv3 output directory, or a specific trace/stats/json/db file.
            top: Number of kernels to show (default 15).
        """
        return _capture(analyze_rocprof.cmd_kernels, report=report, top=top)

    @mcp.tool()
    def families(report: str) -> str:
        """GPU time grouped by kernel library family (e.g. hipBLASLt, AITER, Triton)."""
        return _capture(analyze_rocprof.cmd_families, report=report)

    @mcp.tool()
    def idle_gaps(report: str, top: int = 10) -> str:
        """GPU busy vs idle, largest idle gaps between kernel launches.

        Args:
            report: rocprofv3 output directory, or a specific trace/stats/json/db file.
            top: Number of gaps to show (default 10).
        """
        return _capture(analyze_rocprof.cmd_idle_gaps, report=report, top=top)

    @mcp.tool()
    def cpu_overhead(report: str) -> str:
        """HIP API launch overhead and launch-bound heuristic."""
        return _capture(analyze_rocprof.cmd_cpu_overhead, report=report)

    @mcp.tool()
    def memory(report: str) -> str:
        """Memory copies by direction, bytes, bandwidth."""
        return _capture(analyze_rocprof.cmd_memory, report=report)

    @mcp.tool()
    def graphs(report: str) -> str:
        """HIP graph launches and attribution-degradation check."""
        return _capture(analyze_rocprof.cmd_graphs, report=report)

    @mcp.tool()
    def host_idle(report: str) -> str:
        """Detect a mostly host-idle / load-missed capture (validity check)."""
        return _capture(analyze_rocprof.cmd_host_idle, report=report)

    @mcp.tool()
    def query(report: str, sql: str) -> str:
        """Run arbitrary SQL against a rocpd SQLite export, if present.

        Args:
            report: rocprofv3 output directory, or a specific trace/stats/json/db file.
            sql: SQL statement to execute.
        """
        return _capture(analyze_rocprof.cmd_query, report=report, sql=sql)

    @mcp.tool()
    def summary(report: str, top: int = 15) -> str:
        """All-in-one analysis: kernels + families + idle_gaps + cpu_overhead + memory + graphs."""
        return _capture(analyze_rocprof.cmd_summary, report=report, top=top)

    # -- counters.py: PMC counter sets, report aggregation, triage ---------

    @mcp.tool()
    def counter_sets(arch: str | None = None) -> str:
        """Print the PMC counter-set catalogue for one architecture, or all of them.

        Args:
            arch: e.g. gfx90a, mi210, gfx942, mi300x, gfx950, mi355x. Omit for every family.
        """
        return _capture(counters.cmd_list_sets, arch=arch)

    @mcp.tool()
    def counter_plan(
        arch: str,
        sets: str,
        kernel: str | None = None,
        out_dir: str = "rocprof_pmc",
        command: list[str] | None = None,
    ) -> str:
        """Print one rocprofv3 --pmc command line per requested counter set.

        Each counter set gets its own process invocation and its own output
        directory — never combine sets into one --pmc call, and never reuse
        an output directory across passes. At most 4 counters per pass.

        Args:
            arch: e.g. gfx90a, mi210, gfx942, mi300x, gfx950, mi355x.
            sets: Comma-separated set names, e.g. "mfma,l2,hbm".
            kernel: Optional rocprofv3 --kernel-include-regex value.
            out_dir: Base output directory for the planned passes (default "rocprof_pmc").
            command: The program to profile, as argv tokens (without a leading "--").
        """
        return _capture(
            counters.cmd_plan,
            arch=arch,
            sets=sets,
            kernel=kernel,
            out_dir=out_dir,
            command=list(command or ()),
        )

    @mcp.tool()
    def counter_report(
        dirs: list[str],
        kernel: str | None = None,
        top: int = 15,
        arch: str | None = None,
    ) -> str:
        """Merge PMC passes per kernel and print resource usage + derived metrics.

        Args:
            dirs: One or more PMC pass output directories.
            kernel: Optional regex filter on Kernel_Name.
            top: Number of kernels to show (default 15).
            arch: For the occupancy/LDS-size model; defaults to gfx942.
        """
        return _capture(counters.cmd_report, dirs=dirs, kernel=kernel, top=top, arch=arch)

    @mcp.tool()
    def counter_triage(
        dirs: list[str],
        arch: str,
        kernel: str | None = None,
        top: int = 15,
    ) -> str:
        """Classify each hot kernel's bottleneck (occupancy/bandwidth/compute/LDS/launch/latency).

        Args:
            dirs: One or more PMC pass output directories.
            arch: e.g. gfx90a, mi210, gfx942, mi300x, gfx950, mi355x.
            kernel: Optional regex filter on Kernel_Name.
            top: Number of kernels to show (default 15).
        """
        return _capture(counters.cmd_triage, dirs=dirs, arch=arch, kernel=kernel, top=top)

    # -- att.py: ATT capture planning + hotspot analysis --------------------

    @mcp.tool()
    def att_plan(  # noqa: PLR0913  # tracked: #288
        arch: str,
        kernel: str,
        target_cu: int = att.DEFAULT_TARGET_CU,
        buffer_size: int = att.DEFAULT_BUFFER_SIZE,
        se_mask: str = att.DEFAULT_SE_MASK,
        simd_select: str = att.DEFAULT_SIMD_SELECT,
        iteration_range: list[str] | None = None,
        out_dir: str = "rocprof_att",
        decoder_lib_dir: str | None = None,
        script_path: str = "rocprof_att.sh",
        write: bool = False,  # noqa: FBT001, FBT002  # tracked: #288
        command: list[str] | None = None,
    ) -> str:
        """Print a rocprofv3 Advanced Thread Trace (ATT) command line and prerequisites.

        Args:
            arch: e.g. gfx90a, mi210, gfx942, mi300x, gfx950, mi355x.
            kernel: kernel_include_regex value selecting which kernel to trace.
            target_cu: Which compute unit to trace (default 1; keeps output small).
            buffer_size: Plain decimal ATT buffer-size byte count (raise if the decoded trace
                reports truncation; unit-suffixed strings like "64MB" are rejected).
            se_mask: Shader-engine mask (hex or decimal).
            simd_select: SIMD select mask (hex or decimal).
            iteration_range: Values passed through to --kernel-iteration-range, if any.
            out_dir: Directory ATT output lands under.
            decoder_lib_dir: Directory containing librocprof-trace-decoder.so
                (--att-library-path). Without it the printed command uses a placeholder.
            script_path: Path for the printed command when write=True.
            write: Also write the command to script_path (default False; print only).
            command: The program to profile, as argv tokens (without a leading "--").
        """
        return _capture(
            att.cmd_plan,
            arch=arch,
            kernel=kernel,
            target_cu=target_cu,
            buffer_size=buffer_size,
            se_mask=se_mask,
            simd_select=simd_select,
            iteration_range=iteration_range,
            out_dir=out_dir,
            decoder_lib_dir=decoder_lib_dir,
            script_path=script_path,
            write=write,
            command=list(command or ()),
        )

    @mcp.tool()
    def att_hotspots(dispatch_dir: str, top: int = 15) -> str:
        """Top stall hotspots (instructions + source lines) from a decoded ATT dispatch dir.

        Args:
            dispatch_dir: A ``ui_output_agent_<PID>_dispatch_<N>`` directory (or its parent).
            top: Number of hotspots to show (default 15).
        """
        return _capture(att.cmd_hotspots, dispatch_dir=dispatch_dir, top=top)

    # -- compute.py: rocprof-compute doctor + analyze ------------------------
    # `compute.py profile` is long-running (drives the workload under a hard
    # timeout) and stays shell-only, like rocprofv3 capture itself.

    @mcp.tool()
    def compute_doctor() -> str:
        """Diagnose the rocprof-compute install and print actionable fixes."""
        return _capture(compute.cmd_doctor)

    @mcp.tool()
    def compute_analyze(
        workload_dir: str,
        blocks: str = ",".join(compute.DEFAULT_ANALYZE_BLOCKS),
        max_stat: int = compute.DEFAULT_MAX_STAT,
        kernel: str = "",
        timeout: float = compute.DEFAULT_ANALYZE_TIMEOUT,
    ) -> str:
        """Run `rocprof-compute analyze`; falls back to raw CSVs if it fails.

        Args:
            workload_dir: A workload directory produced by `compute.py profile`.
            blocks: Comma-separated rocprof-compute analysis block IDs.
            max_stat: Max rows/stats to print per block.
            kernel: Optional regex filtering kernels (-k).
            timeout: Hard timeout in seconds for the analyze subprocess.
        """
        return _capture(
            compute.cmd_analyze,
            workload_dir=workload_dir,
            blocks=blocks,
            max_stat=max_stat,
            kernel=kernel,
            timeout=timeout,
        )

    # -- kernel_bench.py: microbenchmark + paired A/B toolkit ---------------

    @mcp.tool()
    def bench_parse(log: str) -> str:
        """Extract `wall_ms: <float>` lines from a driver log into a JSON `{"wall_ms": [...]}`.

        Args:
            log: Path to the driver log.
        """
        return _capture(kernel_bench.cmd_parse, log=log)

    @mcp.tool()
    def bench_compare(
        file_a: str,
        file_b: str,
        threshold_pct: float = kernel_bench.DEFAULT_THRESHOLD_PCT,
    ) -> str:
        """Unpaired median comparison of two recorded (non-interleaved) sample files.

        Args:
            file_a: Path to the first recorded ``{"wall_ms": [...]}`` samples file.
            file_b: Path to the second recorded samples file.
            threshold_pct: Minimum median delta (%) to call the result decisive.
        """
        return _capture(
            kernel_bench.cmd_compare,
            file_a=file_a,
            file_b=file_b,
            threshold_pct=threshold_pct,
        )

    @mcp.tool()
    def bench_verdict(
        samples: str,
        threshold_pct: float = kernel_bench.DEFAULT_THRESHOLD_PCT,
    ) -> str:
        """Paired A/B verdict from a file of genuinely interleaved recorded pairs.

        Args:
            samples: Path to a recorded ``{"pairs": [[baseline_ms, candidate_ms], ...]}`` file.
            threshold_pct: Minimum median delta (%) to call the result decisive.
        """
        return _capture(kernel_bench.cmd_verdict, samples=samples, threshold_pct=threshold_pct)

    return mcp


def main(argv: list[str] | None = None) -> None:  # noqa: D103  # tracked: #288
    parser = argparse.ArgumentParser(
        prog="vibesys-rocprof-mcp",
        description="Stdio MCP server exposing the rocprof analysis toolkit.",
    )
    parser.parse_args(argv)
    mcp = build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
