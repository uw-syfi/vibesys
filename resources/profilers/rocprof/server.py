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
import atexit
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import ModuleType

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
import capture_runtime  # noqa: E402  # LW-920170; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file
import mcp_async  # noqa: E402  # LW-920171; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file

# Import the analysis + capture modules by path so this file is usable both
# from inside the workspace (``rocprof_profiler/server.py``) and as a
# host-side helper. None of these modules import each other except through
# ``capture``, so import order otherwise doesn't matter.
sys.path.insert(0, str(_HERE))
import analyze_rocprof  # noqa: E402  # LW-920172; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file
import att  # noqa: E402  # LW-920173; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file
import capture  # noqa: E402  # LW-920174; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file
import compute  # noqa: E402  # LW-920175; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file
import counters  # noqa: E402  # LW-920176; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file
import kernel_bench  # noqa: E402  # LW-920177; the sys.path setup directly above must run before this import, so it cannot sort to the top of the file

_capture_cli = capture.run_cli


def build_server(  # noqa: C901, PLR0915  # LW-910097; this function implements one cohesive parsing/validation routine that resists a clean split
    *,
    run_worker: Callable[..., Awaitable[str]] = mcp_async.run_cancellable,
    import_sibling: Callable[[str], ModuleType | None] = capture.import_torch_sibling,
) -> FastMCP:
    """Construct the FastMCP instance with the curated rocprof tool set.

    Exposed separately so unit tests can introspect registered tools
    without spawning a stdio loop. ``run_worker`` runs each blocking
    ``profile_*`` capture off the event loop; ``import_sibling`` locates the
    torch plugin's analyzer module.
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
    async def profile_timeline(  # noqa: PLR0913  # LW-910098; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ready_command: str | None = None,
        ready_timeout_s: float = 60.0,
        load_command: str | None = None,
        setup_command: str | None = None,
        stop_signal: str = "SIGINT",
        grace_s: float = 10.0,
        timeout_s: float = 300.0,
        hip_api: bool = False,
        kernel_include: str | None = None,
        collection_delay_s: float | None = None,
        collection_duration_s: float | None = None,
        target: str | None = None,
    ) -> str:
        """Capture a whole-run rocprofv3 system trace: host/device overlap, idle, launch cost.

        Runs the target through the shared capture lifecycle (start, wait
        for ``ready_command`` if given, run ``load_command`` if given, stop
        with ``stop_signal`` after ``grace_s``), then automatically checks
        validity (``host_idle``) and prints a summary. Runs off the main
        event loop, so other tool calls (e.g. ``captures()``) stay
        responsive while this is in flight; a client that cancels the call
        stops the capture rather than leaving it running unsupervised. If
        another GPU-using capture is already running in this server
        process, returns "busy: ..." immediately instead of queuing. Args:

            command: Command run via ``bash -lc`` (offline script, or a
                server when ``load_command`` is also given).
            cwd: Working directory for command/load_command.
            env: Extra environment variables for the target process.
            ready_command: Polled until it exits 0 before load_command runs.
                Omit for a bounded script that exits on its own.
            ready_timeout_s: Max seconds to wait for ready_command.
            load_command: Run once ready_command succeeds; command is
                stopped via stop_signal once this returns.
            setup_command: Runs to completion BEFORE command, outside the
                profiler entirely (rocprofv3 injects itself into every
                child of a profiled command, so a value command needs --
                e.g. a free port -- must be picked here, not via `$(...)`
                inside command/load_command, which would print rocprofv3's
                own banner into the captured value). Write the value to a
                file here, then have command read it back with the shell's
                `read` builtin (`read -r PORT < /tmp/port`), never
                `$(...)`/`$(cat ...)`. A nonzero exit here stops the
                capture immediately (status setup_failed) with no target
                ever started.
            stop_signal: Signal name to stop command with (default SIGINT).
            grace_s: Seconds to wait after stop_signal before escalating
                (rocprofv3 only flushes traces on a clean exit; size this
                generously for a large capture).
            timeout_s: Hard wall-clock budget for the whole capture. Set
                your MCP client's own tool-call timeout above this value:
                a capture commonly runs 10-25 minutes.
            hip_api: Also collect --hip-runtime-trace (needed for
                cpu_overhead/graphs; 2-4x the kernel-trace volume).
            kernel_include: Optional --kernel-include-regex filter.
            collection_delay_s: Seconds to skip before collecting (must be
                given together with collection_duration_s).
            collection_duration_s: Seconds to collect after the delay (must
                be given together with collection_delay_s).
            target: Not supported by this tool (rocprofv3 attach is
                unavailable); passing it returns a clear error naming the
                fix instead of an obscure rocprofv3 failure.
        """
        lifecycle = capture_runtime.Lifecycle(
            command=command,
            cwd=cwd,
            env=dict(env or {}),
            ready_command=ready_command,
            ready_timeout_s=ready_timeout_s,
            load_command=load_command,
            setup_command=setup_command,
            stop_signal=stop_signal,
            grace_s=grace_s,
            timeout_s=timeout_s,
        )
        cancel_event = threading.Event()
        try:
            return await run_worker(
                capture.profile_timeline,
                lifecycle,
                cancel_event=cancel_event,
                hip_api=hip_api,
                kernel_include=kernel_include,
                collection_delay_s=collection_delay_s,
                collection_duration_s=collection_duration_s,
                target=target,
            )
        except ValueError as exc:
            return f"error: {exc}"
        except capture_runtime.CaptureBusyError as exc:
            return capture_runtime.format_busy(exc.active)

    @mcp.tool()
    async def profile_counters(  # noqa: PLR0913  # LW-910099; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ready_command: str | None = None,
        ready_timeout_s: float = 60.0,
        load_command: str | None = None,
        setup_command: str | None = None,
        stop_signal: str = "SIGINT",
        grace_s: float = 10.0,
        timeout_s: float = 300.0,
        sets: list[str],
        kernel: str | None = None,
        target: str | None = None,
    ) -> str:
        """Capture PMC hardware counters: which hardware ceiling one hot kernel is against.

        Packs the requested sets into as few rocprofv3 --pmc passes as a
        conservative per-hardware-block model allows -- real MI210
        validation packed mfma+hbm and mfma+l2 into one pass each. **Each
        pass, not each requested set, re-runs the profiled workload in
        full**; the returned text reports how many passes were planned vs.
        actually run. Requires a detectable GPU architecture (see
        profiling_capabilities). Target one already-identified hot kernel
        via `kernel`; this is not a whole-run tool. Runs off the main event
        loop and honors client cancellation the same way profile_timeline
        does; returns "busy: ..." immediately if another capture is already
        running in this server process. Args:

            command, cwd, env, ready_command, ready_timeout_s, load_command, setup_command,
                stop_signal, grace_s, timeout_s: same as profile_timeline.
            sets: Counter-set names from the catalogue for the detected
                architecture (see profiling_capabilities), e.g.
                ["mfma", "l2", "hbm"].
            kernel: Optional --kernel-include-regex filter, applied to every pass.
            target: Not supported by this tool (rocprofv3 attach is
                unavailable); passing it returns a clear error.
        """
        lifecycle = capture_runtime.Lifecycle(
            command=command,
            cwd=cwd,
            env=dict(env or {}),
            ready_command=ready_command,
            ready_timeout_s=ready_timeout_s,
            load_command=load_command,
            setup_command=setup_command,
            stop_signal=stop_signal,
            grace_s=grace_s,
            timeout_s=timeout_s,
        )
        cancel_event = threading.Event()
        try:
            return await run_worker(
                capture.profile_counters,
                lifecycle,
                cancel_event=cancel_event,
                sets=sets,
                kernel=kernel,
                target=target,
            )
        except ValueError as exc:
            return f"error: {exc}"
        except capture_runtime.CaptureBusyError as exc:
            return capture_runtime.format_busy(exc.active)

    @mcp.tool()
    async def profile_kernel_deep(  # noqa: PLR0913  # LW-910100; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ready_command: str | None = None,
        ready_timeout_s: float = 60.0,
        load_command: str | None = None,
        setup_command: str | None = None,
        stop_signal: str = "SIGINT",
        grace_s: float = 10.0,
        timeout_s: float = 300.0,
        kernel: str,
        dispatch: int | None = None,
        target: str | None = None,
    ) -> str:
        """Capture full Speed-of-Light + roofline for one targeted kernel (rocprof-compute).

        Costs one full counter-collection sweep (minutes, not seconds). If
        rocprof-compute isn't usable on this host, returns a clear error
        instead of attempting a capture -- check profiling_capabilities
        first. Runs off the main event loop and honors client cancellation
        the same way profile_timeline does; returns "busy: ..." immediately
        if another capture is already running in this server process. Args:

            command, cwd, env, ready_command, ready_timeout_s, load_command, setup_command,
                stop_signal, grace_s, timeout_s: same as profile_timeline.
            kernel: A literal substring of the real kernel name (torch GEMMs
                dispatch as Tensile kernels like 'Cijk_Ailk_Bljk_...', not
                anything containing "gemm"); list real names with
                profile_timeline + kernels first.
            dispatch: Optional specific dispatch index to target.
            target: Not supported by this tool (rocprofv3 attach is
                unavailable); passing it returns a clear error.
        """
        lifecycle = capture_runtime.Lifecycle(
            command=command,
            cwd=cwd,
            env=dict(env or {}),
            ready_command=ready_command,
            ready_timeout_s=ready_timeout_s,
            load_command=load_command,
            setup_command=setup_command,
            stop_signal=stop_signal,
            grace_s=grace_s,
            timeout_s=timeout_s,
        )
        cancel_event = threading.Event()
        try:
            return await run_worker(
                capture.profile_kernel_deep,
                lifecycle,
                cancel_event=cancel_event,
                kernel=kernel,
                dispatch=dispatch,
                target=target,
            )
        except ValueError as exc:
            return f"error: {exc}"
        except capture_runtime.CaptureBusyError as exc:
            return capture_runtime.format_busy(exc.active)

    @mcp.tool()
    async def profile_instructions(  # noqa: PLR0913  # LW-910101; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ready_command: str | None = None,
        ready_timeout_s: float = 60.0,
        load_command: str | None = None,
        setup_command: str | None = None,
        stop_signal: str = "SIGINT",
        grace_s: float = 10.0,
        timeout_s: float = 300.0,
        kernel: str,
        target_cu: int = att.DEFAULT_TARGET_CU,
        buffer_bytes: int = att.DEFAULT_BUFFER_SIZE,
        target: str | None = None,
    ) -> str:
        """Capture per-instruction stalls inside one kernel, one compute unit (ATT).

        Requires rocprofv3 >= 7.1 and the separate rocprof-trace-decoder
        library (see profiling_capabilities); costs the most overhead of
        any capture tool here -- only reach for it after counters already
        point at a specific stall class. Runs off the main event loop and
        honors client cancellation the same way profile_timeline does;
        returns "busy: ..." immediately if another capture is already
        running in this server process. Args:

            command, cwd, env, ready_command, ready_timeout_s, load_command, setup_command,
                stop_signal, grace_s, timeout_s: same as profile_timeline.
            kernel: --kernel-include-regex value selecting the kernel to trace.
            target_cu: Which compute unit to trace (keeps output small).
            buffer_bytes: ATT buffer size in bytes; raise if the decoded
                trace reports truncation.
            target: Not supported by this tool (rocprofv3 attach is
                unavailable); passing it returns a clear error.
        """
        lifecycle = capture_runtime.Lifecycle(
            command=command,
            cwd=cwd,
            env=dict(env or {}),
            ready_command=ready_command,
            ready_timeout_s=ready_timeout_s,
            load_command=load_command,
            setup_command=setup_command,
            stop_signal=stop_signal,
            grace_s=grace_s,
            timeout_s=timeout_s,
        )
        cancel_event = threading.Event()
        try:
            return await run_worker(
                capture.profile_instructions,
                lifecycle,
                cancel_event=cancel_event,
                kernel=kernel,
                target_cu=target_cu,
                buffer_bytes=buffer_bytes,
                target=target,
            )
        except ValueError as exc:
            return f"error: {exc}"
        except capture_runtime.CaptureBusyError as exc:
            return capture_runtime.format_busy(exc.active)

    @mcp.tool()
    async def profile_ops(  # noqa: PLR0913  # LW-910102; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
        command: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ready_command: str | None = None,
        ready_timeout_s: float = 600.0,
        load_command: str | None = None,
        setup_command: str | None = None,
        stop_signal: str = "SIGINT",
        grace_s: float = 120.0,
        timeout_s: float = 1800.0,
        delay_s: float = 0.0,
        duration_s: float | None = None,
        record_shapes: bool = True,  # noqa: FBT001, FBT002  # LW-910103; this boolean parameter mirrors an external tool's own boolean flag
        inject: bool = True,  # noqa: FBT001, FBT002  # LW-910104; this boolean parameter mirrors an external tool's own boolean flag
        target: str | None = None,
    ) -> str:
        """Capture an in-process torch.profiler trace of any candidate program.

        Delegates to the torch plugin's capture_ops.profile_ops, staged
        alongside rocprof; returns a clear error if that plugin isn't
        staged. Pass target (a target_id from start_target) instead of
        command to window an already-running warm target rather than
        launching a fresh process -- load_command is then required. Runs
        off the main event loop and honors client cancellation the same way
        profile_timeline does; returns "busy: ..." immediately if another
        capture is already running in this server process. Args:

            command, cwd, env, ready_command, ready_timeout_s, load_command, setup_command,
                stop_signal, grace_s, timeout_s: same as profile_timeline
                (grace/timeout are more generous here: the in-process trace
                write can itself take a while). command is omitted when
                target is given.
            delay_s: Seconds after arming before the profiler starts.
            duration_s: Seconds to profile before auto-stopping; omit to
                profile until process exit / stop_signal. With target=,
                bounds the window instead.
            record_shapes: Capture per-op input shapes (needed for
                gemm_shapes/roofline and for certify to pass). Not
                applicable with target= (fixed when the target was started).
            inject: Set False when command already opens its own separate
                torch.profiler session internally (e.g. a serving engine's
                native profiler_config + start_profile()/stop_profile()
                hooks). Two independent profiler sessions in one process
                crash the CUPTI/roctracer/kineto backend outright (SIGSEGV,
                not a catchable error). With inject=False this tool never
                arms its own signal-based session; it still points the
                command at VIBESYS_TORCH_PROFILE_OUT_DIR so the command's
                own profiler writes its trace where this tool's discovery/
                analysis pipeline will find it.
            target: A target_id from start_target, to window an
                already-running warm target instead of launching a fresh
                process.
        """
        cancel_event = threading.Event()
        try:
            return await run_worker(
                capture.profile_ops,
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
                inject=inject,
                target=target,
            )
        except capture_runtime.CaptureBusyError as exc:
            return capture_runtime.format_busy(exc.active)

    @mcp.tool()
    def start_target(  # noqa: PLR0913  # LW-910105; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        setup_command: str | None = None,
        ready_command: str | None = None,
        ready_timeout_s: float = 60.0,
        stop_signal: str = "SIGINT",
        grace_s: float = 10.0,
        timeout_s: float = 300.0,
    ) -> str:
        """Launch a reusable, warm target process for profile_ops(target=...).

        Delegates to the torch plugin's capture_ops.start_target (the same
        function torch/server.py's own start_target tool calls -- one
        shared implementation). rocprof's own rocprofv3-based captures
        cannot use a warm target (rocprofv3 attach is unavailable; see
        profiling_capabilities), so this exists only to support the
        delegated profile_ops(target=...) above without needing a second
        MCP server. Once started, call profile_ops(target=<id>,
        load_command=...) for each window, and stop_target(<id>) when done
        -- every target still running when this server process exits is
        stopped automatically.

        Args:
            command: Command to launch, run via bash -lc; must keep running
                (a server, or a script that loops/sleeps) rather than exit
                on its own.
            cwd: Working directory for command/ready_command.
            env: Extra environment variables for the target process.
            setup_command: Runs to completion BEFORE command, outside the
                profiler injection entirely. A nonzero exit here means no
                target is started.
            ready_command: Polled until it exits 0 before this call
                returns. Omit to return as soon as command is launched.
            ready_timeout_s: Max seconds to wait for ready_command.
            stop_signal: Signal name stop_target sends (default SIGINT).
            grace_s: Seconds stop_target waits after stop_signal before
                escalating to SIGTERM/SIGKILL.
            timeout_s: Hard wall-clock budget for setup_command plus the
                ready-wait only (the target itself keeps running afterward).
        """
        return capture.start_target(
            command,
            cwd=cwd,
            env=env,
            setup_command=setup_command,
            ready_command=ready_command,
            ready_timeout_s=ready_timeout_s,
            stop_signal=stop_signal,
            grace_s=grace_s,
            timeout_s=timeout_s,
        )

    @mcp.tool()
    def stop_target(target: str) -> str:
        """Stop a warm target started with start_target: stop_signal -> grace -> escalate.

        Args:
            target: The target_id returned by start_target.
        """
        return capture.stop_target(target)

    @mcp.tool()
    def targets() -> str:
        """List targets currently running in this server process (from start_target)."""
        return capture.targets()

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
    def kernels(report: str, top: int = 15, window: str = "load") -> str:
        """Top GPU kernels by total execution time, with a library-family column.

        Args:
            report: A timeline capture id, or an explicit directory/file path.
            top: Number of kernels to show (default 15).
            window: 'load' (default, when recorded) | 'all' | 'startup' | explicit
                'start_s:end_s' relative to trace start. A server capture's startup
                (weight load, warmup, KV init) can dominate the trace, so this defaults
                to just the recorded load phase; pass 'all' for the whole run.
        """
        return _capture_cli(
            analyze_rocprof.cmd_kernels,
            report=capture.resolve_report_arg(report),
            top=top,
            window=window,
        )

    @mcp.tool()
    def families(report: str, window: str = "load") -> str:
        """GPU time grouped by kernel library family (e.g. hipBLASLt, AITER, Triton).

        Args:
            report: A timeline capture id, or an explicit directory/file path.
            window: 'load' (default, when recorded) | 'all' | 'startup' | explicit
                'start_s:end_s' relative to trace start. A server capture's startup
                (weight load, warmup, KV init) can dominate the trace, so this defaults
                to just the recorded load phase; pass 'all' for the whole run.
        """
        return _capture_cli(
            analyze_rocprof.cmd_families,
            report=capture.resolve_report_arg(report),
            window=window,
        )

    @mcp.tool()
    def idle_gaps(report: str, top: int = 10, window: str = "load") -> str:
        """GPU busy vs idle, largest idle gaps between kernel launches.

        Args:
            report: A timeline capture id, or an explicit directory/file path.
            top: Number of gaps to show (default 10).
            window: 'load' (default, when recorded) | 'all' | 'startup' | explicit
                'start_s:end_s' relative to trace start. A server capture's startup
                (weight load, warmup, KV init) can dominate the trace, so this defaults
                to just the recorded load phase; pass 'all' for the whole run.
        """
        return _capture_cli(
            analyze_rocprof.cmd_idle_gaps,
            report=capture.resolve_report_arg(report),
            top=top,
            window=window,
        )

    @mcp.tool()
    def cpu_overhead(report: str, window: str = "load") -> str:
        """HIP API launch overhead and launch-bound heuristic.

        Args:
            report: A timeline capture id, or an explicit directory/file path.
            window: 'load' (default, when recorded) | 'all' | 'startup' | explicit
                'start_s:end_s' relative to trace start. A server capture's startup
                (weight load, warmup, KV init) can dominate the trace, so this defaults
                to just the recorded load phase; pass 'all' for the whole run.
        """
        return _capture_cli(
            analyze_rocprof.cmd_cpu_overhead,
            report=capture.resolve_report_arg(report),
            window=window,
        )

    @mcp.tool()
    def memory(report: str, window: str = "load") -> str:
        """Memory copies by direction, bytes, bandwidth.

        Args:
            report: A timeline capture id, or an explicit directory/file path.
            window: 'load' (default, when recorded) | 'all' | 'startup' | explicit
                'start_s:end_s' relative to trace start. A server capture's startup
                (weight load, warmup, KV init) can dominate the trace, so this defaults
                to just the recorded load phase; pass 'all' for the whole run.
        """
        return _capture_cli(
            analyze_rocprof.cmd_memory, report=capture.resolve_report_arg(report), window=window
        )

    @mcp.tool()
    def graphs(report: str, window: str = "load") -> str:
        """HIP graph launches and attribution-degradation check.

        Args:
            report: A timeline capture id, or an explicit directory/file path.
            window: 'load' (default, when recorded) | 'all' | 'startup' | explicit
                'start_s:end_s' relative to trace start. A server capture's startup
                (weight load, warmup, KV init) can dominate the trace, so this defaults
                to just the recorded load phase; pass 'all' for the whole run.
        """
        return _capture_cli(
            analyze_rocprof.cmd_graphs, report=capture.resolve_report_arg(report), window=window
        )

    @mcp.tool()
    def host_idle(report: str, window: str = "load") -> str:
        """Detect a mostly host-idle / load-missed capture (validity check).

        Args:
            report: A timeline capture id, or an explicit directory/file path.
            window: 'load' (default, when recorded) | 'all' | 'startup' | explicit
                'start_s:end_s' relative to trace start. A server capture's startup
                (weight load, warmup, KV init) can dominate the trace, so this defaults
                to just the recorded load phase; pass 'all' for the whole run.
        """
        return _capture_cli(
            analyze_rocprof.cmd_host_idle,
            report=capture.resolve_report_arg(report),
            window=window,
        )

    @mcp.tool()
    def query(report: str, sql: str) -> str:
        """Run arbitrary SQL against a rocpd SQLite export (ROCm 7+ only).

        Only works against a rocpd SQLite (.db) export -- ROCm 7+'s
        ``rocprofv3 ... --output-format rocpd``. Does not work against the
        CSV or ``--output-format json`` output the other tools here read;
        use kernels/families/summary/etc. for those. Not windowed: the SQL
        runs against the whole rocpd export.

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

    torch_analyzer = import_sibling("analyze_torch_profile")

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


def main(argv: list[str] | None = None) -> None:  # noqa: D103  # LW-910106; this is a small standalone-script helper whose name and body are self-explanatory
    parser = argparse.ArgumentParser(
        prog="vibesys-rocprof-mcp",
        description="Stdio MCP server exposing the rocprof capture + analysis toolkit.",
    )
    parser.parse_args(argv)
    # Any warm target started via start_target() and still running when this
    # process exits (normal exit, or an uncaught error unwinding to here)
    # must not be left running -- see capture_runtime.stop_all_targets.
    atexit.register(capture_runtime.stop_all_targets)
    mcp = build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
