#!/usr/bin/env python3
"""Generic, engine-agnostic op-level ``torch.profiler`` capture.

``profile_ops`` runs *any* command (a candidate torch program: an offline
script, or a server driven by a load generator) through the shared
``capture_runtime`` lifecycle, with ``inject/sitecustomize.py`` prepended to
its ``PYTHONPATH``. That module arms itself the moment the target process
imports torch and shows a visible GPU, and writes a raw Kineto/Chrome trace
per process (see its docstring for the full mechanism). This file owns
nothing engine-specific: ``command``/``env``/``ready_command``/
``load_command`` are opaque, agent-supplied shell strings, exactly like every
other ``profile_*`` tool built on ``capture_runtime``.

After the capture, this module discovers every trace the run produced
(one per process that armed), picks the *primary* one (the trace with the
most GPU kernel events — the process that actually did GPU work, in a
multi-process program), runs the torch analyzer's ``certify`` plus a compact
summary against it, and records the choice in the capture's manifest.

Usage as a CLI (mirrors ``analyze_torch_profile.py``'s subcommand style, for
tests and ad hoc use without the MCP server)::

    python capture_ops.py --command "python3 workload.py"
        [--cwd DIR] [--env KEY=VALUE ...]
        [--ready-command CMD] [--ready-timeout-s N]
        [--load-command CMD] [--stop-signal SIGINT]
        [--grace-s N] [--timeout-s N]
        [--delay-s N] [--duration-s N] [--no-record-shapes] [--no-inject]

Pass ``--no-inject`` when ``--command`` manages its own, separate
``torch.profiler`` session (e.g. a serving engine's native
``profiler_config``/``start_profile()`` path) instead of relying on this
module's injection: running two independent ``torch.profiler.profile()``
sessions in one process is unsupported and has crashed the profiling
backend outright (SIGSEGV, empty trace) on real ROCm hardware rather than
raising a catchable error. See ``profile_ops``'s ``inject`` argument.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import signal
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading

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

sys.path.insert(0, str(_HERE))
import analyze_torch_profile  # noqa: E402

_INJECT_DIR = _HERE / "inject"
_TRACE_GLOB = "*.pt.trace.json.gz"
_SUMMARY_TOP = 5

# Fixed subdirectory names under a warm target's own out_dir (see
# start_target): the injection's default VIBESYS_TORCH_PROFILE_OUT_DIR (used
# only if a window's control file is somehow never consumed) and the
# control-file directory each profile_ops(target=...) window writes its
# desired per-window out_dir into before signaling (see
# inject/sitecustomize.py's "Repeated windows and warm targets" section).
_TARGET_TRACES_SUBDIR = "traces"
_TARGET_CONTROL_SUBDIR = "control"
_TARGET_CONTROL_FILE = "next_window"

__all__ = ["profile_ops", "start_target"]


# ---------------------------------------------------------------------------
# Env wiring for the injected sitecustomize
# ---------------------------------------------------------------------------


def _build_capture_env(  # noqa: PLR0913  # tracked: #288
    *,
    user_env: dict[str, str] | None,
    out_dir: Path,
    delay_s: float,
    duration_s: float | None,
    record_shapes: bool,
    inject: bool = True,
) -> dict[str, str]:
    """Layer the injection's env controls under any caller-supplied ``env``.

    ``PYTHONPATH`` is prepended (not replaced) with the inject directory so
    the target's own module search path still works; every other key the
    caller passes wins over our defaults on conflict.

    ``inject=False`` (see ``profile_ops``'s ``inject`` argument) skips
    arming ``sitecustomize.py`` entirely (no ``VIBESYS_TORCH_PROFILE=1``, no
    ``PYTHONPATH`` prepend): use this when ``command`` already manages its
    own, separate ``torch.profiler`` session (e.g. a serving engine's native
    ``profiler_config``/``start_profile()``/``stop_profile()`` path).
    Running *two* independent ``torch.profiler.profile()`` sessions in one
    process is unsupported and has been observed to crash the profiling
    backend outright (SIGSEGV, empty trace) on real ROCm/MI210 hardware
    rather than raise a catchable Python error -- see the module docstring's
    "inject=False" section. ``VIBESYS_TORCH_PROFILE_OUT_DIR`` is still set
    even when ``inject`` is false, so a self-profiling target can write its
    trace where this module's discovery/analysis pipeline will find it.
    """
    merged: dict[str, str] = {"VIBESYS_TORCH_PROFILE_OUT_DIR": str(out_dir)}
    if inject:
        merged["VIBESYS_TORCH_PROFILE"] = "1"
        merged["VIBESYS_TORCH_PROFILE_DELAY_S"] = str(delay_s)
        merged["VIBESYS_TORCH_PROFILE_RECORD_SHAPES"] = "1" if record_shapes else "0"
        if duration_s is not None:
            merged["VIBESYS_TORCH_PROFILE_DURATION_S"] = str(duration_s)
    merged.update(user_env or {})

    if inject:
        existing_pythonpath = merged.get("PYTHONPATH") or os.environ.get("PYTHONPATH", "")
        parts = [str(_INJECT_DIR)]
        if existing_pythonpath:
            parts.append(existing_pythonpath)
        merged["PYTHONPATH"] = os.pathsep.join(parts)
    return merged


def _target_arm_env(
    out_dir: Path, *, record_shapes: bool, existing_pythonpath: str
) -> dict[str, str]:
    """Env additions that arm a warm target for repeated signal windows.

    ``VIBESYS_TORCH_PROFILE_TRIGGER=signal`` (see inject/sitecustomize.py)
    means the injection never self-triggers a window; every window here is
    driven explicitly by ``_profile_ops_on_target`` sending SIGUSR1/SIGUSR2
    directly. ``VIBESYS_TORCH_PROFILE_CONTROL_DIR`` is the directory each
    window writes its desired output directory into before signaling
    SIGUSR1 (the control-file protocol); ``VIBESYS_TORCH_PROFILE_OUT_DIR``
    is only the fallback used if a window somehow starts without consuming
    a control file. *existing_pythonpath* is prepended with (not replaced
    by) the inject directory, mirroring ``_build_capture_env``: it must be
    resolved from the caller's own ``env``/``PYTHONPATH`` before this
    callable runs (``capture_runtime.start_target`` invokes it with only the
    allocated ``out_dir``, not the caller's ``env`` dict), since ``arm``
    always wins over ``env`` on key conflicts.
    """
    control_dir = out_dir / _TARGET_CONTROL_SUBDIR
    control_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "VIBESYS_TORCH_PROFILE": "1",
        "VIBESYS_TORCH_PROFILE_TRIGGER": "signal",
        "VIBESYS_TORCH_PROFILE_OUT_DIR": str(out_dir / _TARGET_TRACES_SUBDIR),
        "VIBESYS_TORCH_PROFILE_CONTROL_DIR": str(control_dir),
        "VIBESYS_TORCH_PROFILE_RECORD_SHAPES": "1" if record_shapes else "0",
    }
    parts = [str(_INJECT_DIR)]
    if existing_pythonpath:
        parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def start_target(  # noqa: PLR0913  # tracked: #288
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    setup_command: str | None = None,
    ready_command: str | None = None,
    ready_timeout_s: float = 60.0,
    stop_signal: str = "SIGINT",
    grace_s: float = 10.0,
    timeout_s: float = 300.0,
    record_shapes: bool = True,  # tracked: #288
) -> str:
    """Launch *command* as a warm target armed for repeated torch.profiler signal windows.

    Thin wrapper over ``capture_runtime.start_target``: composes the
    injection env (``inject/sitecustomize.py``'s ``VIBESYS_TORCH_PROFILE_*``
    controls, in "signal"-trigger mode) and keeps the target running.
    Returns a ``target_id``; call ``profile_ops(target=target_id, ...)`` to
    take a window against it, and ``stop_target(target_id)`` when done.

    A signal sent before the target process has actually installed its
    SIGUSR1/SIGUSR2 handlers (interpreter/site startup) is dropped, not
    queued -- so this call does not return until sitecustomize.py's own
    "armed" marker file confirms the handlers are live, ANDed with
    *ready_command* when the caller also supplies one (e.g. a server health
    check): the target is only ever reported ready once both are true.

    Overhead of leaving a target armed but idle (no window open) is
    negligible: measured within run-to-run noise on real MI210 hardware
    (see ``docs/contributing/amd-profiler-worklog.md``).
    """
    existing_pythonpath = (env or {}).get("PYTHONPATH") or os.environ.get("PYTHONPATH", "")
    return capture_runtime.start_target(
        command,
        cwd=cwd,
        env=env,
        setup_command=setup_command,
        ready_command=lambda out_dir: _armed_ready_command(out_dir, ready_command),
        ready_timeout_s=ready_timeout_s,
        stop_signal=stop_signal,
        grace_s=grace_s,
        timeout_s=timeout_s,
        arm=lambda out_dir: _target_arm_env(
            out_dir, record_shapes=record_shapes, existing_pythonpath=existing_pythonpath
        ),
    )


def _armed_ready_command(out_dir: Path, user_ready_command: str | None) -> str:
    """The target is ready once sitecustomize's "armed" marker exists (see its docstring).

    ANDed with *user_ready_command* when given, so a caller's own readiness
    check (e.g. a server health probe) still applies on top.
    """
    marker = out_dir / _TARGET_CONTROL_SUBDIR / "armed"
    check = f"test -f '{marker}'"
    if user_ready_command:
        return f"{check} && ( {user_ready_command} )"
    return check


# ---------------------------------------------------------------------------
# Trace discovery + primary selection
# ---------------------------------------------------------------------------


def discover_traces(out_dir: Path) -> list[Path]:
    """Every ``<pid>.pt.trace.json.gz`` the capture's processes wrote, sorted."""
    return sorted(out_dir.rglob(_TRACE_GLOB))


_POST_STOP_TRACE_POLL_S = 1.0


def wait_for_additional_traces(out_dir: Path, *, grace_s: float) -> None:
    """Give sibling/descendant target processes a bounded window to export.

    ``capture_runtime.run_capture`` only waits for the *one* process it
    directly launched (``command``, e.g. a serving engine's API-server
    process) to exit. A multi-process target's other processes (e.g. a
    separate engine-core/worker process, the one that actually does GPU
    work) receive the same ``stop_signal`` via the process-group broadcast
    (``os.killpg`` in ``capture_runtime``) and, if this injection is armed
    there too, run their own independent ``stop_and_export()`` -- which
    pays the same real, minutes-scale ROCm post-export hang documented in
    ``inject/sitecustomize.py``, on its own schedule, not synchronized with
    the directly-launched process's exit at all. Observed on real ROCm
    hardware (see the worklog): the worker process's own
    "profiling started" log line appeared, but by the time this function's
    caller used to call ``discover_traces`` immediately, its trace file did
    not exist yet -- so the primary-trace selection silently fell back to
    the driver-only process's near-empty trace (0 GPU kernels) instead of
    the real one, with no error at all.

    Bounded and cheap in the common single-process case: polls for
    ``*.pt.trace.json.gz`` files under *out_dir* and returns as soon as the
    set of paths and their sizes are unchanged across one full poll
    interval (nothing left to arrive, or nothing ever will), so it costs at
    most one ``_POST_STOP_TRACE_POLL_S`` tick when there is only ever one
    trace. Never waits longer than *grace_s*, the same "how long might the
    known hang take" budget the caller already sized for the directly
    launched process's own stop.
    """
    if grace_s <= 0:
        return
    deadline = time.monotonic() + grace_s
    last_sizes: dict[Path, int] | None = None
    while time.monotonic() < deadline:
        current = {p: p.stat().st_size for p in discover_traces(out_dir) if p.is_file()}
        if last_sizes is not None and current == last_sizes:
            return
        last_sizes = current
        time.sleep(min(_POST_STOP_TRACE_POLL_S, max(0.0, deadline - time.monotonic())))


def _kernel_count(path: Path) -> int | None:
    """Number of GPU kernel events in *path*, or ``None`` if unreadable/not a trace.

    Reuses the torch analyzer's own trace-indexing helpers rather than
    re-implementing Chrome-trace parsing here: this module and
    ``analyze_torch_profile.py`` are one cohesive plugin (co-staged, same
    directory), not separate architectural layers.
    """
    try:
        raw = analyze_torch_profile._read_json_maybe_gz(str(path))  # noqa: SLF001
    except (OSError, ValueError) as exc:
        print(f"[profile_ops] could not read {path}: {exc!r}", file=sys.stderr)  # noqa: T201  # tracked: #288
        return None
    if not analyze_torch_profile._is_chrome_trace(raw):  # noqa: SLF001
        return None
    index = analyze_torch_profile._index_trace(raw)  # noqa: SLF001
    return len(index.kernels)


def pick_primary_trace(trace_paths: list[Path]) -> Path | None:
    """The trace with the most GPU kernel events — the process that did GPU work.

    Ties broken by path for determinism (stable across otherwise-identical
    reruns). A trace that cannot be read/parsed loses to any readable one.
    """
    best: Path | None = None
    best_count = -1
    for path in trace_paths:
        count = _kernel_count(path)
        if count is None:
            continue
        if count > best_count or (count == best_count and best is not None and path < best):
            best_count = count
            best = path
    return best


def _record_traces_in_manifest(out_dir: Path, *, primary: Path | None, traces: list[Path]) -> None:
    manifest: dict = {}
    with contextlib.suppress(FileNotFoundError, ValueError):
        manifest = capture_runtime.load_manifest(out_dir)
    manifest["primary_trace"] = str(primary.relative_to(out_dir)) if primary else None
    manifest["trace_files"] = [str(p.relative_to(out_dir)) for p in traces]
    capture_runtime.write_manifest(out_dir, manifest)


# ---------------------------------------------------------------------------
# Compact post-capture analysis
# ---------------------------------------------------------------------------


def _run_cmd(fn, **kwargs) -> str:  # noqa: ANN001, ANN003  # tracked: #288
    """Run an ``analyze_torch_profile.cmd_*`` and capture its stdout.

    Mirrors ``server.py``'s ``_capture`` helper: several ``cmd_*`` functions
    reject bad input via ``sys.exit(message)`` rather than raising, so a
    ``SystemExit`` becomes an ``error: ...`` line instead of aborting this
    module.
    """
    ns = types.SimpleNamespace(**kwargs)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(ns)
    except SystemExit as exc:
        return f"error: {exc}"
    return buf.getvalue() or "(no output)"


def _analyze_primary(trace_path: Path) -> str:
    sections = [
        "\n--- certify ---",
        _run_cmd(analyze_torch_profile.cmd_certify, trace=str(trace_path)),
        "\n--- top ops ---",
        _run_cmd(analyze_torch_profile.cmd_operators, report=str(trace_path), top=_SUMMARY_TOP),
        "\n--- top kernels ---",
        _run_cmd(analyze_torch_profile.cmd_kernels, report=str(trace_path), top=_SUMMARY_TOP),
        "\n--- gemm shapes ---",
        _run_cmd(
            analyze_torch_profile.cmd_gemm_shapes, trace=str(trace_path), top=_SUMMARY_TOP, out=None
        ),
    ]
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# profile_ops(target=...): a signal-triggered window on an already-running
# warm target (see start_target above), instead of a fresh process.
# ---------------------------------------------------------------------------

_LOAD_POLL_CHUNK_S = 1.0


def _tail_text(path: Path, *, max_chars: int = 4000) -> str:
    if not path.is_file():
        return ""
    text = path.read_bytes().decode("utf-8", errors="replace")
    return text if len(text) <= max_chars else text[-max_chars:]


def _run_load_command(
    load_command: str,
    *,
    cwd: str | None,
    timeout_s: float,
    log_path: Path,
    cancel_event: threading.Event | None,
) -> tuple[int | None, str, bool]:
    """Run *load_command* to completion (or timeout/cancellation), for one target window.

    Mirrors ``capture_runtime``'s own unprofiled-step runner (``setup_command``/
    ``load_command`` in the full ``run_capture`` lifecycle) but stays local
    to this module: a target-mode window signals an already-running target
    directly rather than driving a ``capture_runtime.Lifecycle``, so there
    is no ``Lifecycle`` object here to reuse that helper against. Returns
    ``(returncode, log_tail, cancelled)``; always leaves no process running
    (SIGTERM, then SIGKILL, on timeout/cancellation).
    """
    script_path = log_path.parent / "load.sh"
    script_path.write_text("#!/usr/bin/env bash\n" + load_command)
    script_path.chmod(script_path.stat().st_mode | 0o111)
    with log_path.open("wb") as handle:
        proc = subprocess.Popen(  # noqa: S603  # tracked: #288
            ["bash", str(script_path)],  # noqa: S607  # tracked: #288
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + max(timeout_s, 0.0)
    cancelled = False
    while True:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            proc.wait(timeout=min(_LOAD_POLL_CHUNK_S, remaining))
            break
        except subprocess.TimeoutExpired:
            continue
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5.0)
    return proc.returncode, _tail_text(log_path), cancelled


def _profile_ops_on_target(  # noqa: PLR0913  # tracked: #288
    target: str,
    *,
    load_command: str,
    cwd: str | None,
    duration_s: float | None,
    grace_s: float,
    timeout_s: float,
    cancel_event: threading.Event | None,
) -> str:
    """One signal-triggered torch.profiler window against an already-running warm target.

    Requires *target* to have been started via ``start_target`` (armed for
    signal-driven windows). Writes this window's own capture directory to
    the target's control file, signals SIGUSR1 to open the window, runs
    *load_command* against the already-running target (bounded by
    ``duration_s`` if given, else ``timeout_s``), signals SIGUSR2 to close
    the window and trigger export, then analyzes the resulting trace
    exactly like the fresh-process ``profile_ops`` path. The target process
    itself is left running: call ``stop_target`` separately when done
    taking windows against it.
    """
    info = capture_runtime.get_target(target)
    capture_id, out_dir = capture_runtime.new_capture("ops")
    control_dir = info.out_dir / _TARGET_CONTROL_SUBDIR
    control_dir.mkdir(parents=True, exist_ok=True)
    (control_dir / _TARGET_CONTROL_FILE).write_text(f"{out_dir}\n")

    with capture_runtime.exclusive_capture("ops", capture_id):
        capture_runtime.signal_target(target, "SIGUSR1")
        load_budget = min(duration_s, timeout_s) if duration_s is not None else timeout_s
        load_rc, load_tail, cancelled = _run_load_command(
            load_command,
            cwd=cwd,
            timeout_s=load_budget,
            log_path=out_dir / "load.log",
            cancel_event=cancel_event,
        )
        capture_runtime.signal_target(target, "SIGUSR2")
        status = "cancelled" if cancelled else ("ok" if load_rc == 0 else "load_failed")
        capture_runtime.write_manifest(
            out_dir,
            {
                "capture_id": capture_id,
                "kind": "ops",
                "target": target,
                "load_returncode": load_rc,
                "status": status,
            },
        )

    lines = [f"capture {capture_id} (ops, target={target}): {status} load_rc={load_rc}"]
    if load_tail:
        lines.append("  load log tail:")
        lines.extend(f"    {ln}" for ln in load_tail.splitlines()[-20:])

    wait_for_additional_traces(out_dir, grace_s=grace_s)
    traces = discover_traces(out_dir)
    if not traces:
        _record_traces_in_manifest(out_dir, primary=None, traces=[])
        lines.append(
            f"\nno {_TRACE_GLOB} trace files were produced for this window; confirm the target "
            "was started with start_target (armed with VIBESYS_TORCH_PROFILE_TRIGGER=signal) and "
            "that it has imported torch and shows a GPU"
        )
        return "\n".join(lines)

    primary = pick_primary_trace(traces)
    _record_traces_in_manifest(out_dir, primary=primary, traces=traces)
    if primary is None:
        lines.append(
            f"\n{len(traces)} trace file(s) found but none were readable Kineto/Chrome traces"
        )
        return "\n".join(lines)

    lines.append(f"\nprimary trace: {primary.relative_to(out_dir)} ({len(traces)} trace(s) total)")
    lines.append(_analyze_primary(primary))
    return "\n".join(lines)


def _dispatch_profile_ops_target(  # noqa: PLR0913  # tracked: #288
    target: str,
    *,
    load_command: str | None,
    cwd: str | None,
    duration_s: float | None,
    grace_s: float,
    timeout_s: float,
    cancel_event: threading.Event | None,
) -> str:
    """``profile_ops(target=...)``'s validation + dispatch, split out to keep that function's own branch count small."""
    if not load_command:
        return "error: profile_ops(target=...) requires load_command (it bounds the window)."
    try:
        return _profile_ops_on_target(
            target,
            load_command=load_command,
            cwd=cwd,
            duration_s=duration_s,
            grace_s=grace_s,
            timeout_s=timeout_s,
            cancel_event=cancel_event,
        )
    except KeyError as exc:
        return f"error: {exc}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def profile_ops(  # noqa: PLR0913  # tracked: #288
    command: str | None = None,
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
    inject: bool = True,  # noqa: FBT001, FBT002  # tracked: #288
    target: str | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Run *command* under the in-process torch.profiler injection.

    Two modes: pass ``command`` to launch a fresh, single-use process for
    this capture (the original behavior, below); pass ``target`` (a
    ``target_id`` from ``start_target``) instead to take one signal-driven
    window against an already-running warm target -- ``load_command`` is
    then required (it bounds the window) and ``command``/``ready_command``/
    ``setup_command``/``delay_s``/``record_shapes``/``inject`` do not apply
    (the target was already armed with its own ``record_shapes`` when
    started). See ``_profile_ops_on_target`` for the target-mode mechanics.

    ``inject=False``: **do not** combine this module's own injected
    ``torch.profiler`` session with a ``command`` that manages its own,
    separate one (e.g. a serving engine's native ``profiler_config`` +
    ``start_profile()``/``stop_profile()`` path). Running two independent
    ``torch.profiler.profile()`` sessions in one process is unsupported and
    has crashed the profiling backend outright on real ROCm/MI210 hardware
    (SIGSEGV, empty trace, no catchable Python error -- confirmed against a
    real eval transcript) rather than raising cleanly. When ``command``
    already produces its own trace this way, pass ``inject=False``: this
    skips arming the injection entirely (no ``VIBESYS_TORCH_PROFILE``, no
    ``PYTHONPATH`` prepend) but still exports
    ``VIBESYS_TORCH_PROFILE_OUT_DIR`` in ``command``'s env, so the engine's
    own profiler can be pointed at it (e.g.
    ``torch_profiler_dir=os.environ["VIBESYS_TORCH_PROFILE_OUT_DIR"]``) and
    the resulting trace still flows through this function's normal
    discovery/certify/summary pipeline below. Default (``inject=True``) is
    unchanged and is the right choice whenever ``command`` does not manage
    its own separate profiler session -- the common case this tool exists
    for.

    Wraps ``capture_runtime.run_capture`` (kind ``"ops"``) with
    ``inject/sitecustomize.py`` armed via ``PYTHONPATH`` + env, then picks
    the primary trace (most GPU kernel events) across every process the
    capture produced and runs ``certify`` plus a compact summary against it.

    ``load_command``/``ready_command``/``setup_command``/``stop_signal``/
    ``grace_s``/``timeout_s`` have the same meaning as every other
    ``capture_runtime`` lifecycle: omit ``load_command`` for a bounded
    script that exits on its own; set it (with ``ready_command``) to drive
    a server under load, then stop it with ``stop_signal`` (default
    ``SIGINT``) once the load finishes. ``setup_command`` runs to
    completion before ``command``, outside the injected profiling env
    entirely; use it for anything (e.g. picking a free port) whose own
    output or child processes must not run under it.

    A multi-process ``command`` (e.g. a server that forks/spawns worker
    processes) only has *one* process directly awaited by
    ``capture_runtime.run_capture``: the one ``command`` itself launched.
    Every other process that armed this injection receives the same
    ``stop_signal`` via the process-group broadcast and runs its own
    independent stop/export afterward, on its own schedule -- observed on
    real ROCm hardware to still be writing its trace after the directly
    launched process had already exited and ``run_capture`` returned. Before
    picking the primary trace, this function gives any such sibling process
    a bounded window (``wait_for_additional_traces``, capped at ``grace_s``)
    to finish, rather than finalizing the trace list immediately and
    silently missing the process that actually did the GPU work (this can
    otherwise fall back to a driver-only process's near-empty trace with no
    error at all).

    Returns prompt-sized text: the capture id, lifecycle status, the primary
    trace's certify verdict, and its top ops/kernels/GEMM shapes. The full
    manifest (every trace path, which one was primary) is written to
    ``manifest.json`` in the capture directory.

    ``capture_runtime.run_capture`` records the same ``load_window``/
    ``capture_start``/``capture_end`` timestamps here as it does for every
    other ``profile_*`` tool (a server capture's steady-state window vs. its
    startup), since that's a lifecycle-level concern, not a rocprof one. The
    op-level analyses above (certify/top ops/top kernels/GEMM shapes) do not
    yet slice their input by that window -- a startup-polluted server
    capture's op/kernel tables can still include one-time setup work. Left
    for a follow-up: TODO slice ``_analyze_primary`` by ``load_window`` the
    way ``analyze_rocprof.py``'s timeline subcommands do.

    Serialized against every other GPU-using capture in this process via
    ``capture_runtime.exclusive_capture``: raises ``CaptureBusyError`` (not
    caught here -- the MCP boundary formats it) if another capture is
    already running. ``cancel_event``, when given, is forwarded to
    ``capture_runtime.run_capture`` so a caller can stop this capture from
    another thread (see ``resources/profilers/_common/mcp_async.py``).
    """
    if target is not None:
        return _dispatch_profile_ops_target(
            target,
            load_command=load_command,
            cwd=cwd,
            duration_s=duration_s,
            grace_s=grace_s,
            timeout_s=timeout_s,
            cancel_event=cancel_event,
        )
    if command is None:
        return (
            "error: profile_ops requires either command= (launch a fresh process for this "
            "capture) or target= (an already-running warm target from start_target)."
        )

    capture_id, out_dir = capture_runtime.new_capture("ops")
    lifecycle = capture_runtime.Lifecycle(
        command=command,
        cwd=cwd,
        env=_build_capture_env(
            user_env=env,
            out_dir=out_dir,
            delay_s=delay_s,
            duration_s=duration_s,
            record_shapes=record_shapes,
            inject=inject,
        ),
        ready_command=ready_command,
        ready_timeout_s=ready_timeout_s,
        load_command=load_command,
        setup_command=setup_command,
        stop_signal=stop_signal,
        grace_s=grace_s,
        timeout_s=timeout_s,
    )
    with capture_runtime.exclusive_capture("ops", capture_id):
        result = capture_runtime.run_capture(
            [],
            lifecycle,
            kind="ops",
            out_dir=out_dir,
            meta={
                "delay_s": delay_s,
                "duration_s": duration_s,
                "record_shapes": record_shapes,
                "inject": inject,
            },
            cancel_event=cancel_event,
        )

    lines = [capture_runtime.format_result(result)]

    wait_for_additional_traces(out_dir, grace_s=grace_s)
    traces = discover_traces(out_dir)
    if not traces:
        _record_traces_in_manifest(out_dir, primary=None, traces=[])
        cause = (
            "inject=False: the process must write its own trace under "
            "$VIBESYS_TORCH_PROFILE_OUT_DIR (e.g. via its native profiler_config) -- confirm it did"
            if not inject
            else "the process never imported torch, or torch.cuda.is_available() was false in it"
        )
        lines.append(
            f"\nno {_TRACE_GLOB} trace files were produced; see the target log tail above "
            f"(common cause: {cause})"
        )
        return "\n".join(lines)

    primary = pick_primary_trace(traces)
    _record_traces_in_manifest(out_dir, primary=primary, traces=traces)
    if primary is None:
        lines.append(
            f"\n{len(traces)} trace file(s) found but none were readable Kineto/Chrome traces"
        )
        return "\n".join(lines)

    lines.append(f"\nprimary trace: {primary.relative_to(out_dir)} ({len(traces)} trace(s) total)")
    lines.append(_analyze_primary(primary))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_env_args(pairs: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--env expects KEY=VALUE, got {pair!r}")  # noqa: TRY003  # tracked: #288
        key, _, value = pair.partition("=")
        env[key] = value
    return env


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generic in-process torch.profiler capture (see profile_ops docstring)."
    )
    parser.add_argument(
        "--command", default=None, help="Target command, run via bash -lc (or use --target)"
    )
    parser.add_argument(
        "--target", default=None, help="A start_target target_id to window instead of --command"
    )
    parser.add_argument("--cwd", default=None)
    parser.add_argument(
        "--env", action="append", default=[], metavar="KEY=VALUE", help="May be repeated"
    )
    parser.add_argument("--ready-command", default=None)
    parser.add_argument("--ready-timeout-s", type=float, default=600.0)
    parser.add_argument("--load-command", default=None)
    parser.add_argument("--stop-signal", default="SIGINT")
    parser.add_argument("--grace-s", type=float, default=120.0)
    parser.add_argument("--timeout-s", type=float, default=1800.0)
    parser.add_argument("--delay-s", type=float, default=0.0)
    parser.add_argument("--duration-s", type=float, default=None)
    parser.add_argument(
        "--no-record-shapes", dest="record_shapes", action="store_false", default=True
    )
    parser.add_argument(
        "--no-inject",
        dest="inject",
        action="store_false",
        default=True,
        help="Don't arm this module's own torch.profiler session; use when --command already "
        "manages its own (e.g. a serving engine's native profiler_config path) -- combining both "
        "crashes the profiling backend.",
    )
    args = parser.parse_args(argv)

    print(  # noqa: T201  # tracked: #288
        profile_ops(
            command=args.command,
            target=args.target,
            cwd=args.cwd,
            env=_parse_env_args(args.env) or None,
            ready_command=args.ready_command,
            ready_timeout_s=args.ready_timeout_s,
            load_command=args.load_command,
            stop_signal=args.stop_signal,
            grace_s=args.grace_s,
            timeout_s=args.timeout_s,
            delay_s=args.delay_s,
            duration_s=args.duration_s,
            record_shapes=args.record_shapes,
            inject=args.inject,
        )
    )


if __name__ == "__main__":
    main()
