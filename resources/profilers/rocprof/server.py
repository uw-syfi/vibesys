"""Stdio MCP server exposing the rocprof capture + analysis toolkit as MCP tools.

An agent with only MCP access must be able to profile anything of interest
(a server under its own load, an offline script, a microbenchmark) on an AMD
GPU. The curated tool surface below is organized by altitude, from whole-run
system trace down to instruction-level stalls (see ``capture.py``'s
docstring): call ``profiling_capabilities()`` first, then one ``profile_*``
capture tool, then its drill-downs (or ``summary``/``compare``, which
dispatch on the capture's recorded kind). Nothing here knows about any
specific serving engine: every ``profile_*`` tool's lifecycle args
(``command``, ``env``, ``ready_command``, ``load_command``, ...) are opaque,
agent-supplied values.

Launch (typically spawned by the agent runner via ``MCPServerSpec``):

    python rocprof_profiler/server.py
    # or, equivalently:
    uv run python rocprof_profiler/server.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

_HERE = Path(__file__).resolve().parent

# capture_runtime import shim (see its own docstring): a checkout stages it
# as a sibling ``_common/``, a materialized agent workspace as a sibling
# ``profilers_common/``.
for _common_name in ("_common", "profilers_common"):
    _common_candidate = _HERE.parent / _common_name
    if (_common_candidate / "capture_runtime.py").is_file():
        if str(_common_candidate) not in sys.path:
            sys.path.insert(0, str(_common_candidate))
        break
import capture_runtime  # noqa: E402

# Import the analysis + capture modules by path so this file is usable both
# from inside the workspace (``rocprof_profiler/server.py``) and as a
# host-side helper. None of these modules import each other except through
# ``capture``, so import order otherwise doesn't matter.
sys.path.insert(0, str(_HERE))
import analyze_rocprof  # noqa: E402
import att  # noqa: E402
import capture  # noqa: E402
import compute  # noqa: E402
import counters  # noqa: E402
import kernel_bench  # noqa: E402

_capture_cli = capture.run_cli


def build_server() -> FastMCP:  # noqa: C901, PLR0915  # tracked: #288
    """Construct the FastMCP instance with the curated rocprof tool set.

    Exposed separately so unit tests can introspect registered tools
    without spawning a stdio loop.
    """
    mcp = FastMCP("vibesys-rocprof-profiler")

    # -- capabilities ---------------------------------------------------

    @mcp.tool()
    def profiling_capabilities() -> str:
        """What this host actually supports, and which tool each line gates.

        Call this first, every round.
        """
        return capture.profiling_capabilities()

    # -- profile_*: capture tools, one per altitude ----------------------

    @mcp.tool()
    def profile_timeline(  # noqa: PLR0913  # tracked: #288
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ready_command: str | None = None,
        ready_timeout_s: float = 60.0,
        load_command: str | None = None,
        stop_signal: str = "SIGINT",
        grace_s: float = 10.0,
        timeout_s: float = 300.0,
        hip_api: bool = False,
        kernel_include: str | None = None,
        collection_delay_s: float | None = None,
        collection_duration_s: float | None = None,
    ) -> str:
        """Capture a whole-run rocprofv3 system trace: host/device overlap, idle, launch cost.

        Runs the target through the shared capture lifecycle (start, wait
        for ``ready_command`` if given, run ``load_command`` if given, stop
        with ``stop_signal`` after ``grace_s``), then automatically checks
        validity (``host_idle``) and prints a summary. Args:

            command: Command run via ``bash -lc`` (offline script, or a
                server when ``load_command`` is also given).
            cwd: Working directory for command/load_command.
            env: Extra environment variables for the target process.
            ready_command: Polled until it exits 0 before load_command runs.
                Omit for a bounded script that exits on its own.
            ready_timeout_s: Max seconds to wait for ready_command.
            load_command: Run once ready_command succeeds; command is
                stopped via stop_signal once this returns.
            stop_signal: Signal name to stop command with (default SIGINT).
            grace_s: Seconds to wait after stop_signal before escalating
                (rocprofv3 only flushes traces on a clean exit; size this
                generously for a large capture).
            timeout_s: Hard wall-clock budget for the whole capture.
            hip_api: Also collect --hip-runtime-trace (needed for
                cpu_overhead/graphs; 2-4x the kernel-trace volume).
            kernel_include: Optional --kernel-include-regex filter.
            collection_delay_s: Seconds to skip before collecting (must be
                given together with collection_duration_s).
            collection_duration_s: Seconds to collect after the delay (must
                be given together with collection_delay_s).
        """
        lifecycle = capture_runtime.Lifecycle(
            command=command,
            cwd=cwd,
            env=dict(env or {}),
            ready_command=ready_command,
            ready_timeout_s=ready_timeout_s,
            load_command=load_command,
            stop_signal=stop_signal,
            grace_s=grace_s,
            timeout_s=timeout_s,
        )
        try:
            return capture.profile_timeline(
                lifecycle,
                hip_api=hip_api,
                kernel_include=kernel_include,
                collection_delay_s=collection_delay_s,
                collection_duration_s=collection_duration_s,
            )
        except ValueError as exc:
            return f"error: {exc}"

    @mcp.tool()
    def profile_counters(  # noqa: PLR0913  # tracked: #288
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ready_command: str | None = None,
        ready_timeout_s: float = 60.0,
        load_command: str | None = None,
        stop_signal: str = "SIGINT",
        grace_s: float = 10.0,
        timeout_s: float = 300.0,
        sets: list[str],
        kernel: str | None = None,
    ) -> str:
        """Capture PMC hardware counters: which hardware ceiling one hot kernel is against.

        Runs ONE rocprofv3 --pmc pass PER requested set -- the profiled
        workload is re-run in full once per set (<=4 counters per pass is a
        hard rocprofv3/CDNA constraint). Requires a detectable GPU
        architecture (see profiling_capabilities). Target one
        already-identified hot kernel via `kernel`; this is not a whole-run
        tool. Args:

            command, cwd, env, ready_command, ready_timeout_s, load_command,
                stop_signal, grace_s, timeout_s: same as profile_timeline.
            sets: Counter-set names from the catalogue for the detected
                architecture (see profiling_capabilities), e.g.
                ["mfma", "l2", "hbm"].
            kernel: Optional --kernel-include-regex filter, applied to every pass.
        """
        lifecycle = capture_runtime.Lifecycle(
            command=command,
            cwd=cwd,
            env=dict(env or {}),
            ready_command=ready_command,
            ready_timeout_s=ready_timeout_s,
            load_command=load_command,
            stop_signal=stop_signal,
            grace_s=grace_s,
            timeout_s=timeout_s,
        )
        try:
            return capture.profile_counters(lifecycle, sets=sets, kernel=kernel)
        except ValueError as exc:
            return f"error: {exc}"

    @mcp.tool()
    def profile_kernel_deep(  # noqa: PLR0913  # tracked: #288
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ready_command: str | None = None,
        ready_timeout_s: float = 60.0,
        load_command: str | None = None,
        stop_signal: str = "SIGINT",
        grace_s: float = 10.0,
        timeout_s: float = 300.0,
        kernel: str,
        dispatch: int | None = None,
    ) -> str:
        """Capture full Speed-of-Light + roofline for one targeted kernel (rocprof-compute).

        Costs one full counter-collection sweep (minutes, not seconds). If
        rocprof-compute isn't usable on this host, returns a clear error
        instead of attempting a capture -- check profiling_capabilities
        first. Args:

            command, cwd, env, ready_command, ready_timeout_s, load_command,
                stop_signal, grace_s, timeout_s: same as profile_timeline.
            kernel: A literal substring of the real kernel name (torch GEMMs
                dispatch as Tensile kernels like 'Cijk_Ailk_Bljk_...', not
                anything containing "gemm"); list real names with
                profile_timeline + kernels first.
            dispatch: Optional specific dispatch index to target.
        """
        lifecycle = capture_runtime.Lifecycle(
            command=command,
            cwd=cwd,
            env=dict(env or {}),
            ready_command=ready_command,
            ready_timeout_s=ready_timeout_s,
            load_command=load_command,
            stop_signal=stop_signal,
            grace_s=grace_s,
            timeout_s=timeout_s,
        )
        try:
            return capture.profile_kernel_deep(lifecycle, kernel=kernel, dispatch=dispatch)
        except ValueError as exc:
            return f"error: {exc}"

    @mcp.tool()
    def profile_instructions(  # noqa: PLR0913  # tracked: #288
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ready_command: str | None = None,
        ready_timeout_s: float = 60.0,
        load_command: str | None = None,
        stop_signal: str = "SIGINT",
        grace_s: float = 10.0,
        timeout_s: float = 300.0,
        kernel: str,
        target_cu: int = att.DEFAULT_TARGET_CU,
        buffer_bytes: int = att.DEFAULT_BUFFER_SIZE,
    ) -> str:
        """Capture per-instruction stalls inside one kernel, one compute unit (ATT).

        Requires rocprofv3 >= 7.1 and the separate rocprof-trace-decoder
        library (see profiling_capabilities); costs the most overhead of
        any capture tool here -- only reach for it after counters already
        point at a specific stall class. Args:

            command, cwd, env, ready_command, ready_timeout_s, load_command,
                stop_signal, grace_s, timeout_s: same as profile_timeline.
            kernel: --kernel-include-regex value selecting the kernel to trace.
            target_cu: Which compute unit to trace (keeps output small).
            buffer_bytes: ATT buffer size in bytes; raise if the decoded
                trace reports truncation.
        """
        lifecycle = capture_runtime.Lifecycle(
            command=command,
            cwd=cwd,
            env=dict(env or {}),
            ready_command=ready_command,
            ready_timeout_s=ready_timeout_s,
            load_command=load_command,
            stop_signal=stop_signal,
            grace_s=grace_s,
            timeout_s=timeout_s,
        )
        try:
            return capture.profile_instructions(
                lifecycle, kernel=kernel, target_cu=target_cu, buffer_bytes=buffer_bytes
            )
        except ValueError as exc:
            return f"error: {exc}"

    @mcp.tool()
    def profile_ops(  # noqa: PLR0913  # tracked: #288
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ready_command: str | None = None,
        ready_timeout_s: float = 600.0,
        load_command: str | None = None,
        stop_signal: str = "SIGINT",
        grace_s: float = 120.0,
        timeout_s: float = 1800.0,
        delay_s: float = 0.0,
        duration_s: float | None = None,
        record_shapes: bool = True,  # noqa: FBT001, FBT002  # tracked: #288
    ) -> str:
        """Capture an in-process torch.profiler trace of any candidate program.

        Delegates to the torch plugin's capture_ops.profile_ops, staged
        alongside rocprof; returns a clear error if that plugin isn't
        staged. Args:

            command, cwd, env, ready_command, ready_timeout_s, load_command,
                stop_signal, grace_s, timeout_s: same as profile_timeline
                (grace/timeout are more generous here: the in-process trace
                write can itself take a while).
            delay_s: Seconds after arming before the profiler starts.
            duration_s: Seconds to profile before auto-stopping; omit to
                profile until process exit / stop_signal.
            record_shapes: Capture per-op input shapes (needed for
                gemm_shapes/roofline and for certify to pass).
        """
        return capture.profile_ops(
            command=command,
            cwd=cwd,
            env=env,
            ready_command=ready_command,
            ready_timeout_s=ready_timeout_s,
            load_command=load_command,
            stop_signal=stop_signal,
            grace_s=grace_s,
            timeout_s=timeout_s,
            delay_s=delay_s,
            duration_s=duration_s,
            record_shapes=record_shapes,
        )

    # -- capture store: list / summarize / diff --------------------------

    @mcp.tool()
    def captures(limit: int = 10) -> str:
        """List recent captures: id, kind, status, and the profiled command's head.

        Args:
            limit: Maximum number of captures to show, newest first.
        """
        return capture.captures(limit=limit)

    @mcp.tool()
    def summary(capture_id: str) -> str:
        """All-in-one analysis of one capture, dispatched by its recorded kind.

        Args:
            capture_id: A capture id (from a profile_* tool or captures()),
                or an explicit capture directory path.
        """
        return capture.summary(capture_id)

    @mcp.tool()
    def compare(a: str, b: str) -> str:
        """Diff two captures of the same kind: top kernel/family deltas, or counter-metric deltas.

        Args:
            a: The baseline capture id or path.
            b: The candidate capture id or path; every delta is b - a.
        """
        return capture.compare(a, b)

    # -- analyze_rocprof.py: rocprofv3 system-trace drill-downs ----------

    @mcp.tool()
    def files(report: str) -> str:
        """Discover rocprofv3 output files, process captures, agents, row counts.

        Args:
            report: A timeline capture id, or an explicit directory/file path.
        """
        return _capture_cli(analyze_rocprof.cmd_files, report=capture.resolve_report_arg(report))

    @mcp.tool()
    def kernels(report: str, top: int = 15) -> str:
        """Top GPU kernels by total execution time, with a library-family column.

        Args:
            report: A timeline capture id, or an explicit directory/file path.
            top: Number of kernels to show (default 15).
        """
        return _capture_cli(
            analyze_rocprof.cmd_kernels, report=capture.resolve_report_arg(report), top=top
        )

    @mcp.tool()
    def families(report: str) -> str:
        """GPU time grouped by kernel library family (e.g. hipBLASLt, AITER, Triton).

        Args:
            report: A timeline capture id, or an explicit directory/file path.
        """
        return _capture_cli(analyze_rocprof.cmd_families, report=capture.resolve_report_arg(report))

    @mcp.tool()
    def idle_gaps(report: str, top: int = 10) -> str:
        """GPU busy vs idle, largest idle gaps between kernel launches.

        Args:
            report: A timeline capture id, or an explicit directory/file path.
            top: Number of gaps to show (default 10).
        """
        return _capture_cli(
            analyze_rocprof.cmd_idle_gaps, report=capture.resolve_report_arg(report), top=top
        )

    @mcp.tool()
    def cpu_overhead(report: str) -> str:
        """HIP API launch overhead and launch-bound heuristic.

        Args:
            report: A timeline capture id, or an explicit directory/file path.
        """
        return _capture_cli(
            analyze_rocprof.cmd_cpu_overhead, report=capture.resolve_report_arg(report)
        )

    @mcp.tool()
    def memory(report: str) -> str:
        """Memory copies by direction, bytes, bandwidth.

        Args:
            report: A timeline capture id, or an explicit directory/file path.
        """
        return _capture_cli(analyze_rocprof.cmd_memory, report=capture.resolve_report_arg(report))

    @mcp.tool()
    def graphs(report: str) -> str:
        """HIP graph launches and attribution-degradation check.

        Args:
            report: A timeline capture id, or an explicit directory/file path.
        """
        return _capture_cli(analyze_rocprof.cmd_graphs, report=capture.resolve_report_arg(report))

    @mcp.tool()
    def host_idle(report: str) -> str:
        """Detect a mostly host-idle / load-missed capture (validity check).

        Args:
            report: A timeline capture id, or an explicit directory/file path.
        """
        return _capture_cli(
            analyze_rocprof.cmd_host_idle, report=capture.resolve_report_arg(report)
        )

    @mcp.tool()
    def query(report: str, sql: str) -> str:
        """Run arbitrary SQL against a rocpd SQLite export, if present.

        Args:
            report: A timeline capture id, or an explicit directory/file path.
            sql: SQL statement to execute.
        """
        return _capture_cli(
            analyze_rocprof.cmd_query, report=capture.resolve_report_arg(report), sql=sql
        )

    # -- counters.py: PMC counter drill-downs -----------------------------

    @mcp.tool()
    def counter_report(
        dirs: list[str], kernel: str | None = None, top: int = 15, arch: str | None = None
    ) -> str:
        """Merge PMC passes per kernel and print resource usage + derived metrics.

        Args:
            dirs: A profile_counters capture id (expands to every pass it
                bundled), or one or more explicit PMC pass output directories.
            kernel: Optional regex filter on Kernel_Name.
            top: Number of kernels to show (default 15).
            arch: For the occupancy/LDS-size model; defaults to gfx942.
        """
        return _capture_cli(
            counters.cmd_report,
            dirs=capture.resolve_counter_dirs(dirs),
            kernel=kernel,
            top=top,
            arch=arch,
        )

    @mcp.tool()
    def counter_triage(dirs: list[str], arch: str, kernel: str | None = None, top: int = 15) -> str:
        """Classify each hot kernel's bottleneck (occupancy/bandwidth/compute/LDS/launch/latency).

        Args:
            dirs: A profile_counters capture id (expands to every pass it
                bundled), or one or more explicit PMC pass output directories.
            arch: e.g. gfx90a, mi210, gfx942, mi300x, gfx950, mi355x.
            kernel: Optional regex filter on Kernel_Name.
            top: Number of kernels to show (default 15).
        """
        return _capture_cli(
            counters.cmd_triage,
            dirs=capture.resolve_counter_dirs(dirs),
            arch=arch,
            kernel=kernel,
            top=top,
        )

    # -- att.py: Advanced Thread Trace drill-down -------------------------

    @mcp.tool()
    def att_hotspots(dispatch_dir: str, top: int = 15) -> str:
        """Top stall hotspots (instructions + source lines) from a decoded ATT dispatch dir.

        Args:
            dispatch_dir: An instructions capture id, or a
                ``ui_output_agent_<PID>_dispatch_<N>`` directory (or its parent).
            top: Number of hotspots to show (default 15).
        """
        return _capture_cli(
            att.cmd_hotspots, dispatch_dir=capture.resolve_report_arg(dispatch_dir), top=top
        )

    # -- compute.py: rocprof-compute analyze drill-down --------------------

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
            workload_dir: A kernel_deep capture id, or an explicit workload
                directory produced by `rocprof-compute profile`.
            blocks: Comma-separated rocprof-compute analysis block IDs.
            max_stat: Max rows/stats to print per block.
            kernel: Optional regex filtering kernels (-k).
            timeout: Hard timeout in seconds for the analyze subprocess.
        """
        return _capture_cli(
            compute.cmd_analyze,
            workload_dir=capture.resolve_kernel_deep_workload_arg(workload_dir),
            blocks=blocks,
            max_stat=max_stat,
            kernel=kernel,
            timeout=timeout,
        )

    # -- analyze_torch_profile.py: cross-check torch-side analysis --------

    torch_analyzer = capture.import_torch_sibling("analyze_torch_profile")

    if torch_analyzer is not None:

        @mcp.tool()
        def certify(trace: str) -> str:
            """Structural validity check on a raw torch trace before trusting it.

            Args:
                trace: A profile_ops capture id, or a *.pt.trace.json(.gz) path.
            """
            return _capture_cli(
                torch_analyzer.cmd_certify, trace=capture.resolve_ops_trace_arg(trace)
            )

        @mcp.tool()
        def gemm_shapes(trace: str, top: int = 20, out: str | None = None) -> str:
            """Extract (M, N, K, dtype) GEMM demand from a raw torch trace, ranked by GPU time.

            Args:
                trace: A profile_ops capture id, or a *.pt.trace.json(.gz) path.
                top: Number of shapes to show (default 20).
                out: Optional path to also write the ranked shapes as JSON.
            """
            return _capture_cli(
                torch_analyzer.cmd_gemm_shapes,
                trace=capture.resolve_ops_trace_arg(trace),
                top=top,
                out=out,
            )

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
                trace: A profile_ops capture id, or a *.pt.trace.json(.gz) path.
                device: Known device key (mi210, mi300x, mi300a, mi325x, mi355x, h100).
                peak_tflops: Explicit dense peak TFLOP/s; overrides device.
                peak_gbps: Explicit peak HBM bandwidth in GB/s; overrides device.
                top: Number of ops to show (default 20).
            """
            return _capture_cli(
                torch_analyzer.cmd_roofline,
                trace=capture.resolve_ops_trace_arg(trace),
                device=device,
                peak_tflops=peak_tflops,
                peak_gbps=peak_gbps,
                top=top,
            )

    # -- kernel_bench.py: microbenchmark + paired A/B toolkit -------------

    @mcp.tool()
    def bench_parse(log: str) -> str:
        """Extract `wall_ms: <float>` lines from a driver log into a JSON `{"wall_ms": [...]}`.

        Args:
            log: Path to the driver log.
        """
        return _capture_cli(kernel_bench.cmd_parse, log=log)

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
        return _capture_cli(
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
        return _capture_cli(kernel_bench.cmd_verdict, samples=samples, threshold_pct=threshold_pct)

    return mcp


def main(argv: list[str] | None = None) -> None:  # noqa: D103  # tracked: #288
    parser = argparse.ArgumentParser(
        prog="vibesys-rocprof-mcp",
        description="Stdio MCP server exposing the rocprof capture + analysis toolkit.",
    )
    parser.parse_args(argv)
    mcp = build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
