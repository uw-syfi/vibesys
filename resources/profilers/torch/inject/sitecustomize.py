"""In-process ``torch.profiler`` injection, activated via ``sitecustomize``.

Python's site machinery imports a top-level ``sitecustomize`` module (if any
directory on ``sys.path`` — including every ``PYTHONPATH`` entry — provides
one) once at interpreter startup, before the target program's own code runs.
``capture_ops.profile_ops`` prepends this file's directory to ``PYTHONPATH``
so it is that module for the duration of one capture, without requiring any
change to the profiled program itself.

Standalone module: stdlib + ``torch`` only (``torch`` imported lazily, never
at module scope), Python 3.10+, no ``vibesys`` imports. It is staged as a
sibling of ``analyze_torch_profile.py``/``capture_ops.py`` inside
``torch_profiler/inject/``, and does nothing at all
unless ``VIBESYS_TORCH_PROFILE=1`` is set — every other process on the
machine that happens to inherit this file on its ``PYTHONPATH`` (e.g. a
child of a profiled process) pays only the cost of the flag check below.

## What this does

When armed, it:

1. Chains to the next ``sitecustomize`` on ``sys.path``, if any (always, even
   when the profiling flag is unset — a ``sitecustomize`` shim must never
   silently swallow whatever the host project already relies on).
2. Waits (in a background daemon thread, not the main thread) for ``torch``
   to actually be imported by the host program, then checks
   ``torch.cuda.is_available()`` — true for both real CUDA and ROCm builds,
   since ROCm's torch reports HIP devices under the same ``torch.cuda`` API.
   A process that never imports torch, or
   imports it but has no visible GPU, is left completely alone: no profiler
   object is ever constructed.
3. Once armed, waits ``VIBESYS_TORCH_PROFILE_DELAY_S`` (default 0), then
   starts a ``torch.profiler.profile(activities=[CPU, CUDA], record_shapes=?,
   with_stack=False)`` session.
4. Stops it after ``VIBESYS_TORCH_PROFILE_DURATION_S`` (if set), or when the
   process receives ``SIGUSR2``, or at process exit (``atexit``/``SIGINT``),
   whichever comes first. Stopping **exports the Chrome trace to
   ``<out_dir>/<pid>-<window#>.pt.trace.json.gz`` first**, before attempting
   anything else that could hang (see "Threading and signals" below).

## Repeated windows and warm targets

After exporting, the capture state machine returns to ``idle`` instead of
staying ``stopped`` permanently: a later ``SIGUSR1`` starts a new window
(``window#`` increments each time) rather than being silently ignored. This
is what lets a caller take more than one profiling window against the same
already-running process (a "warm target"), instead of every capture needing
its own process lifetime.

``VIBESYS_TORCH_PROFILE_TRIGGER`` selects how windows are driven:

- ``auto`` (default): unchanged from before -- self-arms after
  ``VIBESYS_TORCH_PROFILE_DELAY_S``, self-stops after
  ``VIBESYS_TORCH_PROFILE_DURATION_S`` if set. Still only takes the one
  window an external caller doesn't otherwise control, since nothing but
  this module ever sends it a second ``SIGUSR1``.
- ``signal``: installs the same ``SIGUSR1``/``SIGUSR2``/``SIGINT`` handlers
  but never self-triggers. An external controller (typically
  ``capture_runtime.start_target`` + repeated
  ``capture_ops.profile_ops(target=...)`` calls) drives every window's
  start/stop explicitly by signaling this process directly. This is the
  mode a warm target is armed with.

Each window's output directory can be pointed at a fresh location via a
small control-file protocol: before sending ``SIGUSR1``, the controller may
write the desired output directory (plain UTF-8 text, an optional trailing
newline) to ``<VIBESYS_TORCH_PROFILE_CONTROL_DIR>/next_window``. The
``SIGUSR1`` handler consumes (reads, then deletes) that file and uses its
content for that one window only, falling back to
``VIBESYS_TORCH_PROFILE_OUT_DIR`` when no control file exists, is empty, or
can't be read. This lets a warm-target caller route each window's trace to
its own capture directory instead of piling every window into one shared
directory. ``VIBESYS_TORCH_PROFILE_CONTROL_DIR`` is optional; omitting it
(the default) just means every window falls back to the configured
``VIBESYS_TORCH_PROFILE_OUT_DIR``.

## Handshake: state files, never timing

A controller never has to guess how long anything takes. Every step it
waits on is announced by a file, written only once the step has happened:

- ``<control_dir>/armed``: signal handlers are installed. A ``SIGUSR1`` sent
  before this hits the inherited disposition (default: terminate).
- ``<control_dir>/ready``: ``import torch`` has *completed* and a GPU is
  visible, so a window can start. The watcher thread decides this: its own
  ``import torch`` blocks on the module's import lock until the host
  program's import finishes. ``<control_dir>/unavailable`` (with the reason)
  is written instead when no window can ever start (no GPU).
- ``<window_dir>/window.started``, ``window.exported`` or ``window.failed``
  (with the reason): per-window acknowledgements, written into the output
  directory the controller named in ``next_window``, so each window's
  acknowledgements are its own.

- ``<window_dir>/.window-<pid>-<n>.started`` / ``.failed``: per process,
  so a controller of a multi-process program knows which processes still
  owe a trace. A window is finished once its ``<pid>-<n>.pt.trace.json.gz``
  exists (renamed into place only when complete), its ``.failed`` marker
  exists, or process ``<pid>`` has exited.

A ``SIGUSR1`` that arrives after ``armed`` but before ``ready`` (including
mid-``import torch``, where a handler running on the importing thread would
see a half-initialized module) is queued, not dropped: the watcher delivers
it once ``ready`` holds. A ``SIGUSR2`` before the queued start fires cancels
it.

## Threading and signals

``torch.profiler.profile.start()``/``.stop()`` are not documented as safe to
call concurrently from an arbitrary background thread while the host
program's own threads are running arbitrary torch ops — and this module has
no way to audit the host program's threading model. The one thing Python
itself guarantees is that a registered `signal.signal` handler always runs
on the main thread, interleaved with that thread's own bytecode execution
(never concurrently with it). So instead of calling ``start()``/``stop()``
directly from the background watcher thread, the watcher only ever sends
*signals to this same process* (``SIGUSR1`` to start, ``SIGUSR2`` to stop),
and the actual ``torch.profiler`` calls happen inside the signal handlers,
always on the main thread, always one at a time. This also gives
``VIBESYS_TORCH_PROFILE`` a uniform stop mechanism: the duration timer and an
operator's explicit early-stop both resolve to "deliver ``SIGUSR2``",
handled identically either way.

``SIGUSR1``/``SIGUSR2``/``SIGINT`` handlers here always chain to whatever
handler (if any) the host program had already installed, so a host that
itself uses one of these signals keeps working.

Measured on a real MI210 (ROCm 7.2.3 / torch 2.12): signal delivery itself is
not the bottleneck. ``os.kill(SIGUSR1)`` to the wrapped handler actually
running was well under 100ms in every measurement (as low as ~0.4ms with the
main thread idle in ``Thread.join()``), including while the host process was
issuing a sustained stream of GPU launches on another thread. The real,
repeatable cost is ``torch.profiler.profile(...).start()`` itself: on this
stack it took **~1.9-2.5 seconds** to return after the handler entered
(CUPTI/roctracer-equivalent backend initialization), every single time,
regardless of workload. ``duration_s`` (see ``capture_ops.py``) is measured
from the moment ``SIGUSR1`` is *sent*, not from when recording actually
starts, so a short ``duration_s`` (single-digit seconds) can lose a large
fraction of its nominal window to this fixed cost; pad it accordingly, or use
a longer window when the caller can. The symmetric stop-side cost
(``prof.stop()`` + ``export_chrome_trace()``) was proportional to trace size
in testing (milliseconds for a near-empty trace, ~8s for a ~4MB/28k-kernel
one) -- expect it to scale further for larger real captures, which is why
``grace_s`` should stay generous (see the ROCm post-export hang note below).

## Cross-thread CPU-op capture (measured gap, not fixed here)

Measured on the same hardware: GPU kernel (CUDA/HIP activity) events are
captured for *every* thread that issues them, started before or after
``prof.start()`` -- device-side activity tracking is process-wide, not
thread-scoped. CPU-side ``cpu_op`` events (the ``RecordFunction``-based
per-op records ``record_shapes``/``gemm_shapes``/``roofline`` need) are a
different story: a background thread that already existed and was already
issuing torch ops *before* ``prof.start()`` executed recorded **zero**
``cpu_op`` events for its own ops in testing (58k GPU kernels, 0 correlated
CPU ops), while an identical workload run entirely on the main thread (so it
necessarily starts *after* the SIGUSR1-triggered ``prof.start()`` returns)
recorded CPU ops for 100% of its GPU-issuing calls. No public
``torch.profiler`` option was found to force capture on already-running
threads; this is a real limitation of arming the profiler this late (via a
signal, after the host program's own threads may already be running), not a
bug in the signal-delivery mechanism above. The practical mitigation: arm as
early as possible (``delay_s=0`` or small), since this module starts
recording as soon as the host process imports torch and shows a GPU --
typically before an application spawns its *own* worker threads during
subsequent initialization. Once armed, GPU kernel-level numbers
(``kernels``, ``certify``'s ``gpu_kernels``/``gpu_busy`` checks) stay
reliable regardless of thread; only op-level attribution
(``record_shapes`` coverage, ``gemm_shapes``, ``roofline``) needs the
issuing thread to have started after ``prof.start()``.

## The ROCm post-export hang

Exiting a ``torch.profiler`` session can hang for minutes on ROCm
(reproduced on ROCm 6.4/torch 2.9.1 and ROCm 7.2.3/torch
2.12) with the trace file already complete and valid on disk. So the order
here is fixed: ``prof.stop()``, then ``export_chrome_trace()`` immediately,
and only *after* the trace file is safely on disk does this module attempt a
best-effort ``torch.cuda.synchronize()`` — off the main thread, under a
short bounded timeout, and never allowed to block process exit even if it
never returns (it is left running in a daemon thread that dies with the
process).

## Multi-process programs

Each Python process that imports this file (typically inherited via
``PYTHONPATH``/environment across a ``subprocess``/``spawn`` boundary — a
fresh interpreter re-runs site initialization and this module) writes its
own ``<pid>-<window#>.pt.trace.json.gz`` per window. A ``fork()``-based worker (no new
interpreter) does not re-run this module at all; that matches CUDA/HIP's own
fork restrictions and is why serving engines spawn worker processes rather
than forking them after GPU init.
"""

