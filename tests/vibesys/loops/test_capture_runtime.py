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
import sys
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    from types import ModuleType

_REPO = Path(__file__).resolve().parents[3]
_MODULE_PATH = _REPO / "resources" / "profilers" / "_common" / "capture_runtime.py"

# Real subprocesses + signal round-trips: keep example counts tiny so the
# whole property-test slice stays well under the ~20s budget.
PROC_SETTINGS = settings(max_examples=5, deadline=None)


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


cr = _load_module("capture_runtime", _MODULE_PATH)


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


# -- capture store ---------------------------------------------------------------


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
