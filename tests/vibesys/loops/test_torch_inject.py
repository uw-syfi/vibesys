"""Tests for the in-process torch.profiler injection.

``resources/profilers/torch/inject/sitecustomize.py`` is exercised end to
end via real Python subprocesses (not imported in-process): its whole job is
correct behavior around interpreter startup (``sitecustomize`` auto-import),
signal delivery, and process exit, none of which an in-process
``exec_module`` call reproduces faithfully. See
``torch_inject_fixtures.py`` for the fake ``torch`` package these subprocess
scripts import (this dev environment has no real torch installed) and the
shared env-building/subprocess helpers.

Every test keeps its sleeps small (well under a couple of seconds) so the
whole suite stays fast; the one property test over delay/duration uses a
small ``max_examples`` for the same reason, mirroring
``tests/vibesys/loops/test_capture_runtime.py``'s ``PROC_SETTINGS``.
"""

from __future__ import annotations

import gzip
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.loops.torch_inject_fixtures import (
    base_env,
    parse_call_log,
    run_python,
    write_fake_torch,
)

if TYPE_CHECKING:
    from pathlib import Path

# Real subprocesses + signal round-trips + sleeps: keep example counts small.
# Each example namespaces its own subdirectory under tmp_path (see
# test_start_and_stop_land_within_delay_duration_window), so reusing the
# function-scoped tmp_path fixture across examples is safe.
PROC_SETTINGS = settings(
    max_examples=5, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


# Child programs block on the fake torch's call log (see
# torch_inject_fixtures.py) instead of sleeping a guessed time.
_WAIT = "from torch._log import wait_for_event\n"
# A failure bound only: children exit as soon as the event they wait on happens.
_CHILD_TIMEOUT_S = 30.0


def _trace_files(out_dir: Path) -> list[Path]:
    return sorted(out_dir.glob("*.pt.trace.json.gz"))


def _read_trace(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Chaining + no-op / no-crash behavior
# ---------------------------------------------------------------------------


def test_chains_to_existing_sitecustomize_regardless_of_flag(tmp_path: Path) -> None:
    marker = tmp_path / "chained.marker"
    chain_dir = tmp_path / "chain_dir"
    chain_dir.mkdir()
    (chain_dir / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('chained')\n"
    )
    out_dir = tmp_path / "out"
    env = base_env(
        out_dir=out_dir,
        fake_torch_root=None,
        extra_pythonpath=chain_dir,
        VIBESYS_TORCH_PROFILE="0",
    )
    result = run_python("pass", env=env)
    assert result.returncode == 0, result.stderr
    assert marker.is_file(), "chained sitecustomize never ran"
    assert marker.read_text() == "chained"


def test_disabled_flag_is_a_no_op(tmp_path: Path) -> None:
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    out_dir = tmp_path / "out"
    env = base_env(out_dir=out_dir, fake_torch_root=fake_torch, VIBESYS_TORCH_PROFILE="0")
    result = run_python("import torch\nimport time\ntime.sleep(0.05)\n", env=env)
    assert result.returncode == 0, result.stderr
    assert not out_dir.exists() or not _trace_files(out_dir)


def test_no_crash_when_torch_absent(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    # No fake torch on PYTHONPATH, and this dev environment has no real
    # torch installed either -- exactly the "torch absent" case.
    env = base_env(out_dir=out_dir, fake_torch_root=None, VIBESYS_TORCH_PROFILE="1")
    result = run_python("print('hello-no-torch')", env=env)
    assert result.returncode == 0, result.stderr
    assert "hello-no-torch" in result.stdout
    assert "Traceback" not in result.stderr


def test_no_crash_when_torch_profiler_start_fails(tmp_path: Path) -> None:
    """A broken torch.profiler must not take the host program down with it."""
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    out_dir = tmp_path / "out"
    env = base_env(
        out_dir=out_dir,
        fake_torch_root=fake_torch,
        call_log=tmp_path / "calls.log",
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S="0",
        FAKE_TORCH_START_FAIL="1",
    )
    result = run_python(
        "import torch\n" + _WAIT + "wait_for_event('profile.start_failed')\nprint('survived')",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "survived" in result.stdout
    assert not _trace_files(out_dir)


def test_skips_gpu_less_process(tmp_path: Path) -> None:
    """A process that imports torch but has no GPU never constructs a profiler."""
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    out_dir = tmp_path / "out"
    call_log = tmp_path / "calls.log"
    env = base_env(
        out_dir=out_dir,
        fake_torch_root=fake_torch,
        call_log=call_log,
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S="0",
        FAKE_TORCH_GPU="0",
    )
    result = run_python("import torch\n" + _WAIT + "wait_for_event('cuda.is_available')", env=env)
    assert result.returncode == 0, result.stderr
    assert not _trace_files(out_dir)
    events = [name for _ts, name in parse_call_log(call_log)]
    assert "profile.start" not in events


# ---------------------------------------------------------------------------
# Delay/duration scheduling
# ---------------------------------------------------------------------------


@PROC_SETTINGS
@given(
    delay_s=st.floats(0.0, 0.25, allow_nan=False, allow_infinity=False),
    duration_s=st.floats(0.05, 0.25, allow_nan=False, allow_infinity=False),
)
def test_start_and_stop_land_within_delay_duration_window(
    tmp_path: Path, delay_s: float, duration_s: float
) -> None:
    """profile.start fires no earlier than delay_s after torch import, and
    profile.stop no earlier than duration_s after the start signal.

    Only lower bounds are asserted: they hold on any machine, however slow.
    That the window happens at all is guaranteed by the child waiting for
    the export event rather than sleeping a guessed time.
    """
    case_dir = tmp_path / f"case-{uuid.uuid4().hex}"
    case_dir.mkdir()
    fake_torch = write_fake_torch(case_dir / "fake_torch")
    out_dir = case_dir / "out"
    call_log = case_dir / "calls.log"
    env = base_env(
        out_dir=out_dir,
        fake_torch_root=fake_torch,
        call_log=call_log,
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S=str(delay_s),
        VIBESYS_TORCH_PROFILE_DURATION_S=str(duration_s),
    )
    result = run_python(
        "import torch\n" + _WAIT + "wait_for_event('profile.export_chrome_trace')",
        env=env,
        timeout=_CHILD_TIMEOUT_S,
    )
    assert result.returncode == 0, result.stderr

    events = parse_call_log(call_log)
    by_name: dict[str, float] = {}
    for ts, name in events:
        by_name.setdefault(name, ts)

    assert "torch.imported" in by_name
    assert "profile.start" in by_name, f"profiler never started; events={events}"
    assert "profile.stop" in by_name, f"profiler never stopped; events={events}"

    # The duration timer starts when SIGUSR1 is sent, just before
    # profile.start is logged; allow for that signal-delivery gap.
    epsilon = 0.05
    start_delta = by_name["profile.start"] - by_name["torch.import_done"]
    stop_delta = by_name["profile.stop"] - by_name["profile.start"]
    assert start_delta >= delay_s - epsilon, f"start {start_delta:.3f}s after import < delay"
    assert stop_delta >= duration_s - epsilon, f"stop {stop_delta:.3f}s after start < duration"


def test_trace_exported_when_duration_elapses(tmp_path: Path) -> None:
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    out_dir = tmp_path / "out"
    env = base_env(
        out_dir=out_dir,
        fake_torch_root=fake_torch,
        call_log=tmp_path / "calls.log",
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S="0",
        VIBESYS_TORCH_PROFILE_DURATION_S="0.1",
    )
    result = run_python(
        "import torch\n" + _WAIT + "wait_for_event('profile.export_chrome_trace')", env=env
    )
    assert result.returncode == 0, result.stderr
    traces = _trace_files(out_dir)
    assert len(traces) == 1
    data = _read_trace(traces[0])
    assert "traceEvents" in data
    assert data["traceEvents"]


# ---------------------------------------------------------------------------
# Export-before-hang-prone-sync ordering
# ---------------------------------------------------------------------------


def test_export_happens_before_bounded_synchronize_and_never_blocks_exit(tmp_path: Path) -> None:
    """The ROCm post-export-hang regression: export first, bound the sync, don't block exit."""
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    out_dir = tmp_path / "out"
    call_log = tmp_path / "calls.log"
    env = base_env(
        out_dir=out_dir,
        fake_torch_root=fake_torch,
        call_log=call_log,
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S="0",
        VIBESYS_TORCH_PROFILE_DURATION_S="0.05",
        VIBESYS_TORCH_PROFILE_SYNC_TIMEOUT_S="0.1",
        # Longer than the run timeout below: waiting on it fails the test.
        FAKE_TORCH_SYNC_DELAY_S=str(10 * _CHILD_TIMEOUT_S),
    )
    result = run_python(
        "import torch\n" + _WAIT + "wait_for_event('cuda.synchronize.start')",
        env=env,
        timeout=_CHILD_TIMEOUT_S,
    )

    assert result.returncode == 0, result.stderr

    traces = _trace_files(out_dir)
    assert len(traces) == 1
    assert _read_trace(traces[0])["traceEvents"]

    events = [name for _ts, name in parse_call_log(call_log)]
    export_idx = events.index("profile.export_chrome_trace")
    sync_start_idx = events.index("cuda.synchronize.start")
    assert export_idx < sync_start_idx, f"export did not happen before sync started: {events}"
    # The bounded wait gave up after 0.1s; the fake's 5s sleep should never
    # have been observed completing.
    assert "cuda.synchronize.done" not in events


# ---------------------------------------------------------------------------
# Signals: SIGUSR2 early-stop, SIGINT lifecycle
# ---------------------------------------------------------------------------


def test_sigusr2_toggles_stop_early_without_killing_process(tmp_path: Path) -> None:
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    out_dir = tmp_path / "out"
    call_log = tmp_path / "calls.log"
    env = base_env(
        out_dir=out_dir,
        fake_torch_root=fake_torch,
        call_log=call_log,
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S="0",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import torch\nfrom torch._log import wait_for_event\n"
            "wait_for_event('profile.export_chrome_trace')\nprint('done')",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # No duration is set, so only the SIGUSR2 can produce the export the
        # child is waiting for: the child exiting at all proves the early stop.
        _wait_for_event(call_log, "profile.start", timeout=_CHILD_TIMEOUT_S)
        os.kill(proc.pid, signal.SIGUSR2)
        stdout, stderr = proc.communicate(timeout=_CHILD_TIMEOUT_S)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, stderr
    assert "done" in stdout
    traces = _trace_files(out_dir)
    assert len(traces) == 1

    events = [name for _ts, name in parse_call_log(call_log)]
    assert events.count("profile.stop") == 1, f"stop must be idempotent (atexit too): {events}"
    assert events.count("profile.export_chrome_trace") == 1


# A failure bound only: every wait below ends as soon as the handshake file
# it waits for appears (see sitecustomize.py's "Handshake" section).
_HANDSHAKE_TIMEOUT_S = 30.0

# Never set: ``wait(interval)`` is only the pause between polls of another process's files.
_POLL_PAUSE = threading.Event()


def _wait_for_file(path: Path, *, proc: subprocess.Popen[str]) -> str:
    """Return *path*'s content once it exists; fail if *proc* exits first."""
    deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_S
    while not path.is_file():
        if proc.poll() is not None:
            pytest.fail(f"process exited (rc={proc.returncode}) before {path.name}")
        if time.monotonic() >= deadline:
            pytest.fail(f"{path} never appeared")
        _POLL_PAUSE.wait(0.01)
    return path.read_text()


def _run_window(proc: subprocess.Popen[str], control_dir: Path, window_dir: Path) -> None:
    """Drive one window the way capture_ops does: name its dir, start, stop, await acks."""
    (control_dir / "next_window").write_text(f"{window_dir}\n")
    os.kill(proc.pid, signal.SIGUSR1)
    _wait_for_file(window_dir / "window.started", proc=proc)
    os.kill(proc.pid, signal.SIGUSR2)
    _wait_for_file(window_dir / "window.exported", proc=proc)


def _signal_mode_target(
    tmp_path: Path, **overrides: str
) -> tuple[subprocess.Popen[str], Path, Path]:
    """Start a signal-trigger target that imports torch, then blocks on stdin."""
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    control_dir = tmp_path / "control"
    call_log = tmp_path / "calls.log"
    env = base_env(
        out_dir=tmp_path / "default_out",
        fake_torch_root=fake_torch,
        call_log=call_log,
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_TRIGGER="signal",
        VIBESYS_TORCH_PROFILE_CONTROL_DIR=str(control_dir),
    )
    env.update(overrides)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys\nimport torch\nsys.stdin.readline()\nprint('done')"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc, control_dir, call_log


def _finish(proc: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        return proc.communicate(input="\n", timeout=_HANDSHAKE_TIMEOUT_S)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_signal_mode_repeated_windows_produce_two_traces(tmp_path: Path) -> None:
    """Regression test: a second SIGUSR1 must start a second window, not be a no-op.

    Pre-fix, ``_Capture.stop_and_export`` set ``self._phase = "stopped"``
    unconditionally and ``start`` only acted ``if self._phase == "idle"``:
    there was no idle -> running cycle after the first stop, so a second
    SIGUSR1 was silently ignored (verified against a real MI210/ROCm 7.2.3
    warm vLLM server). Drives two full windows through the handshake and
    requires one non-empty trace per window, each in its own directory.
    """
    proc, control_dir, _ = _signal_mode_target(tmp_path)
    try:
        _wait_for_file(control_dir / "ready", proc=proc)
        _run_window(proc, control_dir, tmp_path / "w1")
        _run_window(proc, control_dir, tmp_path / "w2")
    finally:
        stdout, stderr = _finish(proc)

    assert proc.returncode == 0, stderr
    assert "done" in stdout
    for window, window_dir in enumerate((tmp_path / "w1", tmp_path / "w2"), start=1):
        traces = _trace_files(window_dir)
        assert len(traces) == 1, f"window {window}: {[p.name for p in traces]}"
        assert _read_trace(traces[0])["traceEvents"]
        assert (window_dir / "window.started").read_text() == str(window)


def test_start_signal_during_torch_import_is_queued_not_dropped(tmp_path: Path) -> None:
    """Regression test: a SIGUSR1 delivered mid-``import torch`` starts the window later.

    Pre-fix, the handler ran on the importing thread, saw a half-initialized
    torch, failed to start, and dropped the request: the window never
    happened (a flaky test exposed it; a warm target signaled during its
    startup would hit the same). The fake torch is held mid-import by a
    gate file so the signal lands there deterministically.
    """
    gate = tmp_path / "import_gate"
    proc, control_dir, call_log = _signal_mode_target(tmp_path, FAKE_TORCH_IMPORT_GATE=str(gate))
    window_dir = tmp_path / "w1"
    try:
        _wait_for_file(control_dir / "armed", proc=proc)
        _wait_for_event(call_log, "torch.imported", timeout=_HANDSHAKE_TIMEOUT_S)
        (control_dir / "next_window").write_text(f"{window_dir}\n")
        os.kill(proc.pid, signal.SIGUSR1)  # main thread is inside `import torch`
        gate.write_text("")
        _wait_for_file(control_dir / "ready", proc=proc)
        _wait_for_file(window_dir / "window.started", proc=proc)
        os.kill(proc.pid, signal.SIGUSR2)
        _wait_for_file(window_dir / "window.exported", proc=proc)
    finally:
        _stdout, stderr = _finish(proc)

    assert proc.returncode == 0, stderr
    assert len(_trace_files(window_dir)) == 1
    events = [name for _ts, name in parse_call_log(call_log)]
    assert events.index("torch.import_done") < events.index("profile.start")


def test_stop_before_ready_cancels_the_queued_start(tmp_path: Path) -> None:
    """A SIGUSR2 while a start is still queued cancels it: no window opens later."""
    gate = tmp_path / "import_gate"
    proc, control_dir, call_log = _signal_mode_target(tmp_path, FAKE_TORCH_IMPORT_GATE=str(gate))
    try:
        _wait_for_file(control_dir / "armed", proc=proc)
        _wait_for_event(call_log, "torch.imported", timeout=_HANDSHAKE_TIMEOUT_S)
        os.kill(proc.pid, signal.SIGUSR1)
        os.kill(proc.pid, signal.SIGUSR2)
        gate.write_text("")
        _wait_for_file(control_dir / "ready", proc=proc)
        # A later, explicit window still works: the queue was cleared, not wedged.
        _run_window(proc, control_dir, tmp_path / "w1")
    finally:
        _stdout, stderr = _finish(proc)

    assert proc.returncode == 0, stderr
    events = [name for _ts, name in parse_call_log(call_log)]
    assert events.count("profile.start") == 1


def test_gpu_less_target_reports_unavailable_instead_of_ready(tmp_path: Path) -> None:
    proc, control_dir, _ = _signal_mode_target(tmp_path, FAKE_TORCH_GPU="0")
    try:
        reason = _wait_for_file(control_dir / "unavailable", proc=proc)
    finally:
        _finish(proc)
    assert "no GPU" in reason
    assert not (control_dir / "ready").exists()


def test_sigint_exports_then_still_terminates_the_process(tmp_path: Path) -> None:
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    out_dir = tmp_path / "out"
    call_log = tmp_path / "calls.log"
    env = base_env(
        out_dir=out_dir,
        fake_torch_root=fake_torch,
        call_log=call_log,
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S="0",
    )
    proc = subprocess.Popen(
        # Stays alive until the SIGINT; the timeouts below are failure bounds.
        [sys.executable, "-c", "import torch\nimport time\ntime.sleep(600)"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_event(call_log, "profile.start", timeout=_CHILD_TIMEOUT_S)
        os.kill(proc.pid, signal.SIGINT)
        proc.wait(timeout=_CHILD_TIMEOUT_S)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    # The host program still terminates via SIGINT the way it would without
    # this module installed (default disposition, not swallowed).
    assert proc.returncode == -signal.SIGINT, proc.returncode
    traces = _trace_files(out_dir)
    assert len(traces) == 1


def test_sigint_during_a_running_start_handler_is_deferred_not_deadlocked(
    tmp_path: Path,
) -> None:
    """Regression test: a signal arriving mid-handler must not hang the host.

    Pre-fix, a SIGINT delivered while the SIGUSR1 handler was still inside
    ``prof.start()`` (seconds long on ROCm) ran ``stop_and_export`` nested
    on the same thread, blocked on the lock ``start`` still held, and hung
    the process forever. The fake start is held open by a gate file so the
    SIGINT lands inside it deterministically.
    """
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    out_dir = tmp_path / "out"
    call_log = tmp_path / "calls.log"
    gate = tmp_path / "start_gate"
    env = base_env(
        out_dir=out_dir,
        fake_torch_root=fake_torch,
        call_log=call_log,
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S="0",
        FAKE_TORCH_START_GATE=str(gate),
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", "import torch\nimport time\ntime.sleep(600)"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_event(call_log, "profile.start", timeout=_CHILD_TIMEOUT_S)
        os.kill(proc.pid, signal.SIGINT)  # start() is still running
        gate.write_text("")
        proc.wait(timeout=_CHILD_TIMEOUT_S)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == -signal.SIGINT, proc.returncode
    assert len(_trace_files(out_dir)) == 1


def _wait_for_event(call_log: Path, event: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(name == event for _ts, name in parse_call_log(call_log)):
            return
        _POLL_PAUSE.wait(0.02)
    raise AssertionError(  # noqa: TRY003  # LW-910311; this is a boundary error that deliberately embeds the offending value for the operator to act on
        f"{event!r} never appeared in {call_log} within {timeout}s"
    )


# ---------------------------------------------------------------------------
# Multi-process
# ---------------------------------------------------------------------------


def test_multi_process_program_writes_one_trace_per_pid(tmp_path: Path) -> None:
    """A subprocess spawned by the profiled program re-arms independently.

    Mirrors the real shape this is designed for: a spawn/exec boundary (a
    fresh interpreter re-runs site initialization), not ``fork()``.
    """
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    out_dir = tmp_path / "out"
    env = base_env(
        out_dir=out_dir,
        fake_torch_root=fake_torch,
        call_log=tmp_path / "calls.log",
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S="0",
        VIBESYS_TORCH_PROFILE_DURATION_S="0.1",
    )
    # Sequential and event-driven: the parent exports its window, then runs
    # the child, which exits once the second export (its own) is logged.
    child_script = "import torch\n" + _WAIT + "wait_for_event('profile.export_chrome_trace', 2)\n"
    parent_script = (
        "import torch, subprocess, sys, os\n"
        + _WAIT
        + "wait_for_event('profile.export_chrome_trace')\n"
        + f"subprocess.run([sys.executable, '-c', {child_script!r}], env=os.environ.copy(), check=True)\n"
    )
    result = run_python(parent_script, env=env, timeout=_CHILD_TIMEOUT_S)
    assert result.returncode == 0, result.stderr

    traces = _trace_files(out_dir)
    assert len(traces) == 2, f"expected one trace per process, got {[p.name for p in traces]}"
    pids = {p.stem.split(".")[0] for p in traces}
    assert len(pids) == 2, "both trace files must be named after distinct pids"
    for trace in traces:
        assert _read_trace(trace)["traceEvents"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