from __future__ import annotations

import atexit
import collections
import contextlib
import gzip
import importlib.machinery
import importlib.util
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_ENV_ENABLE = "VIBESYS_TORCH_PROFILE"
_ENV_OUT_DIR = "VIBESYS_TORCH_PROFILE_OUT_DIR"
_ENV_DELAY_S = "VIBESYS_TORCH_PROFILE_DELAY_S"
_ENV_DURATION_S = "VIBESYS_TORCH_PROFILE_DURATION_S"
_ENV_RECORD_SHAPES = "VIBESYS_TORCH_PROFILE_RECORD_SHAPES"
# See "Repeated windows and warm targets" above.
_ENV_TRIGGER = "VIBESYS_TORCH_PROFILE_TRIGGER"
_ENV_CONTROL_DIR = "VIBESYS_TORCH_PROFILE_CONTROL_DIR"
_TRIGGER_AUTO = "auto"
_TRIGGER_SIGNAL = "signal"
_VALID_TRIGGERS = (_TRIGGER_AUTO, _TRIGGER_SIGNAL)
_CONTROL_NEXT_WINDOW_NAME = "next_window"
# Handshake files (see "Handshake: state files, never timing" above).
_CONTROL_ARMED_NAME = "armed"
_CONTROL_READY_NAME = "ready"
_CONTROL_UNAVAILABLE_NAME = "unavailable"
_ACK_STARTED_NAME = "window.started"
_ACK_EXPORTED_NAME = "window.exported"
_ACK_FAILED_NAME = "window.failed"
# Per-process window markers, in the window's output directory: how a
# controller knows which processes still owe a trace (see the handshake
# section).
_WINDOW_STARTED_STATE = "started"
_WINDOW_FAILED_STATE = "failed"


