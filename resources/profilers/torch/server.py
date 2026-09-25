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
import threading
import types
from pathlib import Path

from mcp.server.fastmcp import FastMCP

_HERE = Path(__file__).resolve().parent

# capture_runtime import shim (see its own docstring): a checkout stages it
# as a sibling ``_common/``, a materialized agent workspace as a sibling
# ``profilers_common/``.
for _name in ("_common", "profilers_common"):
    _candidate = _HERE.parent / _name
    if (_candidate / "capture_runtime.py").is_file():
        sys.path.insert(0, str(_candidate))
        break
import capture_runtime  # noqa: E402
import mcp_async  # noqa: E402

sys.path.insert(0, str(_HERE))
import analyze_torch_profile  # noqa: E402
import capture_ops  # noqa: E402


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


def _resolve_report(report: str) -> str:
    """Resolve *report* as a literal path, or a ``profile_ops`` capture id.

    ``profile_ops`` returns a capture id (not a file path); every analysis
    tool below accepts either, so the agent can pass that id straight
    through without separately looking up the primary trace file.
    """
    if Path(report).is_file():
        return report
    try:
        capture_dir = capture_runtime.resolve(report)
    except FileNotFoundError:
        return report  # not a capture id either; let the loader raise a clear error
    primary = None
    with contextlib.suppress(FileNotFoundError, ValueError):
        primary = capture_runtime.load_manifest(capture_dir).get("primary_trace")
    if not primary:
        raise FileNotFoundError(  # noqa: TRY003  # tracked: #288
            f"capture {report!r} resolved to {capture_dir} but has no recorded primary trace "
            "(manifest.json missing 'primary_trace'); pass an explicit trace file path instead."
        )
    return str(capture_dir / primary)


