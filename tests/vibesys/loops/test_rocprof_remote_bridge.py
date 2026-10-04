from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from threading import Event, Thread
from typing import TYPE_CHECKING

import pytest
from resources.profilers.rocprof.remote_bridge import RemoteCaptureBridge, capture_runtime

from vs_sandbox.api.slurm import (
    SlurmCapturePlan,
    SlurmProcessBroker,
    configured_capture_lifecycle,
    load_slurm_policy,
    write_slurm_capture_plan,
)
from vs_slurm.api import SlurmError, SlurmJobResult, load_slurm_config
from vs_slurm.fake_connector import JOB_ID, SUBMITTED_FILE, recorded_commands

if TYPE_CHECKING:
    from pathlib import Path

    from vs_slurm.api import SlurmJobRequest


_STATUS = capture_runtime.CaptureStatus


class _FakeJobRunner:
    """A remote capture job: one capture with the manifest production writes."""

    def __init__(self, status: str = "ok") -> None:
        self.requests: list[SlurmJobRequest] = []
        self.status = status

    def run(self, request: SlurmJobRequest) -> SlurmJobResult:
        self.requests.append(request)
        result = request.file_artifacts[0].local_path
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_text(
            json.dumps(
                {
                    "output": "captured at /remote/profiles/capture-1",
                    "capture_ids": ["capture-1"],
                    "profiles_path": "/remote/profiles",
                }
            ),
            encoding="utf-8",
        )
        profile = request.tree_artifacts[0].local_path / "capture-1"
        profile.mkdir(parents=True)
        (profile / "results.csv").write_text("kernel,duration\n", encoding="utf-8")
        (profile / "manifest.json").write_text(
            json.dumps({"capture_id": "capture-1", "status": self.status, "load_returncode": 0}),
            encoding="utf-8",
        )
        # The remote job exits 0 for every capture it ran, whatever its status.
        return SlurmJobResult(job_id="42", exit_code=0, output="")


class _BlockingJobRunner(_FakeJobRunner):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def run(self, request: SlurmJobRequest) -> SlurmJobResult:
        self.entered.set()
        self.release.wait()
        return super().run(request)


class _SubmissionError(RuntimeError):
    pass


class _FailOnceJobRunner(_FakeJobRunner):
    def __init__(self) -> None:
        super().__init__()
        self._failed = False

    def run(self, request: SlurmJobRequest) -> SlurmJobResult:
        if not self._failed:
            self._failed = True
            raise _SubmissionError
        return super().run(request)


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


def _config_path(tmp_path: Path) -> Path:
    config_path = tmp_path / "slurm.toml"
    config_path.write_text(
        """[slurm]
name = "test-cluster"
remote_workspace_root = "/remote/runs"
job_timeout_seconds = 1800

[slurm.transport]
kind = "ssh"
host = "cluster"

[vibesys]
remote_python = "/remote/venv/bin/python"
setup_script = "/remote/setup.sh"
benchmark_arguments = ["--base-url", "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/v1"]

[vibesys.service]
command = ["python", "-m", "engine.server", "--port", "VIBESYS_DYNAMIC_PORT"]
readiness_url = "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/health"
startup_timeout_seconds = 700
""",
        encoding="utf-8",
    )
    return config_path


def _configured_bridge(tmp_path: Path, runner: _FakeJobRunner) -> RemoteCaptureBridge:
    config_path = _config_path(tmp_path)
    plan_path = tmp_path / "evaluation-plan.json"
    write_slurm_capture_plan(
        plan_path,
        SlurmCapturePlan(
            profile_command=(
                "python",
                "profile.py",
                "--base-url",
                "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/v1",
            ),
            support_paths={},
        ),
    )
    workspace = tmp_path / "configured-workspace"
    workspace.mkdir()
    return RemoteCaptureBridge(
        config_path,
        workspace,
        profile_root=tmp_path / "configured-profiles",
        evaluator_plan=plan_path,
        runner=runner,
    )


def test_configured_lifecycle_uses_one_dynamic_port_and_declared_bounds(tmp_path: Path) -> None:
    bridge = _configured_bridge(tmp_path, _FakeJobRunner())

    recipe = bridge.configured_lifecycle()

    assert recipe is not None
    assert recipe["ready_timeout_s"] == 700.0
    assert recipe["grace_s"] == 120.0
    timeout_s = recipe["timeout_s"]
    assert isinstance(timeout_s, float)
    assert timeout_s == 1680.0
    assert timeout_s < 1800.0
    assert "bind" in str(recipe["setup_command"])
    assert ".vibesys-profile-port" in str(recipe["setup_command"])
    assert "read -r PORT" in str(recipe["command"])
    assert "${PORT}" in str(recipe["command"])
    assert "read -r PORT" in str(recipe["ready_command"])
    assert "/remote/venv/bin/python" in str(recipe["ready_command"])
    assert "urllib.request.urlopen" in str(recipe["ready_command"])
    assert "curl" not in str(recipe["ready_command"])
    assert "127.0.0.1:" in str(recipe["ready_command"])
    assert "${PORT}" in str(recipe["ready_command"])
    assert "/health" in str(recipe["ready_command"])
    assert "read -r PORT" in str(recipe["load_command"])
    assert "${PORT}" in str(recipe["load_command"])
    assert "/v1" in str(recipe["load_command"])


def _bridge(tmp_path: Path, runner: _FakeJobRunner) -> RemoteCaptureBridge:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return RemoteCaptureBridge(
        _config_path(tmp_path),
        workspace,
        profile_root=tmp_path / "profiles",
        runner=runner,
    )


