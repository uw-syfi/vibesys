"""Tests for the shared capture-lifecycle runtime.

``resources/profilers/_common/capture_runtime.py`` is a standalone script
(stdlib only, no ``vibesys`` imports — it is staged alongside every profiler
plugin, see ``docs/contributing/amd-profiler-worklog.md``), so it is loaded
by file path here rather than imported as a package, mirroring
``tests/vibesys/loops/test_profiler_mcp.py`` and
``tests/vibesys/loops/test_torch_profile_analyzer.py``.

Process-lifecycle behavior is exercised against real subprocesses (a tiny
configurable "fake profiler" script, see ``_FAKE_PROFILER_SOURCE`` below)
rather than mocks, since the whole point of this module is correct signal
delivery, process-group management, and /proc descendant walking. Every
test keeps its timeouts small (well under a second in the common case) to
keep the whole suite fast.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

_REPO = Path(__file__).resolve().parents[3]
_MODULE_PATH = _REPO / "resources" / "profilers" / "_common" / "capture_runtime.py"

# Real subprocesses + signal round-trips: keep example counts tiny so the
# whole property-test slice stays well under the ~20s budget.
PROC_SETTINGS = settings(max_examples=5, deadline=None)
# Pure in-process logic (no subprocesses): can afford more examples.
FAST = settings(max_examples=20, deadline=None)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations (from `from __future__ import
    # annotations`) via sys.modules[cls.__module__], so the module must be
    # registered there before exec_module runs its class bodies.
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# NOT registered under the bare "capture_runtime" name: that name is
# deliberately pre-claimed by test_rocprof_capture.py's own module-level
# load (see its comment) so that capture.py's internal `import
# capture_runtime` resolves to the *same* object that test file's own
# tests use. sys.modules is process-global and collection order across
# test files is not something either file controls; clobbering that name
# here would silently split module identity for every test that relies on
# it (server.py's `import capture_runtime`, lazily resolved by
# test_profiler_mcp.py's server fixtures, ends up with whichever module
# last won the shared slot) -- this file only needs its own private
# capture_runtime.py instance to test in isolation, so it gets one under a
# name nothing else claims.
cr = _load_module("_test_capture_runtime_standalone", _MODULE_PATH)


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.05) -> bool:  # noqa: ANN001  # tracked: #288
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _read_pid(path: Path) -> int:
    assert _wait_until(path.is_file, timeout=2.0)
    return int(path.read_text().strip())


# -- fake profiler harness -----------------------------------------------------
#
# Configurable via env vars so one script covers every escalation scenario:
#   FAKE_PROFILER_OUT_DIR       where to write profiler.pid / trace.txt / grandchild.pid
#   FAKE_PROFILER_IGNORE_SIGINT "1" -> the wrapper process itself ignores SIGINT
#   FAKE_PROFILER_GRANDCHILD    "1" -> also spawns a setsid'd grandchild (sleep)
#
# Forwards its own liveness through profiler.pid always; writes trace.txt only
# once its (non-setsid) child has exited *and* it received SIGINT first —
# mirroring a real profiler that flushes its trace on a clean, signaled exit.
_FAKE_PROFILER_SOURCE = textwrap.dedent(
    """
    import signal

    # Register the handler before anything else (importing subprocess pulls
    # in locale and can take a nontrivial moment): otherwise a SIGINT that
    # arrives during our own startup hits Python's default handler and
    # raises KeyboardInterrupt instead of the intended no-op/forward below.
    state = {"got_sigint": False, "ignore_sigint": False}

    def _on_sigint(signum, frame):
        if state["ignore_sigint"]:
            return
        state["got_sigint"] = True

    signal.signal(signal.SIGINT, _on_sigint)

    import os
    import subprocess
    import sys
    import time

    out_dir = os.environ.get("FAKE_PROFILER_OUT_DIR")
    state["ignore_sigint"] = os.environ.get("FAKE_PROFILER_IGNORE_SIGINT") == "1"
    spawn_grandchild = os.environ.get("FAKE_PROFILER_GRANDCHILD") == "1"

    if spawn_grandchild:
        grandchild = subprocess.Popen(["sleep", "60"], start_new_session=True)
        if out_dir:
            with open(os.path.join(out_dir, "grandchild.pid"), "w") as handle:
                handle.write(str(grandchild.pid))

    child = subprocess.Popen(sys.argv[1:])

    # Written last, once the handler is armed and the child is running, so a
    # readiness check keyed on this file only fires after both are true.
    if out_dir:
        with open(os.path.join(out_dir, "profiler.pid"), "w") as handle:
            handle.write(str(os.getpid()))

    rc = None
    while rc is None:
        rc = child.poll()
        if rc is None:
            time.sleep(0.02)

    if state["got_sigint"] and out_dir:
        time.sleep(0.05)
        with open(os.path.join(out_dir, "trace.txt"), "w") as handle:
            handle.write("trace\\n")

    sys.exit(rc)
    """
)


@pytest.fixture
def fake_profiler(tmp_path: Path) -> Path:
    path = tmp_path / "fake_profiler.py"
    path.write_text(_FAKE_PROFILER_SOURCE)
    return path


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / ".profiles"
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(directory))
    return directory


# -- explicit scenarios: no load_command ---------------------------------------


def test_no_load_ok(tmp_path: Path) -> None:
    lifecycle = cr.Lifecycle(command="exit 0", timeout_s=5.0)
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=tmp_path / "c", meta={})

    assert result.status is cr.CaptureStatus.OK
    assert result.target_returncode == 0
    assert result.escalated is False
    assert result.manifest_path.is_file()


def test_no_load_target_failed(tmp_path: Path) -> None:
    lifecycle = cr.Lifecycle(command="exit 7", timeout_s=5.0)
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=tmp_path / "c", meta={})

    assert result.status is cr.CaptureStatus.TARGET_FAILED
    assert result.target_returncode == 7
    assert result.escalated is False


def test_no_load_timed_out_kills_target(tmp_path: Path) -> None:
    out_dir = tmp_path / "c"
    pidfile = tmp_path / "target.pid"
    lifecycle = cr.Lifecycle(command=f"echo $$ > {pidfile}; sleep 100", timeout_s=0.3)

    start = time.monotonic()
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})
    elapsed = time.monotonic() - start

    assert result.status is cr.CaptureStatus.TIMED_OUT
    assert result.escalated is True
    assert elapsed < lifecycle.timeout_s + 4.0
    pid = _read_pid(pidfile)
    assert _wait_until(lambda: not _is_alive(pid))


# -- explicit scenarios: with load_command -------------------------------------


def test_load_not_ready(tmp_path: Path) -> None:
    out_dir = tmp_path / "c"
    pidfile = tmp_path / "target.pid"
    lifecycle = cr.Lifecycle(
        command=f"echo $$ > {pidfile}; sleep 100",
        ready_command="false",
        ready_timeout_s=0.2,
        ready_interval_s=0.05,
        load_command="true",
        grace_s=1.0,
        timeout_s=5.0,
    )
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})

    assert result.status is cr.CaptureStatus.NOT_READY
    assert result.ready_achieved is False
    assert result.load_returncode is None
    pid = _read_pid(pidfile)
    assert _wait_until(lambda: not _is_alive(pid))


def test_load_failed(tmp_path: Path) -> None:
    out_dir = tmp_path / "c"
    pidfile = tmp_path / "target.pid"
    lifecycle = cr.Lifecycle(
        command=f"echo $$ > {pidfile}; sleep 100",
        ready_command="true",
        load_command="exit 3",
        grace_s=1.0,
        timeout_s=5.0,
    )
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})

    assert result.status is cr.CaptureStatus.LOAD_FAILED
    assert result.ready_achieved is True
    assert result.load_returncode == 3
    pid = _read_pid(pidfile)
    assert _wait_until(lambda: not _is_alive(pid))


def test_load_timed_out(tmp_path: Path) -> None:
    out_dir = tmp_path / "c"
    pidfile = tmp_path / "target.pid"
    lifecycle = cr.Lifecycle(
        command=f"echo $$ > {pidfile}; sleep 100",
        ready_command="true",
        load_command="sleep 100",
        grace_s=0.5,
        timeout_s=0.3,
    )
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})

    assert result.status is cr.CaptureStatus.TIMED_OUT
    assert result.ready_achieved is True
    assert result.load_returncode is None
    pid = _read_pid(pidfile)
    assert _wait_until(lambda: not _is_alive(pid))


def test_load_ok_writes_trace_after_clean_sigint(fake_profiler: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "c"
    out_dir.mkdir()
    lifecycle = cr.Lifecycle(
        command="sleep 100",
        env={"FAKE_PROFILER_OUT_DIR": str(out_dir)},
        # Only "ready" once the fake profiler has armed its SIGINT handler
        # and started its child (it writes profiler.pid last); a plain
        # "true" would race the wrapper's own interpreter startup.
        ready_command=f"test -f {out_dir}/profiler.pid",
        ready_timeout_s=5.0,
        ready_interval_s=0.02,
        load_command="true",
        grace_s=2.0,
        timeout_s=5.0,
    )
    result = cr.run_capture(
        [sys.executable, str(fake_profiler)], lifecycle, kind="unit", out_dir=out_dir, meta={}
    )

    assert result.status is cr.CaptureStatus.OK
    assert result.escalated is False
    assert (out_dir / "trace.txt").is_file()
    pid = int((out_dir / "profiler.pid").read_text().strip())
    assert _wait_until(lambda: not _is_alive(pid))


def test_load_killed_after_grace_when_profiler_ignores_sigint(
    fake_profiler: Path, tmp_path: Path
) -> None:
    out_dir = tmp_path / "c"
    out_dir.mkdir()
    lifecycle = cr.Lifecycle(
        command="trap '' INT; sleep 100",
        env={"FAKE_PROFILER_OUT_DIR": str(out_dir), "FAKE_PROFILER_IGNORE_SIGINT": "1"},
        ready_command=f"test -f {out_dir}/profiler.pid",
        ready_timeout_s=5.0,
        ready_interval_s=0.02,
        load_command="true",
        grace_s=0.3,
        timeout_s=5.0,
    )
    start = time.monotonic()
    result = cr.run_capture(
        [sys.executable, str(fake_profiler)], lifecycle, kind="unit", out_dir=out_dir, meta={}
    )
    elapsed = time.monotonic() - start

    assert result.status is cr.CaptureStatus.KILLED_AFTER_GRACE
    assert result.escalated is True
    assert not (out_dir / "trace.txt").exists()
    assert elapsed < lifecycle.grace_s + 5.0
    pid = int((out_dir / "profiler.pid").read_text().strip())
    assert _wait_until(lambda: not _is_alive(pid))


def test_timeout_kills_setsid_grandchild_via_proc_walk(fake_profiler: Path, tmp_path: Path) -> None:
    """Escalation must walk /proc descendants, since a child may setsid away."""
    out_dir = tmp_path / "c"
    out_dir.mkdir()
    lifecycle = cr.Lifecycle(
        command="sleep 100",
        env={"FAKE_PROFILER_OUT_DIR": str(out_dir), "FAKE_PROFILER_GRANDCHILD": "1"},
        timeout_s=0.3,
    )
    result = cr.run_capture(
        [sys.executable, str(fake_profiler)], lifecycle, kind="unit", out_dir=out_dir, meta={}
    )

    assert result.status is cr.CaptureStatus.TIMED_OUT
    assert result.escalated is True
    grandchild_pid = int((out_dir / "grandchild.pid").read_text().strip())
    assert _wait_until(lambda: not _is_alive(grandchild_pid))


# -- setup_command: an unprofiled step that runs before the target --------------
#
# rocprofv3 LD_PRELOADs its SDK into the entire environment handed to the
# profiled process tree, and nothing suppresses the resulting init/banner
# print (confirmed on real MI210 hardware -- see
# docs/contributing/amd-profiler-worklog.md). setup_command exists so a step
# whose own output must stay clean (e.g. picking a free port) can run
# entirely outside that tree. _FAKE_INJECTING_PROFILER_SOURCE below models
# the real mechanism generically: an env var standing in for LD_PRELOAD,
# propagated to every descendant of the *wrapped* command via ordinary env
# inheritance, with no "quiet" setting that turns it off.


def test_setup_command_runs_before_target_and_both_succeed(tmp_path: Path) -> None:
    out_dir = tmp_path / "c"
    setup_marker = tmp_path / "setup.marker"
    target_marker = tmp_path / "target.marker"
    lifecycle = cr.Lifecycle(
        command=f"touch {target_marker}",
        setup_command=f"touch {setup_marker}",
        timeout_s=5.0,
    )

    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})

    assert result.status is cr.CaptureStatus.OK
    assert result.setup_returncode == 0
    assert result.target_returncode == 0
    assert setup_marker.is_file()
    assert target_marker.is_file()


def test_setup_command_failure_stops_capture_before_target_starts(tmp_path: Path) -> None:
    """Regression: a nonzero setup_command must never let the target start.

    Fails on code with no setup-gating at all (setup_command ignored, or the
    target runs regardless of setup's outcome): verified by temporarily
    making the setup-failure branch in ``run_capture`` a no-op (always fall
    through to starting the target) and re-running this test, per the
    repo's regression-test policy -- the target marker then exists and the
    status reads ``ok``, not ``setup_failed``.
    """
    out_dir = tmp_path / "c"
    target_marker = tmp_path / "target.marker"
    lifecycle = cr.Lifecycle(
        command=f"touch {target_marker}",
        setup_command="exit 9",
        timeout_s=5.0,
    )

    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})

    assert result.status is cr.CaptureStatus.SETUP_FAILED
    assert result.setup_returncode == 9
    assert result.target_returncode is None
    assert not target_marker.exists()
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == "setup_failed"
    assert manifest["setup_returncode"] == 9
    assert manifest["target_returncode"] is None


def test_setup_command_timeout_stops_capture_before_target_starts(tmp_path: Path) -> None:
    out_dir = tmp_path / "c"
    target_marker = tmp_path / "target.marker"
    lifecycle = cr.Lifecycle(
        command=f"touch {target_marker}",
        setup_command="sleep 100",
        timeout_s=0.3,
    )

    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})

    assert result.status is cr.CaptureStatus.TIMED_OUT
    assert result.target_returncode is None
    assert not target_marker.exists()


def test_setup_command_output_is_reported_in_format_result(tmp_path: Path) -> None:
    out_dir = tmp_path / "c"
    lifecycle = cr.Lifecycle(
        command="true", setup_command="echo setup-output-marker", timeout_s=5.0
    )

    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})

    assert "setup-output-marker" in (result.setup_log_tail or "")
    assert "setup log tail" in cr.format_result(result)


_FAKE_INJECTING_PROFILER_SOURCE = textwrap.dedent(
    """
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env[os.environ["FAKE_MARKER_NAME"]] = "1"
    rc = subprocess.Popen(sys.argv[1:], env=env).wait()
    sys.exit(rc)
    """
)

_FAKE_BANNERING_HELPER_SOURCE = textwrap.dedent(
    """
    import os
    import sys

    if os.environ.get(os.environ["FAKE_MARKER_NAME"]) == "1":
        sys.stdout.write("UNSUPPRESSIBLE BANNER\\n")
    sys.stdout.write("VALUE\\n")
    """
)


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{source}")
    path.chmod(path.stat().st_mode | 0o111)
    return path


def test_setup_command_output_never_leaks_the_profilers_injected_banner(tmp_path: Path) -> None:
    """Regression, models the real mechanism directly (see the section docstring above).

    ``profiler_prefix`` here stands in for rocprofv3: it injects a marker
    env var into everything it launches, and a helper that sees that marker
    always prints a banner ahead of the real value -- there is no quiet
    setting to check, matching the confirmed-on-hardware finding. Reading
    the value via ``setup_command`` (never wrapped in ``profiler_prefix``)
    must come back clean. Fails on any change that runs ``setup_command``
    through ``profiler_prefix`` instead of plain ``bash``: verified by
    temporarily changing ``_run_setup``'s ``subprocess.Popen`` call to
    prepend ``profiler_prefix`` and re-running this test -- the marker then
    shows up in the setup output.
    """
    profiler = _write_executable(
        tmp_path / "fake_injecting_profiler.py", _FAKE_INJECTING_PROFILER_SOURCE
    )
    helper = _write_executable(tmp_path / "helper.py", _FAKE_BANNERING_HELPER_SOURCE)
    out_dir = tmp_path / "c"
    marker_env = {"FAKE_MARKER_NAME": "FAKE_ROCPROFV3_INJECTED"}
    setup_out = tmp_path / "setup_output.txt"
    lifecycle = cr.Lifecycle(
        command="true",
        setup_command=f"{helper} > {setup_out}",
        env=marker_env,
        timeout_s=5.0,
    )

    result = cr.run_capture(
        [sys.executable, str(profiler)], lifecycle, kind="unit", out_dir=out_dir, meta={}
    )

    assert result.status is cr.CaptureStatus.OK
    assert setup_out.read_text() == "VALUE\n"
    assert "UNSUPPRESSIBLE BANNER" not in setup_out.read_text()


@given(
    marker_name=st.text(
        alphabet=st.characters(whitelist_categories=("Lu",)), min_size=3, max_size=16
    ).map(lambda s: "FAKE_MARKER_" + s)
)
@PROC_SETTINGS
def test_property_setup_command_never_observes_whatever_env_var_the_profiler_injects(
    tmp_path_factory: pytest.TempPathFactory, marker_name: str
) -> None:
    """Generalizes the fix over any injected-env-var name, not just one hardcoded name.

    A real profiler's injection channel (LD_PRELOAD-set env, in rocprofv3's
    case) is an implementation detail this module must not assume a
    specific name for; the property that must hold is structural --
    setup_command never runs inside profiler_prefix's process tree at all,
    so it can never observe *any* var the profiler injects into that tree.
    """
    tmp_path = tmp_path_factory.mktemp("cr")
    profiler = _write_executable(tmp_path / "profiler.py", _FAKE_INJECTING_PROFILER_SOURCE)
    helper = _write_executable(tmp_path / "helper.py", _FAKE_BANNERING_HELPER_SOURCE)
    out_dir = tmp_path / "c"
    setup_out = tmp_path / "setup_output.txt"
    lifecycle = cr.Lifecycle(
        command="true",
        setup_command=f"{helper} > {setup_out}",
        env={"FAKE_MARKER_NAME": marker_name},
        timeout_s=5.0,
    )

    result = cr.run_capture(
        [sys.executable, str(profiler)], lifecycle, kind="unit", out_dir=out_dir, meta={}
    )

    assert result.status is cr.CaptureStatus.OK
    assert setup_out.read_text() == "VALUE\n"


# -- cancellation: cancel_event stops an in-flight capture -----------------------


def test_no_load_cancel_event_stops_target_and_returns_cancelled(tmp_path: Path) -> None:
    out_dir = tmp_path / "c"
    pidfile = tmp_path / "target.pid"
    lifecycle = cr.Lifecycle(command=f"echo $$ > {pidfile}; sleep 100", timeout_s=30.0)
    cancel_event = threading.Event()

    def _cancel_soon() -> None:
        assert _wait_until(pidfile.is_file, timeout=3.0)
        cancel_event.set()

    canceller = threading.Thread(target=_cancel_soon)
    canceller.start()
    start = time.monotonic()
    result = cr.run_capture(
        [], lifecycle, kind="unit", out_dir=out_dir, meta={}, cancel_event=cancel_event
    )
    elapsed = time.monotonic() - start
    canceller.join()

    assert result.status is cr.CaptureStatus.CANCELLED
    assert result.escalated is True
    # Bounded by _POLL_CHUNK_S plus escalation, not by the target's own
    # (never reached) sleep -- proves cancellation actually interrupted the
    # wait instead of the target just happening to exit.
    assert elapsed < 10.0
    pid = _read_pid(pidfile)
    assert _wait_until(lambda: not _is_alive(pid))


def test_load_cancel_event_during_load_command_stops_both_and_returns_cancelled(
    fake_profiler: Path, tmp_path: Path
) -> None:
    out_dir = tmp_path / "c"
    out_dir.mkdir()
    load_pidfile = tmp_path / "load.pid"
    lifecycle = cr.Lifecycle(
        command="sleep 100",
        env={"FAKE_PROFILER_OUT_DIR": str(out_dir)},
        ready_command=f"test -f {out_dir}/profiler.pid",
        ready_timeout_s=5.0,
        ready_interval_s=0.02,
        load_command=f"echo $$ > {load_pidfile}; sleep 100",
        grace_s=5.0,
        timeout_s=30.0,
    )
    cancel_event = threading.Event()

    def _cancel_soon() -> None:
        assert _wait_until(load_pidfile.is_file, timeout=5.0)
        cancel_event.set()

    canceller = threading.Thread(target=_cancel_soon)
    canceller.start()
    start = time.monotonic()
    result = cr.run_capture(
        [sys.executable, str(fake_profiler)],
        lifecycle,
        kind="unit",
        out_dir=out_dir,
        meta={},
        cancel_event=cancel_event,
    )
    elapsed = time.monotonic() - start
    canceller.join()

    assert result.status is cr.CaptureStatus.CANCELLED
    assert elapsed < 10.0
    load_pid = _read_pid(load_pidfile)
    assert _wait_until(lambda: not _is_alive(load_pid))
    profiler_pid = int((out_dir / "profiler.pid").read_text().strip())
    assert _wait_until(lambda: not _is_alive(profiler_pid))


# -- exclusive_capture / CaptureBusyError: one capture at a time per process -----


@pytest.fixture(autouse=True)
def _reset_capture_slot() -> Iterator[None]:
    """Every test starts and ends with no capture holding the in-process slot."""
    cr.release_capture_slot()
    yield
    cr.release_capture_slot()


def test_acquire_capture_slot_then_second_acquire_raises_busy() -> None:
    active = cr.acquire_capture_slot("timeline", "timeline-abc")

    assert cr.active_capture() == active
    with pytest.raises(cr.CaptureBusyError) as excinfo:
        cr.acquire_capture_slot("counters", "counters-xyz")
    assert excinfo.value.active == active


def test_release_capture_slot_is_idempotent() -> None:
    cr.acquire_capture_slot("timeline", "timeline-abc")
    cr.release_capture_slot()
    cr.release_capture_slot()

    assert cr.active_capture() is None
    # A fresh acquire after release must succeed (the slot isn't stuck).
    cr.acquire_capture_slot("counters", "counters-xyz")


def test_exclusive_capture_releases_slot_on_exception() -> None:
    def _run_and_raise() -> None:
        with cr.exclusive_capture("timeline", "t-1"):
            assert cr.active_capture() is not None
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _run_and_raise()

    assert cr.active_capture() is None


def test_exclusive_capture_raises_busy_immediately_before_running_anything() -> None:
    calls = []
    with cr.exclusive_capture("timeline", "t-1"):
        try:
            with cr.exclusive_capture("counters", "t-2"):
                calls.append("ran")
        except cr.CaptureBusyError:
            pass

    assert calls == []  # the second capture's body never ran


def test_format_busy_names_the_capture_id_and_kind() -> None:
    active = cr.ActiveCapture(
        capture_id="timeline-20260101-000000-ab12", kind="timeline", started_at=0.0
    )

    message = cr.format_busy(active)

    assert "timeline-20260101-000000-ab12" in message
    assert "(timeline)" in message
    assert message.startswith("busy: capture ")


@given(
    kind=st.sampled_from(["timeline", "counters", "kernel_deep", "instructions", "ops"]),
    capture_id=st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Nd")), min_size=1, max_size=20
    ),
)
@FAST
def test_property_capture_busy_error_always_names_the_holder(kind: str, capture_id: str) -> None:
    active = cr.acquire_capture_slot(kind, capture_id)
    try:
        with pytest.raises(cr.CaptureBusyError) as excinfo:
            cr.acquire_capture_slot("other-kind", "other-id")
        assert excinfo.value.active is active
        assert capture_id in cr.format_busy(active)
        assert kind in cr.format_busy(active)
    finally:
        cr.release_capture_slot()


# -- manifest / secrets ---------------------------------------------------------


def test_manifest_written_and_parseable(tmp_path: Path) -> None:
    out_dir = tmp_path / "c"
    lifecycle = cr.Lifecycle(command="exit 0", timeout_s=5.0)
    result = cr.run_capture(
        [], lifecycle, kind="unit", out_dir=out_dir, meta={"tool_version": "1.2.3"}
    )

    data = json.loads(result.manifest_path.read_text())
    assert data["status"] == "ok"
    assert data["capture_id"] == result.capture_id
    assert data["kind"] == "unit"
    assert data["meta"] == {"tool_version": "1.2.3"}
    assert "manifest.json" not in data["output_files"]
    assert "target.log" in data["output_files"]


def test_manifest_redacts_env_values_not_keys(tmp_path: Path) -> None:
    out_dir = tmp_path / "c"
    secret = "super-secret-token-xyz"  # noqa: S105  # tracked: #288
    lifecycle = cr.Lifecycle(command="exit 0", env={"MY_TOKEN": secret}, timeout_s=5.0)
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})

    raw = result.manifest_path.read_text()
    assert secret not in raw
    data = json.loads(raw)
    assert data["lifecycle"]["env_keys"] == ["MY_TOKEN"]
    assert "env" not in data["lifecycle"]


def test_format_result_is_compact_and_mentions_status(tmp_path: Path) -> None:
    lifecycle = cr.Lifecycle(command="echo hello; exit 0", timeout_s=5.0)
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=tmp_path / "c", meta={})

    text = cr.format_result(result)
    assert "ok" in text
    assert result.capture_id in text
    assert "hello" in text


# -- command scripts (issue: profiler wrappers must not re-tokenize `command`) --

_MANGLING_PROFILER_SOURCE = textwrap.dedent(
    """
    import shlex
    import subprocess
    import sys

    # Mimics a profiler wrapper that re-joins its trailing argv with spaces
    # and re-splits it (e.g. to log or re-parse the command about to run)
    # instead of exec'ing the received argv list untouched.
    joined = " ".join(sys.argv[1:])
    mangled = shlex.split(joined)
    sys.exit(subprocess.run(mangled).returncode)
    """
)


@pytest.fixture
def mangling_profiler(tmp_path: Path) -> Path:
    path = tmp_path / "mangling_profiler.py"
    path.write_text(_MANGLING_PROFILER_SOURCE)
    return path


def test_command_survives_a_profiler_that_rejoins_and_resplits_argv(
    mangling_profiler: Path, tmp_path: Path
) -> None:
    """Regression: a profiler wrapper that re-joins/re-splits its trailing argv

    must not corrupt a multi-line ``command`` with embedded quotes. Writing
    ``command`` to a script file and handing the wrapper a single, simple
    path argument (``run_capture``'s fix) keeps the wrapper's re-join/
    re-split a no-op, since there is nothing left in the wrapped argv for it
    to mangle -- unlike the old ``bash -lc "<command>"`` argv, whose own
    quotes and newlines a naive re-join+``shlex.split`` destroys.
    """
    marker = tmp_path / "out.txt"
    command = textwrap.dedent(
        f"""\
        python3 -c "
        import pathlib
        pathlib.Path({str(marker)!r}).write_text('multi\\nline \\'quoted\\' \\$HOME text\\n')
        "
        """
    )
    lifecycle = cr.Lifecycle(command=command, timeout_s=10.0)
    result = cr.run_capture(
        [sys.executable, str(mangling_profiler)],
        lifecycle,
        kind="unit",
        out_dir=tmp_path / "c",
        meta={},
    )

    assert result.status is cr.CaptureStatus.OK, result.target_log_tail
    assert marker.read_text() == "multi\nline 'quoted' $HOME text\n"


def test_run_capture_writes_executable_scripts_and_records_them_in_manifest(
    tmp_path: Path,
) -> None:
    lifecycle = cr.Lifecycle(
        command="true", ready_command="true", load_command="true", grace_s=1.0, timeout_s=5.0
    )
    out_dir = tmp_path / "c"
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})

    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["scripts"] == {"target": "target.sh", "ready": "ready.sh", "load": "load.sh"}
    for name in ("target.sh", "ready.sh", "load.sh"):
        script = out_dir / name
        assert script.is_file()
        assert os.access(script, os.X_OK)
        assert script.read_text().startswith("#!/usr/bin/env bash\n")


def test_run_capture_no_load_command_writes_only_target_script(tmp_path: Path) -> None:
    lifecycle = cr.Lifecycle(command="true", timeout_s=5.0)
    out_dir = tmp_path / "c"
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})

    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["scripts"] == {"target": "target.sh"}
    assert not (out_dir / "ready.sh").exists()
    assert not (out_dir / "load.sh").exists()


_PRINTABLE_ASCII = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=200
)


@given(content=_PRINTABLE_ASCII)
@PROC_SETTINGS
def test_property_script_text_is_byte_for_byte_and_matches_bash_c(
    tmp_path_factory: pytest.TempPathFactory, content: str
) -> None:
    """For arbitrary bounded printable command text, the executed script's

    text equals the input byte-for-byte (after the shebang line), and the
    command runs with the same result as ``bash -c <text>`` directly.
    Wrapped in a quoted heredoc so *content* (which may contain quotes,
    ``$vars``, or heredoc-ish substrings itself) is never shell-expanded --
    the property under test is about this module's own script-writing/exec
    path, not about generating syntactically valid-vs-invalid shell text.
    """
    assume("VIBESYS_EOF" not in content)
    tmp_path = tmp_path_factory.mktemp("cr")
    text = f"cat <<'VIBESYS_EOF'\n{content}\nVIBESYS_EOF\n"

    direct = subprocess.run(  # noqa: S603  # tracked: #288
        ["bash", "-c", text],  # noqa: S607  # tracked: #288
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    lifecycle = cr.Lifecycle(command=text, timeout_s=10.0)
    out_dir = tmp_path / "c"
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})

    assert (out_dir / "target.sh").read_text() == "#!/usr/bin/env bash\n" + text
    assert result.target_returncode == direct.returncode
    assert result.target_log_tail.rstrip("\n") == direct.stdout.rstrip("\n")


# -- load window (issue: server captures polluted by startup) -------------------


def test_run_capture_records_load_window_bracketing_the_load_phase(tmp_path: Path) -> None:
    lifecycle = cr.Lifecycle(
        command="sleep 100",
        ready_command="true",
        load_command="sleep 0.05",
        grace_s=2.0,
        timeout_s=5.0,
    )
    out_dir = tmp_path / "c"
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=out_dir, meta={})

    manifest = json.loads(result.manifest_path.read_text())
    window = manifest["load_window"]
    assert window is not None
    for phase in ("ready", "start", "end"):
        stamp = window[phase]
        assert stamp["monotonic_ns"] > 0
        assert stamp["clock_monotonic_ns"] > 0
        assert stamp["realtime_ns"] > 0
    assert window["ready"]["clock_monotonic_ns"] <= window["start"]["clock_monotonic_ns"]
    assert window["start"]["clock_monotonic_ns"] <= window["end"]["clock_monotonic_ns"]


def test_run_capture_load_window_is_none_without_load_command(tmp_path: Path) -> None:
    lifecycle = cr.Lifecycle(command="true", timeout_s=5.0)
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=tmp_path / "c", meta={})

    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["load_window"] is None


def test_run_capture_records_capture_start_and_end_regardless_of_load_command(
    tmp_path: Path,
) -> None:
    """``capture_start``/``capture_end`` bracket the *whole* capture, not just the load phase.

    A downstream analyzer (``analyze_rocprof.py``'s window resolution) needs
    these two anchors for two things a load-phase-only window can't give it:
    the 'startup' window (``capture_start`` .. the load phase's own start)
    and a clock-alignment sanity check (do a trace's own timestamps
    plausibly fall inside this capture's real span at all, before trusting
    any window slice of it). Recorded even for a bounded no-load-command
    capture, which still has a real start/end even though it has no
    separate "startup" vs. "load" phase to distinguish.
    """
    lifecycle = cr.Lifecycle(command="true", timeout_s=5.0)
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=tmp_path / "c", meta={})

    manifest = json.loads(result.manifest_path.read_text())
    for key in ("capture_start", "capture_end"):
        stamp = manifest[key]
        assert stamp["monotonic_ns"] > 0
        assert stamp["clock_monotonic_ns"] > 0
        assert stamp["realtime_ns"] > 0
    assert (
        manifest["capture_start"]["clock_monotonic_ns"]
        <= manifest["capture_end"]["clock_monotonic_ns"]
    )


@given(load_sleep_s=st.floats(min_value=0.01, max_value=0.15))
@PROC_SETTINGS
def test_property_capture_start_ready_load_end_are_monotonically_ordered(
    tmp_path_factory: pytest.TempPathFactory, load_sleep_s: float
) -> None:
    """capture_start <= ready <= load-start <= load-end <= capture_end, for any load duration."""
    tmp_path = tmp_path_factory.mktemp("cr")
    lifecycle = cr.Lifecycle(
        command="sleep 100",
        ready_command="true",
        load_command=f"sleep {load_sleep_s:.3f}",
        grace_s=2.0,
        timeout_s=5.0,
    )
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=tmp_path / "c", meta={})

    manifest = json.loads(result.manifest_path.read_text())
    window = manifest["load_window"]
    order = [
        manifest["capture_start"]["clock_monotonic_ns"],
        window["ready"]["clock_monotonic_ns"],
        window["start"]["clock_monotonic_ns"],
        window["end"]["clock_monotonic_ns"],
        manifest["capture_end"]["clock_monotonic_ns"],
    ]
    assert order == sorted(order)


# -- capture store ---------------------------------------------------------------


def test_profiles_root_absolute_with_relative_env_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``new_capture``'s directory is passed both to a profiled subprocess

    (launched with ``cwd=lifecycle.cwd``, an arbitrary agent-supplied
    directory) as its ``-d``-style output argument, and used directly by
    this process's own analysis code afterward. If it resolves to a
    relative path, the two sides can disagree on where it actually is: the
    subprocess resolves it against ``lifecycle.cwd``, this process resolves
    it against its own launch directory. Any capture whose ``cwd`` differs
    from the server's own launch directory (the common case for a real
    profiling target) then writes its whole output tree somewhere the
    analyzer never looks, and every downstream ``summary``/analyzer call
    sees an empty capture. ``$VIBESYS_PROFILE_DIR`` unset (the documented
    default, ``./.profiles``) is exactly this relative case.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBESYS_PROFILE_DIR", raising=False)

    root = cr.profiles_root()

    assert root.is_absolute()
    assert root == (tmp_path / ".profiles").resolve()


@given(
    relative_env=st.sampled_from(["./.profiles", ".profiles", "out/profiles", "./a/b/.profiles"])
)
@settings(
    max_examples=5, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_profiles_root_absolute_for_any_relative_env_value(
    relative_env: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generalizes the fixed bug over any relative ``$VIBESYS_PROFILE_DIR`` value.

    Whatever relative form the env var takes, the resolved root must always
    be absolute and anchored to this process's own cwd -- never left
    relative for a profiled subprocess (launched with a different cwd) to
    resolve differently.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", relative_env)

    root = cr.profiles_root()

    assert root.is_absolute()
    assert root == (tmp_path / relative_env).resolve()


def test_new_capture_out_dir_matches_what_a_subprocess_with_different_cwd_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end regression: a profiler subprocess launched with a *different*

    ``lifecycle.cwd`` than this process's own cwd, given ``new_capture``'s
    directory as its output argument (exactly how every ``profile_*`` tool
    in ``capture.py`` calls it), must write into the same directory this
    process reads back from -- not a directory relative to its own cwd
    instead. Mirrors the real symptom: a real ``profile_timeline`` capture
    with a different ``cwd`` completed with ``target_rc=0`` and rocprofv3's
    own log confirming files were written, but the analyzer discovered zero
    CSVs because it looked in a different (wrongly relative) directory.
    """
    server_cwd = tmp_path / "server_launch_dir"
    server_cwd.mkdir()
    target_cwd = tmp_path / "target_cwd"
    target_cwd.mkdir()
    monkeypatch.chdir(server_cwd)
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", "./.profiles")

    _capture_id, out_dir = cr.new_capture("timeline")

    # A profiler that receives out_dir as a *relative* argument the way a
    # real rocprofv3 invocation's "-d <out_dir>" would if the bug were
    # still present would resolve it against its own cwd (lifecycle.cwd).
    # out_dir is passed here exactly as capture.py's profile_* tools pass
    # it to their profiler_prefix, i.e. an absolute Path once resolved.
    fake_profiler = tmp_path / "fake_profiler.py"
    fake_profiler.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "d = Path(sys.argv[sys.argv.index('-d') + 1])\n"
        "d.mkdir(parents=True, exist_ok=True)\n"
        "(d / 'marker.csv').write_text('ok\\n')\n"
    )
    lifecycle = cr.Lifecycle(command="true", cwd=str(target_cwd), timeout_s=10.0)

    result = cr.run_capture(
        [sys.executable, str(fake_profiler), "-d", str(out_dir)],
        lifecycle,
        kind="timeline",
        out_dir=out_dir,
        meta={},
    )

    assert result.status is cr.CaptureStatus.OK
    assert (result.out_dir / "marker.csv").is_file()
    assert out_dir.is_absolute()


def test_new_capture_id_format_and_directory(profiles_dir: Path) -> None:
    capture_id, directory = cr.new_capture("timeline")

    assert directory.is_dir()
    assert directory.parent == profiles_dir
    assert capture_id.startswith("timeline-")
    parts = capture_id.split("-")
    assert len(parts) == 4  # kind, date, time, hex
    assert len(parts[-1]) == 4


@pytest.mark.usefixtures("profiles_dir")
def test_new_capture_ids_are_unique() -> None:
    ids = {cr.new_capture("counters")[0] for _ in range(10)}
    assert len(ids) == 10


@pytest.mark.usefixtures("profiles_dir")
def test_resolve_by_id_and_by_path() -> None:
    capture_id, directory = cr.new_capture("ops")

    assert cr.resolve(capture_id) == directory.resolve()
    assert cr.resolve(str(directory)) == directory.resolve()

    with pytest.raises(FileNotFoundError):
        cr.resolve("no-such-capture")


@pytest.mark.usefixtures("profiles_dir")
def test_list_captures_orders_newest_first_and_respects_limit() -> None:
    ids = []
    for _ in range(3):
        capture_id, directory = cr.new_capture("kernel_deep")
        cr.write_manifest(directory, {"kind": "kernel_deep", "status": "ok"})
        ids.append(capture_id)
        time.sleep(0.01)  # distinct mtimes

    summaries = cr.list_captures(limit=2)
    assert len(summaries) == 2
    assert summaries[0].capture_id == ids[-1]
    assert summaries[0].kind == "kernel_deep"
    assert summaries[0].status == "ok"


@pytest.mark.usefixtures("profiles_dir")
def test_list_captures_empty_when_no_profiles_dir() -> None:
    assert cr.list_captures() == []


# -- import shim: must work from both the repo tree and a staged workspace -----


def _find_common_dir(plugin_dir: Path) -> Path:
    """Mirror the shim documented in capture_runtime.py's module docstring."""
    base = plugin_dir.parent
    for name in ("_common", "profilers_common"):
        candidate = base / name
        if (candidate / "capture_runtime.py").is_file():
            return candidate
    raise AssertionError("capture_runtime.py not found via either shim name")  # noqa: TRY003


@pytest.mark.parametrize(
    ("plugin_rel", "common_name"),
    [
        ("resources/profilers/rocprof", "_common"),
        ("rocprof_profiler", "profilers_common"),
    ],
    ids=["repo_checkout", "staged_workspace"],
)
def test_capture_runtime_importable_via_documented_shim(
    tmp_path: Path, plugin_rel: str, common_name: str
) -> None:
    plugin_dir = tmp_path / plugin_rel
    plugin_dir.mkdir(parents=True)
    common_dir = plugin_dir.parent / common_name
    common_dir.mkdir(parents=True)
    shutil.copy2(_MODULE_PATH, common_dir / "capture_runtime.py")

    found = _find_common_dir(plugin_dir)
    assert found == common_dir

    module = _load_module(f"capture_runtime_{common_name}", found / "capture_runtime.py")
    assert module.CaptureStatus.OK.value == "ok"
    assert module.Lifecycle(command="true").timeout_s == 300.0


# -- hypothesis: lifecycle invariants --------------------------------------------


@given(timeout_s=st.floats(min_value=0.1, max_value=0.5))
@PROC_SETTINGS
def test_property_no_load_timeout_bound_and_no_leaks(
    tmp_path_factory: pytest.TempPathFactory, timeout_s: float
) -> None:
    tmp_path = tmp_path_factory.mktemp("cr")
    pidfile = tmp_path / "target.pid"
    lifecycle = cr.Lifecycle(command=f"echo $$ > {pidfile}; sleep 100", timeout_s=timeout_s)

    start = time.monotonic()
    result = cr.run_capture([], lifecycle, kind="unit", out_dir=tmp_path / "c", meta={})
    elapsed = time.monotonic() - start

    # Consistency: the runtime only reports TIMED_OUT here (the target never
    # exits on its own within any of the fuzzed timeouts).
    assert result.status is cr.CaptureStatus.TIMED_OUT
    assert result.escalated is True
    assert elapsed <= timeout_s + 6.0
    assert result.manifest_path.is_file()
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == result.status.value

    pid = _read_pid(pidfile)
    assert _wait_until(lambda: not _is_alive(pid))


@given(grace_s=st.floats(min_value=0.1, max_value=0.4), ignore_stop=st.booleans())
@PROC_SETTINGS
def test_property_load_grace_bound_status_consistent_no_leaks(
    tmp_path_factory: pytest.TempPathFactory,
    grace_s: float,
    *,
    ignore_stop: bool,
) -> None:
    # Reads the module-level source directly (not a fixture): hypothesis
    # flags function-scoped fixtures under @given as a health-check risk.
    tmp_path = tmp_path_factory.mktemp("cr")
    out_dir = tmp_path / "c"
    out_dir.mkdir()
    fake_profiler = tmp_path / "fake_profiler.py"
    fake_profiler.write_text(_FAKE_PROFILER_SOURCE)

    command = "trap '' INT; sleep 100" if ignore_stop else "sleep 100"
    env = {"FAKE_PROFILER_OUT_DIR": str(out_dir)}
    if ignore_stop:
        env["FAKE_PROFILER_IGNORE_SIGINT"] = "1"
    lifecycle = cr.Lifecycle(
        command=command,
        env=env,
        ready_command=f"test -f {out_dir}/profiler.pid",
        ready_timeout_s=5.0,
        ready_interval_s=0.02,
        load_command="true",
        grace_s=grace_s,
        timeout_s=5.0,
    )

    start = time.monotonic()
    result = cr.run_capture(
        [sys.executable, str(fake_profiler)], lifecycle, kind="unit", out_dir=out_dir, meta={}
    )
    elapsed = time.monotonic() - start

    expected_status = cr.CaptureStatus.KILLED_AFTER_GRACE if ignore_stop else cr.CaptureStatus.OK
    assert result.status is expected_status
    assert result.escalated is ignore_stop
    assert elapsed <= grace_s + 6.0
    assert result.manifest_path.is_file()
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == result.status.value

    pid = int((out_dir / "profiler.pid").read_text().strip())
    assert _wait_until(lambda: not _is_alive(pid))