def _window_marker(window: int, state: str) -> str:
    return f".window-{os.getpid()}-{window}.{state}"


# Not part of the documented capture_ops.py contract: an internal knob so
# tests can exercise the "bounded wait, never block exit" path in well under
# a second instead of the real-world default below.
_ENV_SYNC_TIMEOUT_S = "VIBESYS_TORCH_PROFILE_SYNC_TIMEOUT_S"

_DEFAULT_OUT_DIR = "./torch_profile_traces"
_POLL_INTERVAL_S = 0.1
_DEFAULT_SYNCHRONIZE_TIMEOUT_S = 5.0

_LOG_PREFIX = "[vibesys-torch-inject]"


def _log(message: str) -> None:
    """Log one line, prefixed with a monotonic timestamp.

    The timestamp is required to diagnose signal-delivery latency (the gap
    between the watcher thread's ``os.kill(SIGUSR1/SIGUSR2)`` and the main
    thread actually running the corresponding handler): both call sites log
    through this function, so their timestamps are directly comparable
    without cross-referencing wall-clock log-arrival order, which a busy
    stderr stream does not guarantee.
    """
    with contextlib.suppress(Exception):
        print(  # noqa: T201  # LW-910120; this standalone script reports progress/results on stdout or stderr, its intended output mechanism
            f"{_LOG_PREFIX} t={time.monotonic():.6f} pid={os.getpid()}: {message}",
            file=sys.stderr,
            flush=True,
        )