def test_remote_capture_uses_configured_python_and_setup_script(tmp_path: Path) -> None:
    profile_root = tmp_path / "profiles"
    runner = _FakeJobRunner()
    bridge = _bridge(tmp_path, runner)

    output = bridge.capture("stats", _Lifecycle(), {}, cancel_event=Event())

    request = runner.requests[0]
    assert request.command[:2] == (
        "/remote/venv/bin/python",
        "rocprof_profiler/remote_capture.py",
    )
    assert request.setup_script == "/remote/setup.sh"
    assert output == f"captured at {profile_root}/capture-1"
    assert (profile_root / "capture-1" / "results.csv").is_file()


@pytest.mark.parametrize("status", [status.value for status in _STATUS])
def test_a_remote_capture_whose_workload_did_not_run_is_a_typed_failure(
    tmp_path: Path, status: str
) -> None:
    """A load_failed capture's analysis was returned as a normal profile."""
    bridge = _bridge(tmp_path, _FakeJobRunner(status))

    if status in capture_runtime.WORKLOAD_RAN_STATUSES:
        assert bridge.capture("stats", _Lifecycle(), {}, cancel_event=Event())
        return
    with pytest.raises(capture_runtime.CaptureFailedError, match=f"status={status}"):
        bridge.capture("stats", _Lifecycle(), {}, cancel_event=Event())


def test_remote_capture_rejects_overlap_without_submitting_another_job(
    tmp_path: Path,
) -> None:
    runner = _BlockingJobRunner()
    bridge = _bridge(tmp_path, runner)
    outputs: list[str] = []

    def capture_first() -> None:
        outputs.append(bridge.capture("stats", _Lifecycle(), {}, cancel_event=Event()))

    worker = Thread(target=capture_first)
    worker.start()
    runner.entered.wait()

    try:
        with pytest.raises(RuntimeError) as failed:
            bridge.capture("stats", _Lifecycle(), {}, cancel_event=Event())
        overlap = getattr(failed.value, "report", None)
    finally:
        runner.release.set()
        worker.join()

    assert overlap == (
        "error: a remote Slurm ROCprof capture is already in progress; "
        "wait for it to finish before starting another"
    )
    assert len(runner.requests) == 1
    assert outputs == [f"captured at {tmp_path / 'profiles'}/capture-1"]


def test_remote_capture_releases_ownership_after_failure(tmp_path: Path) -> None:
    runner = _FailOnceJobRunner()
    bridge = _bridge(tmp_path, runner)

    with pytest.raises(_SubmissionError):
        bridge.capture("stats", _Lifecycle(), {}, cancel_event=Event())

    output = bridge.capture("stats", _Lifecycle(), {}, cancel_event=Event())

    assert output == f"captured at {tmp_path / 'profiles'}/capture-1"


def test_a_brokered_capture_from_a_candidate_workspace_reaches_the_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    config_path.write_text(
        f"""[slurm]
name = "test-cluster"
remote_workspace_root = "/remote/runs"
poll_interval_seconds = 0.01

[slurm.transport]
kind = "ssh"
host = "cluster"
ssh_command = {program_tail}, "ssh"]
rsync_command = {program_tail}, "rsync"]

[vibesys]
remote_python = "/remote/venv/bin/python"
""",
        encoding="utf-8",
    )
    worktrees = tmp_path / "worktrees"
    workspace = worktrees / "candidate" / "workspace"
    workspace.mkdir(parents=True)
    broker = SlurmProcessBroker(
        load_slurm_config(config_path), tmp_path / "broker.sock", local_roots=(worktrees,)
    )
    broker.start()
    monkeypatch.setenv("VIBESYS_SLURM_BROKER_SOCKET", str(broker.socket_path))
    monkeypatch.setenv("VIBESYS_SLURM_BROKER_TOKEN", broker.token)
    bridge = RemoteCaptureBridge(config_path, workspace, profile_root=tmp_path / "profiles")
    cancel = Event()
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

    worker = Thread(target=capture)
    try:
        worker.start()
        submitted = (cluster / SUBMITTED_FILE).read_text(encoding="utf-8")
        cancel.set()
        worker.join()
    finally:
        broker.close()

    assert submitted == JOB_ID, [str(item) for item in failures]
    assert f"scancel {JOB_ID}" in recorded_commands(cluster)


@pytest.mark.parametrize("status", list(capture_runtime.CaptureStatus))
def test_remote_capture_manifest_cannot_turn_failure_into_a_profile(
    tmp_path: Path, status: capture_runtime.CaptureStatus
) -> None:
    bridge = _bridge(tmp_path, _FakeJobRunner(status))
    if status in (
        capture_runtime.CaptureStatus.OK,
        capture_runtime.CaptureStatus.KILLED_AFTER_GRACE,
    ):
        assert bridge.capture("stats", _Lifecycle(), {}, cancel_event=Event())
    else:
        with pytest.raises(RuntimeError, match=f"status={status.value}") as failed:
            bridge.capture("stats", _Lifecycle(), {}, cancel_event=Event())
        assert type(failed.value).__name__ == "CaptureFailedError"


def test_a_configured_capture_requires_the_bundle_profile_command(tmp_path: Path) -> None:
    path = _config_path(tmp_path)
    with pytest.raises(ValueError, match=r"profile\.command"):
        configured_capture_lifecycle(load_slurm_config(path), load_slurm_policy(path), None)
