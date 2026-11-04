"""A brokered rocprof capture from a candidate workspace, over a real broker socket.

Guarantee: a capture requested from inside a candidate worktree reaches the scheduler (an
``sbatch`` is issued through the real Slurm process broker), and cancelling it cancels the
job. The bridge's own logic is covered in-process, against a fake cluster, in
``tests/vibesys/loops/test_rocprof_remote_bridge.py``; this is the one test that puts a real
broker socket and a real connector process under it.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from resources.profilers.rocprof.remote_bridge import RemoteCaptureBridge

from vs_sandbox.api.slurm import SlurmCapturePlan, SlurmProcessBroker, write_slurm_capture_plan
from vs_sim.api import MonotonicClock, OsProcesses
from vs_sim.api.testing import HANG_GUARD_S, start_thread
from vs_slurm.api import SlurmError, load_slurm_config, slurm_transport_broker_policy
from vs_slurm.fake_connector import JOB_ID, SUBMITTED_FILE, recorded_commands

if TYPE_CHECKING:
    from pathlib import Path


def _cancel() -> threading.Event:
    """The bridge is stdlib-only and takes a ``threading.Event`` token."""
    return threading.Event()


@dataclass
class _Lifecycle:
    command: str = "python serve.py"
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    ready_command: str | None = None
    ready_timeout_s: float = 10.0
    ready_interval_s: float = 0.1
    load_command: str | None = "python load.py"
    load_timeout_s: float | None = None
    setup_command: str | None = None
    stop_signal: str = "SIGINT"
    grace_s: float = 2.0
    timeout_s: float = 30.0


@pytest.mark.parametrize("transport", ["ssh", "connector"])
def test_a_brokered_capture_from_a_candidate_workspace_reaches_the_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    """Regression: every brokered capture failed before sbatch.

    The runner wrote its job script under the system temporary directory, and
    the broker refuses to transfer a file outside the run-owned roots, so a
    capture from a candidate worktree never reached the scheduler.
    """
    cluster = tmp_path / "cluster"
    cluster.mkdir()
    os.mkfifo(cluster / SUBMITTED_FILE)
    program = f'["{sys.executable}", "-m", "vs_slurm.fake_connector", "{cluster}"]'
    program_tail = program.removesuffix("]")
    config_path = tmp_path / "slurm.toml"
    transport_table = (
        f"""kind = "ssh"
host = "cluster"
ssh_command = {program_tail}, "ssh"]
rsync_command = {program_tail}, "rsync"]"""
        if transport == "ssh"
        else f"""kind = "connector"
command = {program}"""
    )
    config_path.write_text(
        f"""[slurm]
name = "test-cluster"
remote_workspace_root = "/remote/runs"
poll_interval_seconds = 0.01

[slurm.transport]
{transport_table}

[vibesys]
remote_python = "/remote/venv/bin/python"
""",
        encoding="utf-8",
    )
    worktrees = tmp_path / "worktrees"
    workspace = worktrees / "candidate" / "workspace"
    workspace.mkdir(parents=True)
    config = load_slurm_config(config_path)
    broker = SlurmProcessBroker(
        slurm_transport_broker_policy(config, local_roots=(worktrees, tmp_path / "cluster-state")),
        tmp_path / "broker.sock",
        processes=OsProcesses(),
    )
    broker.start()
    monkeypatch.setenv("VIBESYS_SLURM_BROKER_SOCKET", str(broker.socket_path))
    monkeypatch.setenv("VIBESYS_SLURM_BROKER_TOKEN", broker.token)
    plan_path = tmp_path / "capture-plan.json"
    state_root = tmp_path / "cluster-state"
    write_slurm_capture_plan(plan_path, SlurmCapturePlan(cluster_state_root=state_root))
    bridge = RemoteCaptureBridge(
        config_path,
        workspace,
        profile_root=tmp_path / "profiles",
        evaluator_plan=plan_path,
        clock=MonotonicClock(),
    )
    cancel = _cancel()
    failures: list[BaseException] = []

    def capture() -> None:
        try:
            bridge.capture("stats", _Lifecycle(), {}, cancel_event=cancel)
        except (SlurmError, PermissionError) as error:
            failures.append(error)
        # Unblock the reader when the capture ended before any job was submitted.
        with contextlib.suppress(OSError):
            descriptor = os.open(cluster / SUBMITTED_FILE, os.O_WRONLY | os.O_NONBLOCK)
            os.write(descriptor, b"none")
            os.close(descriptor)

    worker = start_thread(capture)
    try:
        submitted = (cluster / SUBMITTED_FILE).read_text(encoding="utf-8")
        cancel.set()
        worker.result(HANG_GUARD_S)
    finally:
        broker.close()

    assert submitted == JOB_ID, [str(item) for item in failures]
    assert f"scancel {JOB_ID}" in recorded_commands(cluster)
