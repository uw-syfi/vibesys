"""The profiler capture lifecycle against real processes, signals and files.

The unit tests (``tests/profilers``) run the lifecycle on a scripted host and a virtual clock.
This is the one place that checks the real host does what the script says it does:

* A bounded target that outlives its budget is killed along with a descendant that started its
  own session (the owned tree is found through ``/proc``, not the process group).
* A server capture whose target traps and flushes on SIGINT within the grace is ``ok``; one
  that ignores SIGINT is escalated to ``killed_after_grace``; neither leaves a process behind.
* ``setup_command`` runs outside the profiler wrapper.
* A warm torch target, started through the real injection, takes two windows and yields one
  trace per window, then stops cleanly.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support.profiler_torch_fixtures import write_fake_torch

from vs_sim.api.testing import HANG_GUARD_S

if TYPE_CHECKING:
    from types import ModuleType

_REPO = Path(__file__).resolve().parents[2]
_COMMON = _REPO / "resources" / "profilers" / "_common"
_TORCH_DIR = _REPO / "resources" / "profilers" / "torch"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    inserted = str(path.parent) not in sys.path
    if inserted:
        sys.path.insert(0, str(path.parent))
    try:
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(str(path.parent))
    return module


@pytest.fixture(scope="module")
def cr() -> ModuleType:
    return _load("_e2e_capture_runtime", _COMMON / "capture_runtime.py")


@pytest.fixture(scope="module")
def capture_ops() -> ModuleType:
    return _load("_e2e_capture_ops", _TORCH_DIR / "capture_ops.py")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _pid(path: Path) -> int:
    return int(path.read_text().strip())


def test_a_timed_out_target_is_killed_with_its_detached_descendant(
    cr: ModuleType, tmp_path: Path
) -> None:
    grandchild = tmp_path / "grandchild.pid"
    command = f"setsid sh -c 'echo $$ > {grandchild}; sleep 100' & while :; do sleep 0.05; done"
    result = cr.run_capture(
        [],
        cr.Lifecycle(command=command, timeout_s=1.0),
        kind="e2e",
        out_dir=tmp_path / "c",
        meta={},
    )

    assert result.status is cr.CaptureStatus.TIMED_OUT
    assert result.escalated is True
    assert grandchild.is_file()
    assert not _alive(_pid(grandchild))


@pytest.mark.parametrize(
    ("command", "status", "escalated"),
    [
        (
            "trap 'echo flushed > {out}/trace.txt; exit 0' INT; while :; do sleep 0.05; done",
            "OK",
            False,
        ),
        ("trap '' INT; while :; do sleep 0.05; done", "KILLED_AFTER_GRACE", True),
    ],
    ids=["flushes_on_sigint", "ignores_sigint"],
)
def test_a_server_capture_is_stopped_with_sigint_within_the_grace_or_killed(
    cr: ModuleType, tmp_path: Path, command: str, status: str, *, escalated: bool
) -> None:
    out_dir = tmp_path / "c"
    out_dir.mkdir()
    pidfile = tmp_path / "target.pid"
    lifecycle = cr.Lifecycle(
        command=f"echo $$ > {pidfile}; " + command.format(out=out_dir),
        ready_command=f"test -f {pidfile}",
        ready_timeout_s=HANG_GUARD_S,
        ready_interval_s=0.02,
        load_command="true",
        grace_s=0.5 if escalated else HANG_GUARD_S,
        timeout_s=HANG_GUARD_S,
    )

    result = cr.run_capture([], lifecycle, kind="e2e", out_dir=out_dir, meta={})

    assert result.status is cr.CaptureStatus[status]
    assert result.escalated is escalated
    assert (out_dir / "trace.txt").exists() is (not escalated)
    assert not _alive(_pid(pidfile))


def test_setup_command_runs_outside_the_profiler_wrapper(cr: ModuleType, tmp_path: Path) -> None:
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text('#!/usr/bin/env bash\nexport WRAPPED=1\nexec "$@"\n')
    wrapper.chmod(0o755)
    seen = tmp_path / "seen.txt"
    lifecycle = cr.Lifecycle(
        command="true",
        setup_command=f'echo "${{WRAPPED:-unwrapped}}" > {seen}',
        timeout_s=HANG_GUARD_S,
    )

    result = cr.run_capture([str(wrapper)], lifecycle, kind="e2e", out_dir=tmp_path / "c", meta={})

    assert result.status is cr.CaptureStatus.OK
    assert seen.read_text() == "unwrapped\n"


def test_a_warm_torch_target_takes_two_windows_and_yields_one_trace_each(
    capture_ops: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path / ".profiles"))
    runtime = capture_ops.capture_runtime
    command = f'{sys.executable} -c "import torch, time; time.sleep({HANG_GUARD_S})"'
    target_id = capture_ops.start_target(command, env={"PYTHONPATH": str(fake_torch)})
    pid = runtime.get_target(target_id).pid
    try:
        for _ in range(2):
            out = capture_ops.profile_ops(target=target_id, load_command="sleep 0.2", duration_s=10)
            assert "primary trace" in out, out
            assert _alive(pid)
    finally:
        runtime.stop_target(target_id)

    assert not _alive(pid)
    traces = sorted(runtime.profiles_root().glob("**/*.pt.trace.json.gz"))
    assert sorted(int(p.name.split("-")[1].split(".")[0]) for p in traces) == [1, 2]
    manifests = [
        json.loads(p.read_text()) for p in runtime.profiles_root().glob("ops-*/manifest.json")
    ]
    assert all(m["status"] == "ok" for m in manifests)
