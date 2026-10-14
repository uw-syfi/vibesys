"""The shared capture lifecycle (``resources/profilers/_common/capture_runtime.py``).

Guarantees under test:

* Outcome mapping. A bounded target is ``ok`` or ``target_failed`` by its exit status and
  ``timed_out`` (killed) when it outlives the budget. A server capture is ``not_ready`` when
  its readiness check never passes, ``load_failed`` or ``timed_out`` by the load's fate,
  ``ok`` when the target flushes within the stop grace and ``killed_after_grace`` (escalated)
  when it ignores the stop signal.
* Cleanliness. Whatever the outcome, no process of the capture is left running, descendants
  included, and a manifest recording the status is written.
* ``setup_command`` runs first, to completion, and never under the profiler prefix; a failed
  or timed-out setup starts no target.
* ``cancel_event`` interrupts a bounded target or a load and yields ``cancelled``.
* The capture slot admits one capture at a time and always names its holder.
* Scripts are written verbatim and executable, and recorded in the manifest; env values are
  redacted; capture and load windows are stamped in order.
* The capture store resolves absolute roots, unique ids, ids and paths, newest first.

The lifecycle runs on a scripted ``ProcessHost`` (``tests/support/profiler_process_world.py``)
and a virtual clock, so no test spawns a process, signals one or sleeps. That the real host
behaves the same is checked once in ``tests/e2e/test_profiler_capture_processes_e2e.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.profiler_process_world import (
    ScriptedHost,
    always,
    exits,
    serves,
)

from vs_sim.api import OsThreads
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType, SimpleNamespace

    from tests.support.profiler_process_world import FakeProcess, Script

_REPO = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO / "resources" / "profilers" / "_common" / "capture_runtime.py"

FAST = settings(max_examples=examples(pr=6, nightly=100), deadline=None)
SCENARIO = settings(
    max_examples=examples(pr=5, nightly=60),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


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


# NOT registered under the bare "capture_runtime" name: that name is claimed by
# test_rocprof_capture.py so capture.py's own `import capture_runtime` resolves to the object
# its tests use. This file needs only a private instance under a name nothing else claims.
cr = _load_module("_test_capture_runtime_standalone", _MODULE_PATH)

_THREADS = OsThreads()

TARGET = "serve-forever"
READY = "check-ready"
LOAD = "drive-load"
SETUP = "prepare"


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / ".profiles"
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(directory))
    return directory


@pytest.fixture
def host() -> Iterator[ScriptedHost]:
    """A scripted host installed for the test; leaves nothing running when it ends."""
    scripted = ScriptedHost()
    with cr.use_host(scripted):
        yield scripted


def _capture(
    tmp_path: Path,
    lifecycle: SimpleNamespace,
    *,
    prefix: list[str] | None = None,
    meta: dict[str, str] | None = None,
    cancel_event: object | None = None,
) -> SimpleNamespace:
    return cr.run_capture(
        prefix or [],
        lifecycle,
        kind="unit",
        out_dir=tmp_path / "c",
        meta=meta or {},
        cancel_event=cancel_event,
    )


def _server(**overrides: object) -> SimpleNamespace:
    """A server lifecycle: TARGET serves until stopped, READY gates LOAD."""
    fields: dict[str, object] = {
        "command": TARGET,
        "ready_command": READY,
        "ready_timeout_s": 5.0,
        "ready_interval_s": 0.05,
        "load_command": LOAD,
        "grace_s": 1.0,
        "timeout_s": 60.0,
    }
    fields.update(overrides)
    return cr.Lifecycle(**fields)


def _assert_clean(host: ScriptedHost, result: SimpleNamespace) -> None:
    assert not host.alive_pids(), "a capture left processes running"
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == result.status.value


# -- outcomes: a bounded target (no load_command) ---------------------------------------


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (exits(0.1, 0), ("OK", 0, False)),
        (exits(0.1, 7), ("TARGET_FAILED", 7, False)),
        (serves(), ("TIMED_OUT", None, True)),
    ],
    ids=["ok", "target_failed", "timed_out"],
)
def test_a_bounded_target_is_judged_by_its_exit_and_never_outlives_its_budget(
    host: ScriptedHost, tmp_path: Path, script: Script, expected: tuple[str, int | None, bool]
) -> None:
    status, returncode, escalated = expected
    host.scripts[TARGET] = script
    result = _capture(tmp_path, cr.Lifecycle(command=TARGET, timeout_s=5.0))

    assert result.status is cr.CaptureStatus[status]
    if returncode is not None:
        assert result.target_returncode == returncode
    assert result.escalated is escalated
    _assert_clean(host, result)
    assert result.manifest_path.is_file()


@given(timeout_s=st.floats(min_value=0.1, max_value=50.0))
@SCENARIO
def test_a_target_outliving_any_budget_is_killed_when_the_budget_ends(
    tmp_path_factory: pytest.TempPathFactory, timeout_s: float
) -> None:
    host = ScriptedHost()
    host.scripts[TARGET] = serves()
    with cr.use_host(host):
        result = _capture(
            tmp_path_factory.mktemp("cr"), cr.Lifecycle(command=TARGET, timeout_s=timeout_s)
        )

    assert result.status is cr.CaptureStatus.TIMED_OUT
    assert host.clock.now == pytest.approx(timeout_s, abs=0.5)
    _assert_clean(host, result)


def test_a_timed_out_target_takes_its_detached_descendants_with_it(
    host: ScriptedHost, tmp_path: Path
) -> None:
    """Escalation owns the whole tree, since a child may start its own session."""
    grandchildren: list[FakeProcess] = []

    def target(proc: FakeProcess) -> None:
        grandchildren.append(proc.spawn_child(serves()))

    host.scripts[TARGET] = target
    result = _capture(tmp_path, cr.Lifecycle(command=TARGET, timeout_s=1.0))

    assert result.status is cr.CaptureStatus.TIMED_OUT
    assert grandchildren
    _assert_clean(host, result)


# -- outcomes: a server capture (load_command) ------------------------------------------


def test_a_target_that_never_becomes_ready_is_not_ready_and_is_stopped(
    host: ScriptedHost, tmp_path: Path
) -> None:
    host.scripts[TARGET] = serves()
    host.checks[READY] = always(1)
    result = _capture(tmp_path, _server(ready_timeout_s=0.5))

    assert result.status is cr.CaptureStatus.NOT_READY
    assert result.ready_achieved is False
    assert result.load_returncode is None
    assert LOAD not in {s.script_text for s in host.spawned}
    _assert_clean(host, result)


def test_a_failing_load_is_reported_with_its_status_and_the_target_is_stopped(
    host: ScriptedHost, tmp_path: Path
) -> None:
    host.scripts[TARGET] = serves()
    host.checks[READY] = always(0)
    host.scripts[LOAD] = exits(0.1, 3)
    result = _capture(tmp_path, _server())

    assert result.status is cr.CaptureStatus.LOAD_FAILED
    assert result.ready_achieved is True
    assert result.load_returncode == 3
    _assert_clean(host, result)


def test_a_load_outliving_the_budget_times_out_the_capture(
    host: ScriptedHost, tmp_path: Path
) -> None:
    host.scripts[TARGET] = serves()
    host.checks[READY] = always(0)
    host.scripts[LOAD] = serves()
    result = _capture(tmp_path, _server(timeout_s=0.5))

    assert result.status is cr.CaptureStatus.TIMED_OUT
    assert result.ready_achieved is True
    assert result.load_returncode is None
    _assert_clean(host, result)


def test_a_target_that_flushes_within_the_grace_is_ok_and_not_escalated(
    host: ScriptedHost, tmp_path: Path
) -> None:
    flushed: list[str] = []
    host.scripts[TARGET] = serves(
        stop_after_s=0.3, on_stop=lambda proc: flushed.append(proc.env.get("TRACE", "trace"))
    )
    host.checks[READY] = always(0)
    host.scripts[LOAD] = exits(0.1)
    result = _capture(tmp_path, _server(grace_s=5.0))

    assert result.status is cr.CaptureStatus.OK
    assert result.escalated is False
    assert flushed == ["trace"]
    _assert_clean(host, result)


def test_a_target_ignoring_the_stop_signal_is_killed_after_the_grace(
    host: ScriptedHost, tmp_path: Path
) -> None:
    host.scripts[TARGET] = serves(ignores=("SIGINT",))
    host.checks[READY] = always(0)
    host.scripts[LOAD] = exits(0.1)
    result = _capture(tmp_path, _server(grace_s=0.3))

    assert result.status is cr.CaptureStatus.KILLED_AFTER_GRACE
    assert result.escalated is True
    _assert_clean(host, result)


@given(
    grace_s=st.floats(min_value=0.1, max_value=20.0),
    ignore_stop=st.booleans(),
    slack_s=st.floats(min_value=0.01, max_value=0.05),
)
@SCENARIO
def test_the_grace_decides_ok_against_killed_and_is_never_overspent(
    tmp_path_factory: pytest.TempPathFactory, grace_s: float, slack_s: float, *, ignore_stop: bool
) -> None:
    """A target exiting strictly inside the grace is ok; one ignoring the stop is killed.

    Time is virtual, so host load cannot flip the verdict, and the time spent waiting after
    the stop signal never exceeds the grace.
    """
    host = ScriptedHost()
    load_ended: list[float] = []
    host.scripts[TARGET] = (
        serves(ignores=("SIGINT",)) if ignore_stop else serves(stop_after_s=grace_s - slack_s)
    )
    host.checks[READY] = always(0)

    def load(proc: FakeProcess) -> None:
        proc.on_exit(lambda: load_ended.append(host.clock.now))
        exits(0.1)(proc)

    host.scripts[LOAD] = load
    with cr.use_host(host):
        result = _capture(tmp_path_factory.mktemp("cr"), _server(grace_s=grace_s))

    expected = cr.CaptureStatus.KILLED_AFTER_GRACE if ignore_stop else cr.CaptureStatus.OK
    assert result.status is expected
    assert result.escalated is ignore_stop
    assert host.clock.now - load_ended[0] <= grace_s + 1e-9
    _assert_clean(host, result)


# -- setup_command: an unprofiled step before the target ---------------------------------


def test_setup_runs_to_completion_before_the_target_and_outside_the_profiler(
    host: ScriptedHost, tmp_path: Path
) -> None:
    order: list[str] = []

    def record(name: str, script: Script) -> Script:
        def run(proc: FakeProcess) -> None:
            order.append(name)
            script(proc)

        return run

    host.scripts[SETUP] = record("setup", exits(0.5, output="setup-output-marker\n"))
    host.scripts[TARGET] = record("target", exits(0.1))
    lifecycle = cr.Lifecycle(command=TARGET, setup_command=SETUP, timeout_s=5.0)

    result = _capture(tmp_path, lifecycle, prefix=["profiler", "--flag"])

    assert result.status is cr.CaptureStatus.OK
    assert order == ["setup", "target"]
    assert result.setup_returncode == 0
    by_text = {s.script_text: s for s in host.spawned}
    assert by_text[SETUP].profiler_prefix == []
    assert by_text[TARGET].profiler_prefix == ["profiler", "--flag"]
    assert "setup-output-marker" in (result.setup_log_tail or "")
    assert "setup log tail" in cr.format_result(result)


@given(
    prefix=st.lists(
        st.text(alphabet="abcdefgh-_./", min_size=1, max_size=12), min_size=1, max_size=4
    )
)
@SCENARIO
def test_setup_never_runs_inside_any_profiler_prefix(
    tmp_path_factory: pytest.TempPathFactory, prefix: list[str]
) -> None:
    """A profiler injects its environment into everything it wraps; setup must stay outside it."""
    host = ScriptedHost()
    host.scripts[SETUP] = exits(0.1)
    host.scripts[TARGET] = exits(0.1)
    with cr.use_host(host):
        result = _capture(
            tmp_path_factory.mktemp("cr"),
            cr.Lifecycle(command=TARGET, setup_command=SETUP, timeout_s=5.0),
            prefix=prefix,
        )

    assert result.status is cr.CaptureStatus.OK
    by_text = {s.script_text: s for s in host.spawned}
    assert by_text[SETUP].argv[:-1] == ["bash"]
    assert by_text[TARGET].argv[: len(prefix)] == prefix


@pytest.mark.parametrize(
    ("setup", "status", "setup_rc"),
    [(exits(0.1, 9), "SETUP_FAILED", 9), (serves(), "TIMED_OUT", None)],
    ids=["failed", "timed_out"],
)
def test_a_setup_that_fails_or_hangs_starts_no_target(
    host: ScriptedHost, tmp_path: Path, setup: Script, status: str, setup_rc: int | None
) -> None:
    host.scripts[SETUP] = setup
    host.scripts[TARGET] = exits(0.1)
    result = _capture(tmp_path, cr.Lifecycle(command=TARGET, setup_command=SETUP, timeout_s=1.0))

    assert result.status is cr.CaptureStatus[status]
    assert result.target_returncode is None
    assert TARGET not in {s.script_text for s in host.spawned}
    if setup_rc is not None:
        assert result.setup_returncode == setup_rc
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == result.status.value
    assert manifest["target_returncode"] is None
    assert not host.alive_pids()


# -- cancellation -------------------------------------------------------------------------


def test_cancelling_a_bounded_capture_stops_the_target_and_reports_cancelled(
    host: ScriptedHost, tmp_path: Path
) -> None:
    """``cancelled``, not ``timed_out``, proves the cancel interrupted the wait before the bound."""
    cancel = _THREADS.event()
    host.scripts[TARGET] = serves()
    host.clock.at(2.0, cancel.set)

    result = _capture(tmp_path, cr.Lifecycle(command=TARGET, timeout_s=30.0), cancel_event=cancel)

    assert result.status is cr.CaptureStatus.CANCELLED
    assert result.escalated is True
    assert host.clock.now < 30.0
    _assert_clean(host, result)


def test_cancelling_during_the_load_stops_the_load_and_the_target(
    host: ScriptedHost, tmp_path: Path
) -> None:
    cancel = _THREADS.event()
    host.scripts[TARGET] = serves()
    host.checks[READY] = always(0)
    host.scripts[LOAD] = serves()
    host.clock.at(3.0, cancel.set)

    result = _capture(tmp_path, _server(grace_s=5.0, timeout_s=30.0), cancel_event=cancel)

    assert result.status is cr.CaptureStatus.CANCELLED
    assert host.clock.now < 30.0
    _assert_clean(host, result)


@FAST
@given(
    remaining_s=st.floats(min_value=0, max_value=10000, allow_nan=False),
    load_timeout_s=st.floats(min_value=0.01, max_value=10000, allow_nan=False),
)
def test_load_budget_cannot_spend_unused_startup_time(
    remaining_s: float, load_timeout_s: float
) -> None:
    lifecycle = cr.Lifecycle(command="server", load_timeout_s=load_timeout_s)
    assert lifecycle.load_budget(remaining_s) == min(remaining_s, load_timeout_s)
    assert cr.Lifecycle(command="server").load_budget(remaining_s) == remaining_s


@pytest.mark.parametrize("load_timeout_s", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_load_timeout_names_the_key(load_timeout_s: float) -> None:
    with pytest.raises(ValueError, match="load_timeout_s"):
        cr.Lifecycle(command="server", load_timeout_s=load_timeout_s)


# -- scripts, manifest, windows ---------------------------------------------------------


def test_scripts_are_written_executable_and_recorded_in_the_manifest(
    host: ScriptedHost, tmp_path: Path
) -> None:
    host.scripts[TARGET] = serves()
    host.checks[READY] = always(0)
    host.scripts[LOAD] = exits(0.1)
    result = _capture(tmp_path, _server())

    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["scripts"] == {"target": "target.sh", "ready": "ready.sh", "load": "load.sh"}
    for name in ("target.sh", "ready.sh", "load.sh"):
        script = result.out_dir / name
        assert os.access(script, os.X_OK)
        assert script.read_text().startswith("#!/usr/bin/env bash\n")


def test_a_bounded_capture_writes_only_the_target_script(
    host: ScriptedHost, tmp_path: Path
) -> None:
    host.scripts[TARGET] = exits(0.1)
    result = _capture(tmp_path, cr.Lifecycle(command=TARGET, timeout_s=5.0))

    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["scripts"] == {"target": "target.sh"}
    assert not (result.out_dir / "ready.sh").exists()
    assert not (result.out_dir / "load.sh").exists()
    assert manifest["load_window"] is None


_PRINTABLE_ASCII = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=200
)


@given(content=_PRINTABLE_ASCII)
@SCENARIO
def test_command_text_reaches_the_profiler_wrapper_as_one_script_path_verbatim(
    tmp_path_factory: pytest.TempPathFactory, content: str
) -> None:
    """A wrapper that re-joins and re-splits its trailing argv cannot corrupt the command.

    The command is written byte for byte after the shebang, and the wrapper is handed only
    ``bash <script>``: nothing in the wrapped argv for it to mangle.
    """
    text = f"cat <<'VIBESYS_EOF'\n{content}\nVIBESYS_EOF\n"
    host = ScriptedHost()
    host.scripts[text] = exits(0.1)
    with cr.use_host(host):
        result = _capture(
            tmp_path_factory.mktemp("cr"),
            cr.Lifecycle(command=text, timeout_s=5.0),
            prefix=["wrapper", "--opt"],
        )

    assert (result.out_dir / "target.sh").read_text() == "#!/usr/bin/env bash\n" + text
    (spawn,) = host.spawned
    assert spawn.argv == ["wrapper", "--opt", "bash", str(result.out_dir / "target.sh")]


def test_the_manifest_records_status_identity_and_meta(host: ScriptedHost, tmp_path: Path) -> None:
    host.scripts[TARGET] = exits(0.1, output="hello\n")
    result = _capture(
        tmp_path,
        cr.Lifecycle(command=TARGET, timeout_s=5.0),
        meta={"tool_version": "1.2.3"},
    )

    data = json.loads(result.manifest_path.read_text())
    assert data["status"] == "ok"
    assert data["capture_id"] == result.capture_id
    assert data["kind"] == "unit"
    assert data["meta"] == {"tool_version": "1.2.3"}
    assert "manifest.json" not in data["output_files"]
    assert "target.log" in data["output_files"]
    text = cr.format_result(result)
    assert "ok" in text
    assert result.capture_id in text
    assert "hello" in text


def test_the_manifest_redacts_env_values_but_not_keys(host: ScriptedHost, tmp_path: Path) -> None:
    secret = "super-secret-token-xyz"  # noqa: S105  # LW-910180; this is a placeholder/test credential, not a real secret
    host.scripts[TARGET] = exits(0.1)
    result = _capture(
        tmp_path, cr.Lifecycle(command=TARGET, env={"MY_TOKEN": secret}, timeout_s=5.0)
    )

    raw = result.manifest_path.read_text()
    assert secret not in raw
    data = json.loads(raw)
    assert data["lifecycle"]["env_keys"] == ["MY_TOKEN"]
    assert "env" not in data["lifecycle"]


_STAMP_KEYS = ("monotonic_ns", "clock_monotonic_ns", "realtime_ns")


@given(load_s=st.floats(min_value=0.01, max_value=5.0))
@SCENARIO
def test_capture_ready_load_and_end_are_stamped_in_order_for_any_load_duration(
    tmp_path_factory: pytest.TempPathFactory, load_s: float
) -> None:
    """capture_start <= ready <= load start <= load end <= capture end."""
    host = ScriptedHost()
    host.scripts[TARGET] = serves()
    host.checks[READY] = always(0)
    host.scripts[LOAD] = exits(load_s)
    with cr.use_host(host):
        result = _capture(tmp_path_factory.mktemp("cr"), _server())

    manifest = json.loads(result.manifest_path.read_text())
    window = manifest["load_window"]
    for stamp in (manifest["capture_start"], manifest["capture_end"], *window.values()):
        assert all(stamp[key] > 0 for key in _STAMP_KEYS)
    order = [
        manifest["capture_start"]["clock_monotonic_ns"],
        window["ready"]["clock_monotonic_ns"],
        window["start"]["clock_monotonic_ns"],
        window["end"]["clock_monotonic_ns"],
        manifest["capture_end"]["clock_monotonic_ns"],
    ]
    assert order == sorted(order)


def test_a_capture_runs_in_the_directory_it_was_given_whatever_the_servers_cwd(
    host: ScriptedHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capture directory is absolute, so a target started elsewhere writes where we read."""
    server_cwd = tmp_path / "server_launch_dir"
    server_cwd.mkdir()
    target_cwd = tmp_path / "target_cwd"
    target_cwd.mkdir()
    monkeypatch.chdir(server_cwd)
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", "./.profiles")
    _capture_id, out_dir = cr.new_capture("timeline")
    host.scripts[TARGET] = exits(0.1)

    result = cr.run_capture(
        ["profiler", "-d", str(out_dir)],
        cr.Lifecycle(command=TARGET, cwd=str(target_cwd), timeout_s=5.0),
        kind="timeline",
        out_dir=out_dir,
        meta={},
    )

    assert out_dir.is_absolute()
    assert result.out_dir == out_dir
    (spawn,) = host.spawned
    assert spawn.launch.cwd == str(target_cwd)
    assert "-d" in spawn.argv
    assert Path(spawn.argv[spawn.argv.index("-d") + 1]).is_absolute()


# -- exclusive_capture / CaptureBusyError: one capture at a time per process ------------


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
    max_examples=examples(pr=4, nightly=50),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
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
        os.utime(directory, (1_000_000 + len(ids), 1_000_000 + len(ids)))  # distinct mtimes

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
    raise AssertionError("capture_runtime.py not found via either shim name")  # noqa: TRY003  # LW-920207; this is a boundary error that deliberately embeds the offending value for the operator to act on


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