def build_server() -> FastMCP:  # noqa: C901  # tracked: #288
    """Construct the FastMCP instance with torch-profiler analysis tools."""
    mcp = FastMCP("vibesys-torch-profiler")

    @mcp.tool()
    async def profile_ops(  # noqa: PLR0913  # tracked: #288
        command: str,
        cwd: str | None = None,
        env: dict | None = None,
        ready_command: str | None = None,
        ready_timeout_s: float = 600.0,
        load_command: str | None = None,
        setup_command: str | None = None,
        stop_signal: str = "SIGINT",
        grace_s: float = 120.0,
        timeout_s: float = 1800.0,
        delay_s: float = 0.0,
        duration_s: float | None = None,
        record_shapes: bool = True,  # noqa: FBT001, FBT002  # tracked: #288
    ) -> str:
        """Generic in-process torch.profiler capture of any candidate program.

        Runs `command` (and, if given, `load_command` against it once
        `ready_command` succeeds) with an in-process torch.profiler
        injection armed via PYTHONPATH/env: no engine-specific code, no
        HTTP profiler-endpoint contract required. The injection arms itself
        the moment the target process imports torch and shows a visible
        GPU, starts after `delay_s`, and stops after `duration_s` (or at
        process exit / SIGINT). Multi-process targets each write their own
        trace; sibling processes get a bounded grace window to finish
        exporting before this picks the one with the most GPU kernel events
        as primary and runs certify + a compact summary against it.

        Measured on real ROCm hardware: prof.start() itself takes ~2s to
        actually begin recording after the signal fires, and `duration_s`
        is measured from when the signal is *sent* -- pad short windows
        accordingly. Also, a background thread that was already running
        before the profiler started records GPU kernels fine but zero
        CPU-side ops (no record_shapes/gemm_shapes/roofline attribution for
        it); prefer `delay_s=0` so recording starts before the target
        spawns its own worker threads.

        Runs off the main event loop, so other tool calls stay responsive
        while this is in flight; a client that cancels the call stops the
        capture rather than leaving it running unsupervised. If another
        GPU-using capture is already running in this server process,
        returns "busy: ..." immediately instead of queuing.

        Args:
            command: Target command, run via bash -lc.
            cwd: Working directory for command/load_command.
            env: Extra environment variables for the target process.
            ready_command: Polled until it exits 0 before load_command runs
                (server-under-load capture). Omit for a bounded script that
                exits on its own.
            ready_timeout_s: Max seconds to wait for ready_command.
            load_command: Run once ready_command succeeds; command is
                stopped via stop_signal once this returns.
            setup_command: Runs to completion BEFORE command, outside the
                profiler injection entirely. Use it for anything (e.g.
                picking a free port) whose own output or child processes
                must not run under the profiled env: write the value to a
                file here, then have command read it back with `read -r
                VAR < file`, never `$(...)`. A nonzero exit here stops the
                capture immediately (status setup_failed) with no target
                ever started.
            stop_signal: Signal name to stop command with (default SIGINT).
            grace_s: Seconds to wait after stop_signal before escalating to
                SIGTERM/SIGKILL (ROCm's post-export hang can take minutes;
                size generously).
            timeout_s: Hard wall-clock budget for the whole capture. Set
                your MCP client's own tool-call timeout above this value:
                a capture commonly runs 10-25 minutes.
            delay_s: Seconds after arming before the profiler starts.
            duration_s: Seconds to profile before auto-stopping; omit to
                profile until process exit / SIGINT.
            record_shapes: Capture per-op input shapes. Required for
                gemm_shapes/roofline and for certify to pass.
        """
        cancel_event = threading.Event()
        try:
            return await mcp_async.run_cancellable(
                capture_ops.profile_ops,
                cancel_event=cancel_event,
                command=command,
                cwd=cwd,
                env=env,
                ready_command=ready_command,
                ready_timeout_s=ready_timeout_s,
                load_command=load_command,
                setup_command=setup_command,
                stop_signal=stop_signal,
                grace_s=grace_s,
                timeout_s=timeout_s,
                delay_s=delay_s,
                duration_s=duration_s,
                record_shapes=record_shapes,
            )
        except capture_runtime.CaptureBusyError as exc:
            return capture_runtime.format_busy(exc.active)

    @mcp.tool()
    def tables(report: str) -> str:
        """Overview of a captured prof.json (event categories + totals).

        Args:
            report: Path to the prof.json file, a raw trace, or a profile_ops capture id.
        """
        return _capture(analyze_torch_profile.cmd_tables, report=_resolve_report(report))

    @mcp.tool()
    def kernels(report: str, top: int = 15) -> str:
        """Top GPU kernels by self-CUDA time.

        Args:
            report: Path to the prof.json file, a raw trace, or a profile_ops capture id.
            top: Number of kernels to show (default 15).
        """
        return _capture(analyze_torch_profile.cmd_kernels, report=_resolve_report(report), top=top)

    @mcp.tool()
    def operators(report: str, top: int = 15) -> str:
        """Top operators (aten::*, torch::*) by self-CPU time.

        Args:
            report: Path to the prof.json file, a raw trace, or a profile_ops capture id.
            top: Number of operators to show (default 15).
        """
        return _capture(
            analyze_torch_profile.cmd_operators, report=_resolve_report(report), top=top
        )

    @mcp.tool()
    def cpu_overhead(report: str) -> str:
        """CPU vs GPU time ratio — detects launch-bound / compute-bound.

        Args:
            report: Path to the prof.json file, a raw trace, or a profile_ops capture id.
        """
        return _capture(analyze_torch_profile.cmd_cpu_overhead, report=_resolve_report(report))

    @mcp.tool()
    def memory(report: str) -> str:
        """Memory allocation / transfer events."""
        return _capture(analyze_torch_profile.cmd_memory, report=_resolve_report(report))

    @mcp.tool()
    def summary(report: str, top: int = 15) -> str:
        """All-in-one: certify (raw traces only) + overhead + kernels + operators + memory.

        Args:
            report: Path to prof.json, a raw *.pt.trace.json(.gz) trace, or a
                profile_ops capture id.
            top: Number of kernels/operators per section (default 15).
        """
        return _capture(analyze_torch_profile.cmd_summary, report=_resolve_report(report), top=top)

    @mcp.tool()
    def certify(trace: str) -> str:
        """Structural validity check on a raw trace before trusting it.

        Checks GPU kernel and cpu_op counts, record_shapes coverage, step/
        annotation markers, GPU busy fraction in the capture window, and
        whether kernels ran inside a hipGraphLaunch/cudaGraphLaunch replay
        (which degrades per-op attribution). Prints PASS/WARN/FAIL with a
        concrete re-capture instruction per failure.

        Args:
            trace: Path to a *.pt.trace.json(.gz) Kineto/Chrome trace, or a
                profile_ops capture id.
        """
        return _capture(analyze_torch_profile.cmd_certify, trace=_resolve_report(trace))

    @mcp.tool()
    def gemm_shapes(trace: str, top: int = 20, out: str | None = None) -> str:
        """Extract (M, N, K, dtype) GEMM demand from a raw trace, ranked by GPU time.

        Reads aten::mm/addmm/bmm/baddbmm/linear/matmul/_scaled_mm "Input Dims",
        weighted by call count and the GPU time of the kernel(s) each call
        launched (via cpu_op -> kernel correlation), deduplicated by shape.

        Args:
            trace: Path to a *.pt.trace.json(.gz) Kineto/Chrome trace, or a
                profile_ops capture id.
            top: Number of shapes to show (default 20).
            out: Optional path to also write the ranked shapes as JSON, e.g.
                as input to hipBLASLt/AITER GEMM tuning.
        """
        return _capture(
            analyze_torch_profile.cmd_gemm_shapes, trace=_resolve_report(trace), top=top, out=out
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
            trace: Path to a *.pt.trace.json(.gz) Kineto/Chrome trace, or a
                profile_ops capture id.
            device: Known device key (mi210, mi300x, mi300a, mi325x, mi355x,
                h100). Auto-detected from the trace's deviceProperties when
                omitted.
            peak_tflops: Explicit dense peak TFLOP/s; overrides device.
            peak_gbps: Explicit peak HBM bandwidth in GB/s; overrides device.
            top: Number of ops to show (default 20).
        """
        return _capture(
            analyze_torch_profile.cmd_roofline,
            trace=_resolve_report(trace),
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