# ---------------------------------------------------------------------------
# Chain to any other sitecustomize on sys.path
# ---------------------------------------------------------------------------


def _chain_sitecustomize() -> None:
    """Import the next ``sitecustomize`` on ``sys.path``, if any, besides us.

    ``importlib.util.find_spec("sitecustomize")`` would just return *this*
    module's own spec (site.py already registered it in ``sys.modules`` under
    that name before executing it, so the cache-checking fast path in
    ``find_spec`` short-circuits back to us). Bypass that by asking
    ``PathFinder`` directly with an explicit search path — it walks
    directories, not the module cache.
    """
    this_dir = str(Path(__file__).resolve().parent)
    search_path = [p for p in sys.path if p and str(Path(p).resolve()) != this_dir]
    try:
        spec = importlib.machinery.PathFinder().find_spec("sitecustomize", search_path)
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001  # never break host startup over a sibling shim  # LW-920183; this boundary code deliberately catches any exception from an external tool or subprocess call
        _log(f"chained sitecustomize import failed (continuing): {exc!r}")


def _write_marker(directory: Path | None, name: str, text: str) -> None:
    """Atomically publish one handshake file (write a temp file, then rename).

    A reader that sees the file therefore sees its whole content. No-op when
    *directory* is None (no controller is listening); never raises.
    """
    if directory is None:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{name}.{os.getpid()}.tmp"
        tmp.write_text(text)
        tmp.replace(directory / name)
    except OSError as exc:
        _log(f"could not write handshake file {directory / name}: {exc!r}")


# ---------------------------------------------------------------------------
# Capture state machine, driven entirely from signal handlers (main thread)
# ---------------------------------------------------------------------------


