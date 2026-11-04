"""The remote rocprof capture bridge, over a fake cluster.

Guarantees under test:

* A configured capture uses one dynamic port, the configured Python and setup script, and the
  declared bounds; it requires the bundle's profile command and an explicit durable state root.
* A remote capture whose workload did not run is a typed failure and a remote manifest cannot
  turn failure into a profile; overlapping captures are rejected without a second submission;
  ownership is released after a failure; an ambiguous capture keeps artifacts but publishes no
  profile.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from resources.profilers.rocprof.remote_bridge import (
    RemoteCaptureBridge,
    RemoteCaptureError,
    _load_result,
    capture_runtime,
)

from vs_sandbox.api.slurm import (
    SlurmCapturePlan,
    configured_capture_lifecycle,
    load_slurm_policy,
    write_slurm_capture_plan,
)
from vs_sim.api.testing import (
    HANG_GUARD_S,
    ManualClock,
    start_thread,
    wait_or_fail,
    wait_until_started_sync,
)
from vs_slurm.api import (
    SlurmJobRequest,
    SlurmJobResult,
    SlurmJobStatus,
    load_slurm_config,
)
from vs_slurm.wiring import FakeCluster

if TYPE_CHECKING:
    from pathlib import Path

    from vs_slurm.api import ClusterSubmitOutcome, SlurmBatchRequest


_STATUS = capture_runtime.CaptureStatus


class _FakeCaptureCluster(FakeCluster):
    """A capture route over the same durable outcomes as the production Cluster."""

    def __init__(self, status: str = "ok", *, collection_failure: str | None = None) -> None:
        super().__init__(clock=ManualClock())
        self.requests: list[SlurmJobRequest] = []
        self.status = status
        self.collection_failure = collection_failure
        self.operation_ids: list[str] = []

    def submit(
        self, request: SlurmJobRequest | SlurmBatchRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        if not isinstance(request, SlurmJobRequest):
            return super().submit(request, operation_id=operation_id)
        self.requests.append(request)
        self.operation_ids.append(operation_id)
        tree = request.tree_artifacts[0].remote_path
        self.script(
            operation_id,
            states=(SlurmJobStatus.COMPLETED,),
            result=SlurmJobResult(
                job_id="42", exit_code=0, output="", collection_failure=self.collection_failure
            ),
            artifact_contents={
                request.file_artifacts[0].remote_path: json.dumps(
                    {
                        "output": "captured at /remote/profiles/capture-1",
                        "capture_ids": ["capture-1"],
                        "profiles_path": "/remote/profiles",
                    }
                ),
                f"{tree}/capture-1/results.csv": "kernel,duration\n",
                f"{tree}/capture-1/manifest.json": json.dumps(
                    {
                        "capture_id": "capture-1",
                        "status": self.status,
                        "load_returncode": 0,
                    }
                ),
            },
        )
        return super().submit(request, operation_id=operation_id)


def _cancel() -> threading.Event:
    """A fresh cancel token.

    The bridge is a stdlib-only resource that runs beside the profiler, so its signature is
    ``threading.Event`` and cannot name the ``vs_sim`` interface.
    """
    return threading.Event()


class _BlockingCaptureCluster(_FakeCaptureCluster):
    def __init__(self) -> None:
        super().__init__()
        self.entered = _cancel()
        self.release = _cancel()

    def submit(
        self, request: SlurmJobRequest | SlurmBatchRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        self.entered.set()
        wait_or_fail(self.release, "the held capture submission to be released")
        return super().submit(request, operation_id=operation_id)


class _SubmissionError(RuntimeError):
    pass


class _FailOnceCaptureCluster(_FakeCaptureCluster):
    def __init__(self) -> None:
        super().__init__()
        self._failed = False

    def submit(
        self, request: SlurmJobRequest | SlurmBatchRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        if not self._failed:
            self._failed = True
            raise _SubmissionError
        return super().submit(request, operation_id=operation_id)


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


def _configured_bridge(tmp_path: Path, runner: _FakeCaptureCluster) -> RemoteCaptureBridge:
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
        cluster=runner,
        clock=ManualClock(),
    )


def test_configured_lifecycle_uses_one_dynamic_port_and_declared_bounds(tmp_path: Path) -> None:
    bridge = _configured_bridge(tmp_path, _FakeCaptureCluster())

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


def _bridge(tmp_path: Path, runner: _FakeCaptureCluster) -> RemoteCaptureBridge:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return RemoteCaptureBridge(
        _config_path(tmp_path),
        workspace,
        profile_root=tmp_path / "profiles",
        cluster=runner,
        clock=ManualClock(),
    )


def test_remote_capture_uses_configured_python_and_setup_script(tmp_path: Path) -> None:
    profile_root = tmp_path / "profiles"
    runner = _FakeCaptureCluster()
    bridge = _bridge(tmp_path, runner)

    output = bridge.capture("stats", _Lifecycle(), {}, cancel_event=_cancel())

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
    bridge = _bridge(tmp_path, _FakeCaptureCluster(status))

    if status in capture_runtime.WORKLOAD_RAN_STATUSES:
        assert bridge.capture("stats", _Lifecycle(), {}, cancel_event=_cancel())
        return
    with pytest.raises(capture_runtime.CaptureFailedError, match=f"status={status}"):
        bridge.capture("stats", _Lifecycle(), {}, cancel_event=_cancel())


def test_remote_capture_rejects_overlap_without_submitting_another_job(
    tmp_path: Path,
) -> None:
    runner = _BlockingCaptureCluster()
    bridge = _bridge(tmp_path, runner)
    outputs: list[str] = []

    def capture_first() -> None:
        outputs.append(bridge.capture("stats", _Lifecycle(), {}, cancel_event=_cancel()))

    worker = start_thread(capture_first)
    wait_until_started_sync(runner.entered, worker)

    try:
        with pytest.raises(RuntimeError) as failed:
            bridge.capture("stats", _Lifecycle(), {}, cancel_event=_cancel())
        overlap = getattr(failed.value, "report", None)
    finally:
        runner.release.set()
        worker.result(timeout=HANG_GUARD_S)

    assert overlap == (
        "error: a remote Slurm ROCprof capture is already in progress; "
        "wait for it to finish before starting another"
    )
    assert len(runner.requests) == 1
    assert outputs == [f"captured at {tmp_path / 'profiles'}/capture-1"]


def test_remote_capture_releases_ownership_after_failure(tmp_path: Path) -> None:
    runner = _FailOnceCaptureCluster()
    bridge = _bridge(tmp_path, runner)

    with pytest.raises(_SubmissionError):
        bridge.capture("stats", _Lifecycle(), {}, cancel_event=_cancel())

    output = bridge.capture("stats", _Lifecycle(), {}, cancel_event=_cancel())

    assert output == f"captured at {tmp_path / 'profiles'}/capture-1"


@pytest.mark.parametrize("status", list(capture_runtime.CaptureStatus))
def test_remote_capture_manifest_cannot_turn_failure_into_a_profile(
    tmp_path: Path, status: capture_runtime.CaptureStatus
) -> None:
    bridge = _bridge(tmp_path, _FakeCaptureCluster(status))
    if status in (
        capture_runtime.CaptureStatus.OK,
        capture_runtime.CaptureStatus.KILLED_AFTER_GRACE,
    ):
        assert bridge.capture("stats", _Lifecycle(), {}, cancel_event=_cancel())
    else:
        with pytest.raises(RuntimeError, match=f"status={status.value}") as failed:
            bridge.capture("stats", _Lifecycle(), {}, cancel_event=_cancel())
        assert type(failed.value).__name__ == "CaptureFailedError"


def test_a_configured_capture_requires_the_bundle_profile_command(tmp_path: Path) -> None:
    path = _config_path(tmp_path)
    with pytest.raises(ValueError, match=r"profile\.command"):
        configured_capture_lifecycle(load_slurm_config(path), load_slurm_policy(path), None)


def test_ambiguous_capture_keeps_artifacts_without_publishing_a_profile(tmp_path: Path) -> None:
    cluster = _FakeCaptureCluster(collection_failure="allocation metadata unavailable")
    bridge = _bridge(tmp_path, cluster)
    with pytest.raises(ValueError, match="outcome is unresolved"):
        bridge.capture("stats", _Lifecycle(), {}, cancel_event=_cancel())
    (operation_id,) = cluster.operation_ids
    (request,) = cluster.requests
    assert request.file_artifacts[0].remote_path == f".vibesys-rocprof-result-{operation_id}.json"
    assert not (tmp_path / "profiles" / "capture-1").exists()


def test_remote_capture_requires_explicit_durable_state_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="cluster_state_root"):
        RemoteCaptureBridge(
            _config_path(tmp_path),
            workspace,
            profile_root=tmp_path / "profiles",
            clock=ManualClock(),
        )


_WHOLE_RESULT = json.dumps(
    {
        "failed_fields": [],
        "output": "captured",
        "capture_ids": ["run-1"],
        "profiles_path": "/remote/profiles",
    },
    sort_keys=True,
)


def test_a_whole_result_file_loads(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text(_WHOLE_RESULT + "\n", encoding="utf-8")

    assert _load_result(path) == {
        "output": "captured",
        "capture_ids": ["run-1"],
        "profiles_path": "/remote/profiles",
    }


@given(cut=st.integers(min_value=0, max_value=len(_WHOLE_RESULT) - 1))
def test_a_cut_result_file_is_malformed_never_an_empty_capture(
    cut: int, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A result cut anywhere is refused; it never reads as a capture that found no profiles."""
    path = tmp_path_factory.mktemp("result") / "result.json"
    path.write_text(_WHOLE_RESULT[:cut], encoding="utf-8")

    with pytest.raises(RemoteCaptureError):
        _load_result(path)


@pytest.mark.parametrize(
    "document",
    [
        '{"output": "x", "capture_ids": ["../escape"], "profiles_path": null}',
        '{"output": "x", "capture_ids": [], "profiles_path": null, "surprise": 1}',
        '{"capture_ids": [], "profiles_path": null}',
        "[]",
    ],
)
def test_a_result_file_of_the_wrong_shape_is_malformed(document: str, tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(RemoteCaptureError):
        _load_result(path)
