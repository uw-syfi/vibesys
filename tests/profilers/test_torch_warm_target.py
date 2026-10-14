"""Warm-target profiling: ``capture_runtime`` targets and ``capture_ops.profile_ops(target=...)``.

Guarantees under test:

* ``start_target`` returns only once the target reports ready, registers it, does not hold the
  capture slot, and leaves it running; ``stop_target`` / ``stop_all_targets`` leave no process
  of any target running and an empty registry (the latter idempotent); an unknown id is a
  ``KeyError``.
* Each ``profile_ops(target=...)`` window yields exactly one trace for that window (windows are
  numbered 1, 2, ...), leaves the target running and reusable, and reports the primary trace.
* The load starts only after the target acknowledged that recording began, never before.
* A target that can never take a window (no GPU) is reported as such, with its reason.
* A window is refused with ``CaptureBusyError`` while another capture holds the slot, and
  succeeds once the slot is free.
* Misuse is a clear error: no ``load_command``, an unknown target, neither command nor target.
* Over any sequence of target lifetimes with any number of windows each, traces equal windows
  and nothing is left running.

The target is a scripted process speaking the injection's file protocol on a virtual clock
(``tests/support/profiler_process_world.py``); that the real injection speaks the same protocol
is checked against real processes in ``tests/e2e/test_profiler_capture_processes_e2e.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.profiler_process_world import (
    ScriptedHost,
    exits,
    torch_target,
)

from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from tests.support.profiler_process_world import FakeProcess

_REPO = Path(__file__).resolve().parents[2]
_TORCH_DIR = _REPO / "resources" / "profilers" / "torch"
_TRACE_JSON = (
    Path(__file__).parent / "fixtures" / "torch" / "eager_trimmed.pt.trace.json"
).read_bytes()

SERVER = "serve-model"
LOAD = "drive-load"


def _load_capture_ops(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(_TORCH_DIR / "capture_ops.py"))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    inserted = str(_TORCH_DIR) not in sys.path
    if inserted:
        sys.path.insert(0, str(_TORCH_DIR))
    try:
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(str(_TORCH_DIR))
    return module


@pytest.fixture(scope="module")
def capture_ops() -> ModuleType:
    return _load_capture_ops("_capture_ops_warm_target")


@pytest.fixture
def world(
    capture_ops: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ScriptedHost]:
    """A scripted host with an isolated capture store, installed for the test."""
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path / ".profiles"))
    host = ScriptedHost(file_checks=True)
    host.scripts[LOAD] = exits(0.2)
    runtime = capture_ops.capture_runtime
    with runtime.use_host(host):
        yield host
        runtime.stop_all_targets()


def _traces(capture_ops: ModuleType) -> list[Path]:
    return sorted(capture_ops.capture_runtime.profiles_root().glob("**/*.pt.trace.json.gz"))


def _start(
    capture_ops: ModuleType, host: ScriptedHost, **target: object
) -> tuple[str, FakeProcess]:
    host.scripts[SERVER] = torch_target(_TRACE_JSON, **target)  # ty: ignore[invalid-argument-type]
    target_id = capture_ops.start_target(SERVER)
    info = capture_ops.capture_runtime.get_target(target_id)
    return target_id, host.processes[info.pid]


def test_a_started_target_is_registered_ready_and_running_until_stopped(
    capture_ops: ModuleType, world: ScriptedHost
) -> None:
    runtime = capture_ops.capture_runtime
    target_id, proc = _start(capture_ops, world)

    info = runtime.get_target(target_id)
    assert info.alive
    assert info.ready_achieved
    assert runtime.active_capture() is None  # starting is not a capture
    assert [t.target_id for t in runtime.list_targets()] == [target_id]

    runtime.stop_target(target_id)
    assert not proc.alive
    assert runtime.list_targets() == []
    with pytest.raises(KeyError):
        runtime.stop_target(target_id)
    with pytest.raises(KeyError):
        runtime.stop_target("does-not-exist")


def test_stop_all_targets_stops_every_target_and_is_idempotent(
    capture_ops: ModuleType, world: ScriptedHost
) -> None:
    runtime = capture_ops.capture_runtime
    procs = [_start(capture_ops, world)[1] for _ in range(2)]
    assert all(p.alive for p in procs)

    runtime.stop_all_targets()
    assert not world.alive_pids()
    assert runtime.list_targets() == []
    runtime.stop_all_targets()


def test_consecutive_windows_each_yield_their_own_trace_and_keep_the_target_up(
    capture_ops: ModuleType, world: ScriptedHost
) -> None:
    target_id, proc = _start(capture_ops, world)

    for _ in range(2):
        out = capture_ops.profile_ops(target=target_id, load_command=LOAD, duration_s=10)
        assert "primary trace" in out, out
        assert proc.alive

    traces = _traces(capture_ops)
    assert sorted(int(p.name.split("-")[1].split(".")[0]) for p in traces) == [1, 2]


def test_the_load_starts_only_after_recording_started(
    capture_ops: ModuleType, world: ScriptedHost
) -> None:
    """A slow profiler start (seconds on ROCm) must not leave the first of the load unrecorded."""
    events: list[str] = []
    load_script = exits(0.2)

    def load(proc: FakeProcess) -> None:
        events.append("load.begin")
        load_script(proc)

    world.scripts[LOAD] = load
    target_id, _ = _start(capture_ops, world, start_delay_s=1.0, events=events)

    out = capture_ops.profile_ops(target=target_id, load_command=LOAD, duration_s=10)

    assert "primary trace" in out, out
    assert events.index("profile.start") < events.index("load.begin")


def test_a_target_with_no_gpu_reports_why_no_window_can_be_taken(
    capture_ops: ModuleType, world: ScriptedHost
) -> None:
    target_id, _ = _start(capture_ops, world, gpu=False)

    with pytest.raises(RuntimeError) as failed:
        capture_ops.profile_ops(target=target_id, load_command=LOAD, duration_s=10)

    assert type(failed.value).__name__ == "CaptureFailedError"
    assert "cannot take a torch.profiler window" in str(failed.value)
    assert "no GPU" in str(failed.value)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"target": "whatever"}, "requires load_command"),
        ({"target": "does-not-exist", "load_command": LOAD}, "does-not-exist"),
        ({}, "requires either command="),
    ],
)
@pytest.mark.usefixtures("world")
def test_misuse_is_a_clear_error(
    capture_ops: ModuleType, kwargs: dict[str, str], message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        capture_ops.profile_ops(**kwargs)


def test_a_window_is_busy_while_another_capture_holds_the_slot_and_works_after(
    capture_ops: ModuleType, world: ScriptedHost
) -> None:
    runtime = capture_ops.capture_runtime
    target_id, _ = _start(capture_ops, world)

    with (
        runtime.exclusive_capture("ops", "some-other-capture"),
        pytest.raises(runtime.CaptureBusyError),
    ):
        capture_ops.profile_ops(target=target_id, load_command=LOAD, duration_s=5)

    assert "primary trace" in capture_ops.profile_ops(
        target=target_id, load_command=LOAD, duration_s=5
    )


@settings(
    max_examples=examples(pr=4, nightly=60),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(window_counts=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=3))
def test_traces_equal_windows_and_nothing_leaks_over_any_target_lifetimes(
    capture_ops: ModuleType, world: ScriptedHost, window_counts: list[int]
) -> None:
    runtime = capture_ops.capture_runtime
    before = len(_traces(capture_ops))
    for count in window_counts:
        target_id, proc = _start(capture_ops, world)
        for _ in range(count):
            assert "primary trace" in capture_ops.profile_ops(
                target=target_id, load_command=LOAD, duration_s=5
            )
        runtime.stop_target(target_id)
        assert not proc.alive

    assert runtime.list_targets() == []
    assert len(_traces(capture_ops)) - before == sum(window_counts)
    assert not world.alive_pids()
