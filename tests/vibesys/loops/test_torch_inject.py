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
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S="0",
        FAKE_TORCH_START_FAIL="1",
    )
    result = run_python("import torch\nimport time\ntime.sleep(0.3)\nprint('survived')", env=env)
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
    result = run_python("import torch\nimport time\ntime.sleep(0.3)", env=env)
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
    """profile.start fires ~delay_s after torch import; profile.stop ~duration_s later.

    Generous slack accounts for the watcher's poll interval and ordinary CI
    scheduling jitter -- this checks the scheduling *contract* (start
    respects delay, stop respects duration), not tight real-time precision.
    """
    case_dir = tmp_path / f"case-{uuid.uuid4().hex}"
    case_dir.mkdir()
    fake_torch = write_fake_torch(case_dir / "fake_torch")
    out_dir = case_dir / "out"
    call_log = case_dir / "calls.log"
    slack = 0.6
    sleep_s = delay_s + duration_s + 0.5
    env = base_env(
        out_dir=out_dir,
        fake_torch_root=fake_torch,
        call_log=call_log,
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S=str(delay_s),
        VIBESYS_TORCH_PROFILE_DURATION_S=str(duration_s),
    )
    result = run_python(f"import torch\nimport time\ntime.sleep({sleep_s})\n", env=env, timeout=20)
    assert result.returncode == 0, result.stderr

    events = parse_call_log(call_log)
    by_name: dict[str, float] = {}
    for ts, name in events:
        by_name.setdefault(name, ts)

    assert "torch.imported" in by_name
    assert "profile.start" in by_name, f"profiler never started; events={events}"
    assert "profile.stop" in by_name, f"profiler never stopped; events={events}"

    start_delta = by_name["profile.start"] - by_name["torch.imported"]
    stop_delta = by_name["profile.stop"] - by_name["profile.start"]
    assert -0.05 <= start_delta <= delay_s + slack, (
        f"start landed {start_delta:.3f}s after import; expected ~{delay_s:.3f}s"
    )
    assert -0.05 <= stop_delta <= duration_s + slack, (
        f"stop landed {stop_delta:.3f}s after start; expected ~{duration_s:.3f}s"
    )


def test_trace_exported_when_duration_elapses(tmp_path: Path) -> None:
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    out_dir = tmp_path / "out"
    env = base_env(
        out_dir=out_dir,
        fake_torch_root=fake_torch,
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S="0",
        VIBESYS_TORCH_PROFILE_DURATION_S="0.1",
    )
    result = run_python("import torch\nimport time\ntime.sleep(0.6)\n", env=env)
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
        FAKE_TORCH_SYNC_DELAY_S="5",  # would hang for 5s if we ever waited on it
    )
    started = time.monotonic()
    result = run_python("import torch\nimport time\ntime.sleep(1.0)\n", env=env, timeout=10)
    wall = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert wall < 3.0, f"process took {wall:.2f}s -- looks like it blocked on the fake 5s sync"

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
    proc = subprocess.Popen(  # tracked: #288
        [sys.executable, "-c", "import torch\nimport time\ntime.sleep(2.0)\nprint('done')"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_event(call_log, "profile.start", timeout=5.0)
        early_stop_at = time.monotonic()
        os.kill(proc.pid, signal.SIGUSR2)
        _wait_for_event(call_log, "profile.export_chrome_trace", timeout=5.0)
        export_delay = time.monotonic() - early_stop_at
        stdout, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, stderr
    assert "done" in stdout
    assert export_delay < 1.0, "export took too long after SIGUSR2 -- did it actually fire early?"
    traces = _trace_files(out_dir)
    assert len(traces) == 1

    events = [name for _ts, name in parse_call_log(call_log)]
    assert events.count("profile.stop") == 1, f"stop must be idempotent (atexit too): {events}"
    assert events.count("profile.export_chrome_trace") == 1


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
    proc = subprocess.Popen(  # tracked: #288
        [sys.executable, "-c", "import torch\nimport time\ntime.sleep(10)"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_event(call_log, "profile.start", timeout=5.0)
        os.kill(proc.pid, signal.SIGINT)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    # The host program still terminates via SIGINT the way it would without
    # this module installed (default disposition, not swallowed).
    assert proc.returncode == -signal.SIGINT, proc.returncode
    traces = _trace_files(out_dir)
    assert len(traces) == 1


def _wait_for_event(call_log: Path, event: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(name == event for _ts, name in parse_call_log(call_log)):
            return
        time.sleep(0.02)
    raise AssertionError(  # noqa: TRY003  # tracked: #288
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
        VIBESYS_TORCH_PROFILE="1",
        VIBESYS_TORCH_PROFILE_DELAY_S="0",
        VIBESYS_TORCH_PROFILE_DURATION_S="0.1",
    )
    child_script = "import torch\nimport time\ntime.sleep(0.4)\n"
    parent_script = (
        "import torch, time, subprocess, sys, os\n"
        "time.sleep(0.4)\n"
        f"subprocess.run([sys.executable, '-c', {child_script!r}], env=os.environ.copy(), check=True)\n"
    )
    result = run_python(parent_script, env=env, timeout=20)
    assert result.returncode == 0, result.stderr

    traces = _trace_files(out_dir)
    assert len(traces) == 2, f"expected one trace per process, got {[p.name for p in traces]}"
    pids = {p.stem.split(".")[0] for p in traces}
    assert len(pids) == 2, "both trace files must be named after distinct pids"
    for trace in traces:
        assert _read_trace(trace)["traceEvents"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