class _Capture:
    """Owns the one ``torch.profiler.profile`` session this process may run."""

    def __init__(
        self,
        *,
        out_dir: Path,
        record_shapes: bool,
        sync_timeout_s: float = _DEFAULT_SYNCHRONIZE_TIMEOUT_S,
        control_dir: Path | None = None,
        import_module: Callable[[str], ModuleType] = importlib.import_module,
    ) -> None:
        self._import_module = import_module
        self._out_dir = out_dir
        self._record_shapes = record_shapes
        self._sync_timeout_s = sync_timeout_s
        self._control_dir = control_dir
        self._lock = threading.Lock()
        self._phase = "idle"  # idle -> running -> idle (see module docstring: repeated windows)
        self._prof = None
        self._window_index = 0
        self._active_out_dir: Path | None = None
        # Where this window's acknowledgements go: only a directory the
        # controller named via next_window, so windows never share ack files.
        self._active_ack_dir: Path | None = None
        # Set by the watcher thread once torch is fully imported and a GPU is
        # visible; a SIGUSR1 before then is queued in _pending_start.
        self._ready = threading.Event()
        self._pending_start = False

    def _resolve_window_out_dir(self) -> tuple[Path, Path | None]:
        """Consume the control file naming this window's output dir, if any.

        Returns ``(out_dir, ack_dir)``: ``ack_dir`` is ``out_dir`` when the
        controller named it, else ``None`` (the shared default out_dir gets
        no acknowledgements). See the module docstring's "Repeated windows
        and warm targets" section. Falls back to the configured default
        out_dir when there is no control dir, no control file, an empty
        file, or the file can't be read -- never raises.
        """
        if self._control_dir is None:
            return self._out_dir, None
        control_file = self._control_dir / _CONTROL_NEXT_WINDOW_NAME
        try:
            text = control_file.read_text().strip()
        except OSError:
            return self._out_dir, None
        with contextlib.suppress(OSError):
            control_file.unlink()
        if not text:
            return self._out_dir, None
        return Path(text), Path(text)

    def mark_ready(self) -> bool:
        """Record that a window can start now; return whether a start was queued.

        Called from the watcher thread. It does not start the window itself
        (``torch.profiler`` calls stay on the main thread, see "Threading and
        signals"): the caller re-delivers ``SIGUSR1`` when this returns True.
        """
        with self._lock:
            self._ready.set()
            pending, self._pending_start = self._pending_start, False
        _write_marker(self._control_dir, _CONTROL_READY_NAME, "1")
        return pending

    def start(self) -> None:
        _log("SIGUSR1 handler entered (start requested)")
        with self._lock:
            if not self._ready.is_set():
                # torch not fully imported yet (possibly mid-import on this
                # very thread): queue the request instead of dropping it.
                self._pending_start = True
                _log("start queued until torch is imported and a GPU is visible")
                return
            if self._phase != "idle":
                return
            out_dir, ack_dir = self._resolve_window_out_dir()
            try:
                self._import_module("torch")
                profiler = self._import_module("torch.profiler")
                activity = profiler.ProfilerActivity

                out_dir.mkdir(parents=True, exist_ok=True)
                prof = profiler.profile(
                    activities=[activity.CPU, activity.CUDA],
                    record_shapes=self._record_shapes,
                    with_stack=False,
                )
                prof.start()
            except Exception as exc:  # noqa: BLE001  # LW-910121; this boundary code deliberately catches any exception from an external tool or subprocess call
                # self._phase was never touched above, so it is still "idle":
                # a later SIGUSR1 can retry.
                _log(
                    f"failed to start torch.profiler (idle again, next SIGUSR1 may retry): {exc!r}"
                )
                _write_marker(ack_dir, _ACK_FAILED_NAME, f"start: {exc!r}")
                return
            self._prof = prof
            self._active_out_dir = out_dir
            self._active_ack_dir = ack_dir
            self._window_index += 1
            self._phase = "running"
            _log(
                f"profiling started (record_shapes={self._record_shapes}, "
                f"window={self._window_index}, out_dir={out_dir})"
            )
            _write_marker(out_dir, _window_marker(self._window_index, _WINDOW_STARTED_STATE), "")
            _write_marker(ack_dir, _ACK_STARTED_NAME, str(self._window_index))

    def stop_and_export(self) -> None:
        _log("SIGUSR2/SIGINT/atexit handler entered (stop requested)")
        with self._lock:
            self._pending_start = False  # a stop cancels a start still queued
            if self._phase != "running" or self._prof is None:
                self._phase = "idle"
                return
            prof = self._prof
            out_dir = self._active_out_dir or self._out_dir
            ack_dir = self._active_ack_dir
            window = self._window_index
            self._prof = None
            self._active_out_dir = None
            self._active_ack_dir = None
            self._phase = "idle"  # ready for the next SIGUSR1 window
            try:
                prof.stop()
            except Exception as exc:  # noqa: BLE001  # LW-910122; this boundary code deliberately catches any exception from an external tool or subprocess call
                _log(f"torch.profiler stop() raised (continuing): {exc!r}")
                self._window_failed(out_dir, window, ack_dir, f"stop: {exc!r}")
                return
            self._export(prof, out_dir=out_dir, window=window, ack_dir=ack_dir)

    def _export(self, prof, *, out_dir: Path, window: int, ack_dir: Path | None) -> None:  # noqa: ANN001  # LW-910123; this parameter's type is intentionally left loose; annotating it now is separate cleanup work
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log(f"could not create output dir {out_dir}: {exc!r}")
            self._window_failed(out_dir, window, ack_dir, f"export: {exc!r}")
            return
        pid = os.getpid()
        raw_path = out_dir / f"{pid}-{window}.pt.trace.json"
        gz_path = out_dir / f"{pid}-{window}.pt.trace.json.gz"
        # The final name appears only once complete (rename), so a reader
        # that sees it never sees a partial trace.
        gz_tmp = out_dir / f".{gz_path.name}.tmp"
        try:
            # Export BEFORE anything that could hang (see module docstring):
            # this is the point where the trace becomes durable.
            prof.export_chrome_trace(str(raw_path))
            with raw_path.open("rb") as src, gzip.open(gz_tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            gz_tmp.replace(gz_path)
        except Exception as exc:  # noqa: BLE001  # LW-910124; this boundary code deliberately catches any exception from an external tool or subprocess call
            _log(f"failed to export chrome trace: {exc!r}")
            self._window_failed(out_dir, window, ack_dir, f"export: {exc!r}")
            return
        finally:
            with contextlib.suppress(OSError):
                raw_path.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                gz_tmp.unlink(missing_ok=True)
        _log(f"exported trace to {gz_path} ({gz_path.stat().st_size} bytes)")
        _write_marker(ack_dir, _ACK_EXPORTED_NAME, gz_path.name)
        self._bounded_synchronize()

    @staticmethod
    def _window_failed(out_dir: Path, window: int, ack_dir: Path | None, reason: str) -> None:
        _write_marker(out_dir, _window_marker(window, _WINDOW_FAILED_STATE), reason)
        _write_marker(ack_dir, _ACK_FAILED_NAME, reason)

    def _bounded_synchronize(self) -> None:
        """Best-effort post-export sync, never allowed to block process exit.

        Runs in its own daemon thread with a short join timeout; if it never
        returns, it is simply left running and dies with the process. The
        trace is already safely on disk by this point (see ``_export``), so
        this step affects nothing but this call's own return latency.
        """
        try:
            torch = self._import_module("torch")
        except Exception:  # noqa: BLE001  # LW-910125; this boundary code deliberately catches any exception from an external tool or subprocess call
            return

        def _sync() -> None:
            with contextlib.suppress(Exception):
                torch.cuda.synchronize()

        thread = threading.Thread(target=_sync, name="vibesys-torch-inject-sync", daemon=True)
        thread.start()
        thread.join(timeout=self._sync_timeout_s)
        if thread.is_alive():
            _log(
                f"post-export synchronize did not finish within {self._sync_timeout_s}s; "
                "continuing without waiting for it"
            )


# ---------------------------------------------------------------------------
# Signal wiring: our handlers always run on the main thread and always chain
# ---------------------------------------------------------------------------


class _SerialHandlers:
    """Run signal work one item at a time, never nested inside another item.

    Python runs signal handlers on the main thread between bytecodes, so a
    second signal can interrupt a handler that is still running (e.g. a
    SIGINT during the multi-second ``prof.start()`` of a SIGUSR1). Running
    the second handler right there would re-enter ``_Capture`` while it
    holds its lock (a deadlock that hangs the host process) or unwind it
    halfway via ``KeyboardInterrupt``. Instead, work that arrives while a
    handler is running is queued and runs, in arrival order, once the
    running one returns. Main-thread only, so no locking is needed here.
    """

    def __init__(self) -> None:
        self._queue: collections.deque[Callable[[], None]] = collections.deque()
        self._running = False

    def submit(self, work: Callable[[], None]) -> None:
        self._queue.append(work)
        if self._running:
            return
        self._running = True
        try:
            while self._queue:
                self._queue.popleft()()
        finally:
            self._running = False


_SERIAL_HANDLERS = _SerialHandlers()


def _install_chained_handler(sig: signal.Signals, handler) -> None:  # noqa: ANN001  # LW-910126; this parameter's type is intentionally left loose; annotating it now is separate cleanup work
    """Install *handler* for *sig*, calling any prior handler after it runs.

    Only callable from the main thread (a Python restriction on
    ``signal.signal`` itself); this module is only ever imported there, at
    interpreter startup. Both run through ``_SERIAL_HANDLERS``, so neither
    ever runs nested inside another signal's handling.
    """
    previous = signal.getsignal(sig)

    def _run(signum: int, frame) -> None:  # noqa: ANN001  # LW-910127; this parameter's type is intentionally left loose; annotating it now is separate cleanup work
        handler(signum, frame)
        if callable(previous):
            previous(signum, frame)
        elif previous is signal.SIG_DFL and sig is signal.SIGINT:
            # Restore and re-deliver so the host program still sees the
            # default KeyboardInterrupt behavior it would have gotten
            # without this module installed.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGINT)

    signal.signal(sig, lambda signum, frame: _SERIAL_HANDLERS.submit(lambda: _run(signum, frame)))


def _arm(capture: _Capture, *, delay_s: float, duration_s: float | None, trigger: str) -> None:
    """Install signal handlers and start the readiness watcher.

    In both trigger modes the watcher waits until torch is fully imported and
    a GPU is visible, then marks the capture ready (re-delivering a queued
    ``SIGUSR1``). Only ``trigger="auto"`` goes on to self-trigger a window
    after ``delay_s`` (and stop it after ``duration_s``); in
    ``trigger="signal"`` mode an external controller (typically
    ``capture_runtime.start_target`` plus repeated
    ``capture_ops.profile_ops(target=...)`` calls) drives every window,
    which is what makes the process a reusable warm target.
    """
    stop_event = threading.Event()
    control_dir = capture._control_dir  # noqa: SLF001  # LW-910128; this test exercises the standalone script's underscore-prefixed helpers directly; there is no other entry point

    def _watch() -> None:
        torch_module = _wait_for_torch(stop_event)
        if torch_module is None:
            return  # process exiting, or torch never imported
        if not _gpu_available(torch_module):
            reason = "torch imported but no GPU visible (torch.cuda.is_available() is false)"
            _log(f"{reason}; skipping capture for this process")
            _write_marker(control_dir, _CONTROL_UNAVAILABLE_NAME, reason)
            return
        if capture.mark_ready():
            _log("re-sending queued SIGUSR1 (start)")
            with contextlib.suppress(ProcessLookupError):
                os.kill(os.getpid(), signal.SIGUSR1)
        if trigger != _TRIGGER_AUTO:
            return
        if delay_s > 0 and stop_event.wait(delay_s):
            return
        _log("sending SIGUSR1 (start)")
        with contextlib.suppress(ProcessLookupError):
            os.kill(os.getpid(), signal.SIGUSR1)
        if duration_s is not None and not stop_event.wait(duration_s):
            _log("sending SIGUSR2 (stop, duration_s elapsed)")
            with contextlib.suppress(ProcessLookupError):
                os.kill(os.getpid(), signal.SIGUSR2)

    # Handlers first, so the watcher's own SIGUSR1 can never hit the
    # inherited default disposition.
    _install_chained_handler(signal.SIGUSR1, lambda *_a: capture.start())
    _install_chained_handler(signal.SIGUSR2, lambda *_a: capture.stop_and_export())
    _install_chained_handler(signal.SIGINT, lambda *_a: capture.stop_and_export())
    atexit.register(capture.stop_and_export)
    # A SIGUSR1 sent before this point hits whatever disposition the process
    # inherited across exec, so a controller must wait for this marker
    # before signaling (see the module docstring's handshake section).
    _write_marker(control_dir, _CONTROL_ARMED_NAME, "1")

    thread = threading.Thread(target=_watch, name="vibesys-torch-inject-watch", daemon=True)
    thread.start()


def _wait_for_torch(stop_event: threading.Event):  # noqa: ANN202  # LW-910131; this private helper's return type is intentionally left loose; annotating it now is separate cleanup work
    """Wait until the host program has *finished* importing torch, or forever.

    Polls ``sys.modules`` (cheap: a dict lookup + sleep). ``torch`` appears
    there at the start of its import, but the ``import torch`` below, run on
    this non-importing thread, blocks on the module's import lock until the
    host thread's import completes, so the module returned is fully
    initialized. An unbounded wait for a process that never imports torch is
    an accepted cost (the module is opt-in only): such a process was never
    going to produce a GPU trace.
    """
    while not stop_event.is_set():
        if "torch" in sys.modules:
            try:
                import torch  # noqa: PLC0415  # LW-920187; this import is deferred to avoid a hard dependency on an optional/heavy library at module load time
            except Exception as exc:  # noqa: BLE001  # LW-910132; this boundary code deliberately catches any exception from an external tool or subprocess call
                _log(f"torch present in sys.modules but import failed: {exc!r}")
                return None
            return torch
        stop_event.wait(_POLL_INTERVAL_S)
    return None


def _gpu_available(torch_module) -> bool:  # noqa: ANN001  # LW-910133; this parameter's type is intentionally left loose; annotating it now is separate cleanup work
    """``torch.cuda.is_available()`` on a fully imported torch (see ``_wait_for_torch``)."""
    try:
        return bool(torch_module.cuda.is_available())
    except Exception as exc:  # noqa: BLE001  # LW-910134; this boundary code deliberately catches any exception from an external tool or subprocess call
        _log(f"torch.cuda.is_available() raised (treating as no GPU): {exc!r}")
        return False


# ---------------------------------------------------------------------------
# Env parsing + entry point
# ---------------------------------------------------------------------------


def _parse_float_env(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        _log(f"ignoring non-numeric {name}={raw!r}; using default {default!r}")
        return default


def _main() -> None:
    _chain_sitecustomize()

    if os.environ.get(_ENV_ENABLE) != "1":
        return

    try:
        out_dir = Path(os.environ.get(_ENV_OUT_DIR) or _DEFAULT_OUT_DIR).expanduser()
        delay_s = _parse_float_env(_ENV_DELAY_S, 0.0) or 0.0
        duration_s = _parse_float_env(_ENV_DURATION_S, None)
        record_shapes = os.environ.get(_ENV_RECORD_SHAPES, "1") == "1"
        sync_timeout_s = _parse_float_env(_ENV_SYNC_TIMEOUT_S, _DEFAULT_SYNCHRONIZE_TIMEOUT_S)
        trigger = os.environ.get(_ENV_TRIGGER, _TRIGGER_AUTO)
        if trigger not in _VALID_TRIGGERS:
            _log(f"ignoring unknown {_ENV_TRIGGER}={trigger!r}; using {_TRIGGER_AUTO!r}")
            trigger = _TRIGGER_AUTO
        control_dir_raw = os.environ.get(_ENV_CONTROL_DIR)
        control_dir = Path(control_dir_raw).expanduser() if control_dir_raw else None

        capture = _Capture(
            out_dir=out_dir,
            record_shapes=record_shapes,
            sync_timeout_s=sync_timeout_s,
            control_dir=control_dir,
        )
        _arm(capture, delay_s=delay_s, duration_s=duration_s, trigger=trigger)
    except Exception as exc:  # noqa: BLE001  # LW-910135; this boundary code deliberately catches any exception from an external tool or subprocess call
        _log(f"failed to arm torch.profiler injection (host program unaffected): {exc!r}")


_main()
