"""Tests for warm-target (reusable running process) profiling.

Covers ``capture_runtime.start_target``/``stop_target``/``list_targets``/
``signal_target``/``stop_all_targets`` (``resources/profilers/_common/
capture_runtime.py``) and the torch plugin's ``capture_ops.start_target`` +
``profile_ops(target=...)`` (``resources/profilers/torch/capture_ops.py``),
which together let an agent take more than one op-level torch.profiler
window against the same already-running process instead of relaunching it
per capture (see ``docs/contributing/amd-profiler-worklog.md`` and the
``warm_attach.md`` experiment notes it references: on a ROCm build without
rocprofiler-sdk default-attachment support, rocprofv3 --attach cannot do
this at all, but the torch plugin's own signal-window injection can, once
armed for repeated windows).

Exercised end to end via real subprocesses using the fake ``torch`` package
from ``torch_inject_fixtures.py`` (this dev environment has no real torch
installed). The state-machine regression this whole feature depends on
(a second SIGUSR1 must not be silently ignored) has its own dedicated
regression test in ``test_torch_inject.py``
(``test_signal_mode_repeated_windows_produce_two_traces``), verified to
fail against the pre-fix ``sitecustomize.py``; this file assumes that fix
and tests one layer up: the target registry, the control-file wiring, and
``profile_ops(target=...)``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.loops.torch_inject_fixtures import write_fake_torch

if TYPE_CHECKING:
    from types import ModuleType

_REPO = Path(__file__).resolve().parents[3]
_TORCH_DIR = _REPO / "resources" / "profilers" / "torch"

# Real subprocesses + signal round-trips: keep example counts small, same
# rationale as test_torch_inject.py's PROC_SETTINGS.
PROC_SETTINGS = settings(
    max_examples=5, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    parent = str(path.parent)
    inserted = parent not in sys.path
    if inserted:
        sys.path.insert(0, parent)
    try:
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(parent)
    return module


@pytest.fixture
def capture_ops(tmp_path: Path) -> ModuleType:
    """A fresh ``capture_ops`` module per test, with its own capture store.

    Not module-scoped (unlike ``test_torch_capture_ops.py``'s fixture):
    each test needs an isolated ``$VIBESYS_PROFILE_DIR`` so target ids and
    live-target registry state from one test can never leak into another,
    and the target registry lives on the shared ``capture_runtime`` module
    this ``capture_ops`` instance imports (see ``test_capture_runtime.py``'s
    comment on why that module's identity is process-global by design).
    """
    os.environ["VIBESYS_PROFILE_DIR"] = str(tmp_path / ".profiles")
    return _load_module("_capture_ops_warm_target", _TORCH_DIR / "capture_ops.py")


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _target_command(fake_torch: Path, *, sleep_s: float = 30.0) -> tuple[str, dict[str, str]]:
    command = f'{sys.executable} -c "import torch, time; time.sleep({sleep_s})"'
    return command, {"PYTHONPATH": str(fake_torch)}


def _all_trace_files(root: Path) -> list[Path]:
    return sorted(root.glob("**/*.pt.trace.json.gz"))


# ---------------------------------------------------------------------------
# start_target / stop_target lifecycle
# ---------------------------------------------------------------------------


def test_start_target_keeps_process_running_until_stopped(capture_ops, tmp_path: Path) -> None:  # noqa: ANN001  # tracked: #288
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    command, env = _target_command(fake_torch)
    target_id = capture_ops.start_target(command, env=env)

    info = capture_ops.capture_runtime.get_target(target_id)
    assert info.alive
    assert _is_alive(info.pid)

    capture_ops.capture_runtime.stop_target(target_id)
    assert not _is_alive(info.pid)
    assert capture_ops.capture_runtime.list_targets() == []


def test_stop_target_unknown_id_raises_key_error(capture_ops) -> None:  # noqa: ANN001  # tracked: #288
    with pytest.raises(KeyError):
        capture_ops.capture_runtime.stop_target("does-not-exist")


def test_stop_all_targets_stops_every_live_target(capture_ops, tmp_path: Path) -> None:  # noqa: ANN001  # tracked: #288
    """Mirrors what each server.py registers via ``atexit`` on process exit."""
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    pids = []
    for _ in range(2):
        command, env = _target_command(fake_torch)
        target_id = capture_ops.start_target(command, env=env)
        pids.append(capture_ops.capture_runtime.get_target(target_id).pid)

    assert all(_is_alive(pid) for pid in pids)
    capture_ops.capture_runtime.stop_all_targets()
    assert not any(_is_alive(pid) for pid in pids)
    assert capture_ops.capture_runtime.list_targets() == []
    # Idempotent: nothing left to stop, must not raise.
    capture_ops.capture_runtime.stop_all_targets()


# ---------------------------------------------------------------------------
# profile_ops(target=...): repeated windows on the same warm target
# ---------------------------------------------------------------------------


def test_two_consecutive_windows_produce_two_traces_and_target_stays_up(  # noqa: ANN201  # tracked: #288
    capture_ops,  # noqa: ANN001  # tracked: #288
    tmp_path: Path,
):
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    command, env = _target_command(fake_torch)
    target_id = capture_ops.start_target(command, env=env)
    pid = capture_ops.capture_runtime.get_target(target_id).pid

    out1 = capture_ops.profile_ops(target=target_id, load_command="sleep 0.2", duration_s=10)
    assert "primary trace" in out1, out1
    assert _is_alive(pid), "target must still be running after window 1"

    out2 = capture_ops.profile_ops(target=target_id, load_command="sleep 0.2", duration_s=10)
    assert "primary trace" in out2, out2
    assert _is_alive(pid), "target must still be running after window 2"

    traces = _all_trace_files(capture_ops.capture_runtime.profiles_root())
    assert len(traces) == 2, f"expected 2 trace files, got {[p.name for p in traces]}"
    windows = sorted(int(p.name.split("-")[1].split(".")[0]) for p in traces)
    assert windows == [1, 2]

    capture_ops.capture_runtime.stop_target(target_id)
    assert not _is_alive(pid)


def test_profile_ops_target_requires_load_command(capture_ops) -> None:  # noqa: ANN001  # tracked: #288
    out = capture_ops.profile_ops(target="whatever")
    assert "requires load_command" in out


def test_profile_ops_unknown_target_returns_clear_error(capture_ops) -> None:  # noqa: ANN001  # tracked: #288
    out = capture_ops.profile_ops(target="does-not-exist", load_command="true")
    assert "error" in out
    assert "does-not-exist" in out


def test_profile_ops_requires_command_or_target(capture_ops) -> None:  # noqa: ANN001  # tracked: #288
    out = capture_ops.profile_ops()
    assert "requires either command=" in out


# ---------------------------------------------------------------------------
# busy/exclusive interplay between captures and targets
# ---------------------------------------------------------------------------


def test_target_window_is_busy_while_another_capture_holds_the_slot(  # noqa: ANN201  # tracked: #288
    capture_ops,  # noqa: ANN001  # tracked: #288
    tmp_path: Path,
):
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    command, env = _target_command(fake_torch)
    target_id = capture_ops.start_target(command, env=env)

    with (
        capture_ops.capture_runtime.exclusive_capture("ops", "some-other-capture"),
        pytest.raises(capture_ops.capture_runtime.CaptureBusyError),
    ):
        capture_ops.profile_ops(target=target_id, load_command="sleep 0.1", duration_s=5)

    # The slot is free again: a window now succeeds normally.
    out = capture_ops.profile_ops(target=target_id, load_command="sleep 0.1", duration_s=5)
    assert "primary trace" in out, out

    capture_ops.capture_runtime.stop_target(target_id)


def test_starting_a_target_does_not_hold_the_capture_slot(capture_ops, tmp_path: Path) -> None:  # noqa: ANN001  # tracked: #288
    """start_target itself is not a capture: it must not block a concurrent one."""
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    command, env = _target_command(fake_torch)
    target_id = capture_ops.start_target(command, env=env)
    assert capture_ops.capture_runtime.active_capture() is None
    capture_ops.capture_runtime.stop_target(target_id)


# ---------------------------------------------------------------------------
# Property test: random window counts never leak processes; window count ==
# trace count.
# ---------------------------------------------------------------------------


@PROC_SETTINGS
@given(window_counts=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=3))
def test_random_start_window_stop_sequences_leak_nothing(  # noqa: ANN201  # tracked: #288
    tmp_path_factory,  # noqa: ANN001  # tracked: #288
    window_counts: list[int],
):
    """Several target lifetimes, each with a random number of windows.

    For each count in *window_counts*: start a fresh target, take that many
    profile_ops(target=...) windows against it, then stop it. Regardless of
    how many windows a lifetime takes (including zero), stopping it must
    leave no live process, and the total trace count across every lifetime
    must equal the total window count -- the property the repeated-window
    state-machine fix (see test_torch_inject.py's regression test) exists
    to guarantee.
    """
    case_dir = tmp_path_factory.mktemp("warm_target_case")
    os.environ["VIBESYS_PROFILE_DIR"] = str(case_dir / ".profiles")
    capture_ops_mod = _load_module("_capture_ops_warm_target_prop", _TORCH_DIR / "capture_ops.py")
    fake_torch = write_fake_torch(case_dir / "fake_torch")

    total_windows = 0
    pids: list[int] = []
    for count in window_counts:
        command, env = _target_command(fake_torch, sleep_s=30.0)
        target_id = capture_ops_mod.start_target(command, env=env)
        pid = capture_ops_mod.capture_runtime.get_target(target_id).pid
        pids.append(pid)
        for _ in range(count):
            out = capture_ops_mod.profile_ops(
                target=target_id, load_command="sleep 0.1", duration_s=5
            )
            assert "primary trace" in out, out
            total_windows += 1
        capture_ops_mod.capture_runtime.stop_target(target_id)
        assert not _is_alive(pid), f"target pid {pid} leaked after stop_target"

    assert capture_ops_mod.capture_runtime.list_targets() == []
    traces = _all_trace_files(capture_ops_mod.capture_runtime.profiles_root())
    assert len(traces) == total_windows, (
        f"window count ({total_windows}) != trace count ({len(traces)}): {[p.name for p in traces]}"
    )
    assert not any(_is_alive(pid) for pid in pids)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
