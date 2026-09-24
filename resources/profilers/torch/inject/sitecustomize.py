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
``torch_profiler/inject/`` (see ``docs/contributing/amd-profiler-worklog.md``
for the general profiler-plugin staging convention), and does nothing at all
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
   since ROCm's torch reports HIP devices under the same ``torch.cuda`` API
   (see the AMD profiler worklog). A process that never imports torch, or
   imports it but has no visible GPU, is left completely alone: no profiler
   object is ever constructed.
3. Once armed, waits ``VIBESYS_TORCH_PROFILE_DELAY_S`` (default 0), then
   starts a ``torch.profiler.profile(activities=[CPU, CUDA], record_shapes=?,
   with_stack=False)`` session.
4. Stops it after ``VIBESYS_TORCH_PROFILE_DURATION_S`` (if set), or when the
   process receives ``SIGUSR2``, or at process exit (``atexit``/``SIGINT``),
   whichever comes first. Stopping **exports the Chrome trace to
   ``<out_dir>/<pid>.pt.trace.json.gz`` first**, before attempting anything
   else that could hang (see "Threading and signals" below).

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

## The ROCm post-export hang

Per the AMD profiler worklog: exiting a ``torch.profiler`` session can hang
for minutes on ROCm (reproduced on ROCm 6.4/torch 2.9.1 and ROCm 7.2.3/torch
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
own ``<pid>.pt.trace.json.gz``. A ``fork()``-based worker (no new
interpreter) does not re-run this module at all; that matches CUDA/HIP's own
fork restrictions and is why serving engines spawn worker processes rather
than forking them after GPU init.
"""

from __future__ import annotations

import atexit
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

_ENV_ENABLE = "VIBESYS_TORCH_PROFILE"
_ENV_OUT_DIR = "VIBESYS_TORCH_PROFILE_OUT_DIR"
_ENV_DELAY_S = "VIBESYS_TORCH_PROFILE_DELAY_S"
_ENV_DURATION_S = "VIBESYS_TORCH_PROFILE_DURATION_S"
_ENV_RECORD_SHAPES = "VIBESYS_TORCH_PROFILE_RECORD_SHAPES"
# Not part of the documented capture_ops.py contract: an internal knob so
# tests can exercise the "bounded wait, never block exit" path in well under
# a second instead of the real-world default below.
_ENV_SYNC_TIMEOUT_S = "VIBESYS_TORCH_PROFILE_SYNC_TIMEOUT_S"

_DEFAULT_OUT_DIR = "./torch_profile_traces"
_POLL_INTERVAL_S = 0.1
_TORCH_INIT_GRACE_RETRIES = 20
_TORCH_INIT_GRACE_INTERVAL_S = 0.05
_DEFAULT_SYNCHRONIZE_TIMEOUT_S = 5.0

_LOG_PREFIX = "[vibesys-torch-inject]"


def _log(message: str) -> None:
    with contextlib.suppress(Exception):
        print(  # noqa: T201  # tracked: #288
            f"{_LOG_PREFIX} pid={os.getpid()}: {message}", file=sys.stderr, flush=True
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
    except Exception as exc:  # noqa: BLE001  # never break host startup over a sibling shim
        _log(f"chained sitecustomize import failed (continuing): {exc!r}")


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
    ) -> None:
        self._out_dir = out_dir
        self._record_shapes = record_shapes
        self._sync_timeout_s = sync_timeout_s
        self._lock = threading.Lock()
        self._phase = "idle"  # idle -> running -> stopped
        self._prof = None

    def start(self) -> None:
        with self._lock:
            if self._phase != "idle":
                return
            try:
                import torch  # noqa: PLC0415
                from torch.profiler import ProfilerActivity, profile  # noqa: PLC0415

                self._out_dir.mkdir(parents=True, exist_ok=True)
                prof = profile(
                    activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
                    record_shapes=self._record_shapes,
                    with_stack=False,
                )
                prof.start()
            except Exception as exc:  # noqa: BLE001  # tracked: #288
                _log(f"failed to start torch.profiler (continuing without a capture): {exc!r}")
                self._phase = "stopped"  # never retry after a failed start
                return
            self._prof = prof
            self._phase = "running"
            _log(f"profiling started (record_shapes={self._record_shapes})")
            _ = torch  # keep the import alive via closure; no further use here

    def stop_and_export(self) -> None:
        with self._lock:
            if self._phase != "running" or self._prof is None:
                self._phase = "stopped"
                return
            prof = self._prof
            self._prof = None
            self._phase = "stopped"
            try:
                prof.stop()
            except Exception as exc:  # noqa: BLE001  # tracked: #288
                _log(f"torch.profiler stop() raised (continuing): {exc!r}")
                return
            self._export(prof)

    def _export(self, prof) -> None:  # noqa: ANN001  # tracked: #288
        try:
            self._out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log(f"could not create output dir {self._out_dir}: {exc!r}")
            return
        pid = os.getpid()
        raw_path = self._out_dir / f"{pid}.pt.trace.json"
        gz_path = self._out_dir / f"{pid}.pt.trace.json.gz"
        try:
            # Export BEFORE anything that could hang (see module docstring):
            # this is the point where the trace becomes durable.
            prof.export_chrome_trace(str(raw_path))
            with raw_path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        except Exception as exc:  # noqa: BLE001  # tracked: #288
            _log(f"failed to export chrome trace: {exc!r}")
            return
        finally:
            with contextlib.suppress(OSError):
                raw_path.unlink(missing_ok=True)
        _log(f"exported trace to {gz_path} ({gz_path.stat().st_size} bytes)")
        self._bounded_synchronize()

    def _bounded_synchronize(self) -> None:
        """Best-effort post-export sync, never allowed to block process exit.

        Runs in its own daemon thread with a short join timeout; if it never
        returns, it is simply left running and dies with the process. The
        trace is already safely on disk by this point (see ``_export``), so
        this step affects nothing but this call's own return latency.
        """
        try:
            import torch  # noqa: PLC0415
        except Exception:  # noqa: BLE001  # tracked: #288
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


def _install_chained_handler(sig: signal.Signals, handler) -> None:  # noqa: ANN001  # tracked: #288
    """Install *handler* for *sig*, calling any prior handler after it runs.

    Only callable from the main thread (a Python restriction on
    ``signal.signal`` itself); this module is only ever imported there, at
    interpreter startup.
    """
    previous = signal.getsignal(sig)

    def _wrapped(signum: int, frame) -> None:  # noqa: ANN001  # tracked: #288
        handler(signum, frame)
        if callable(previous):
            previous(signum, frame)
        elif previous is signal.SIG_DFL and sig is signal.SIGINT:
            # Restore and re-deliver so the host program still sees the
            # default KeyboardInterrupt behavior it would have gotten
            # without this module installed.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGINT)

    signal.signal(sig, _wrapped)


def _arm(capture: _Capture, *, delay_s: float, duration_s: float | None) -> None:
    """Wait for torch + a visible GPU, then drive start/stop via self-signals."""
    stop_event = threading.Event()

    def _watch() -> None:
        torch_module = _wait_for_torch(stop_event)
        if torch_module is None:
            return  # process exiting, or torch never imported
        if not _gpu_available(torch_module):
            _log(
                "torch imported but no GPU visible (torch.cuda.is_available() is false); "
                "skipping capture for this process"
            )
            return
        if delay_s > 0 and stop_event.wait(delay_s):
            return
        with contextlib.suppress(ProcessLookupError):
            os.kill(os.getpid(), signal.SIGUSR1)
        if duration_s is not None and not stop_event.wait(duration_s):
            with contextlib.suppress(ProcessLookupError):
                os.kill(os.getpid(), signal.SIGUSR2)

    thread = threading.Thread(target=_watch, name="vibesys-torch-inject-watch", daemon=True)
    thread.start()

    _install_chained_handler(signal.SIGUSR1, lambda *_a: capture.start())
    _install_chained_handler(signal.SIGUSR2, lambda *_a: capture.stop_and_export())
    _install_chained_handler(signal.SIGINT, lambda *_a: capture.stop_and_export())
    atexit.register(capture.stop_and_export)


def _wait_for_torch(stop_event: threading.Event):  # noqa: ANN202  # tracked: #288
    """Poll ``sys.modules`` until the host program imports torch, or forever.

    Cheap (a dict lookup + sleep) and opt-in only (this whole module is a
    no-op unless ``VIBESYS_TORCH_PROFILE=1``), so an unbounded wait for a
    process that never imports torch is an accepted cost, not a bug: such a
    process was never going to produce a GPU trace regardless.
    """
    while not stop_event.is_set():
        if "torch" in sys.modules:
            try:
                import torch  # noqa: PLC0415
            except Exception as exc:  # noqa: BLE001  # tracked: #288
                _log(f"torch present in sys.modules but import failed: {exc!r}")
                return None
            return torch
        stop_event.wait(_POLL_INTERVAL_S)
    return None


def _gpu_available(torch_module) -> bool:  # noqa: ANN001  # tracked: #288
    """``torch.cuda.is_available()``, tolerant of torch still mid-import.

    ``sys.modules["torch"]`` is populated at the *start* of ``import torch``
    (before its body finishes), so seeing it there does not guarantee
    ``torch.cuda`` is already attached. Retry briefly rather than concluding
    "no GPU" from a partial-init race.
    """
    for _attempt in range(_TORCH_INIT_GRACE_RETRIES):
        try:
            return bool(torch_module.cuda.is_available())
        except AttributeError:
            time.sleep(_TORCH_INIT_GRACE_INTERVAL_S)
        except Exception as exc:  # noqa: BLE001  # tracked: #288
            _log(f"torch.cuda.is_available() raised (treating as no GPU): {exc!r}")
            return False
    _log("torch.cuda never became available after import; treating as no GPU")
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

        capture = _Capture(
            out_dir=out_dir, record_shapes=record_shapes, sync_timeout_s=sync_timeout_s
        )
        _arm(capture, delay_s=delay_s, duration_s=duration_s)
    except Exception as exc:  # noqa: BLE001  # tracked: #288
        _log(f"failed to arm torch.profiler injection (host program unaffected): {exc!r}")


_main()
