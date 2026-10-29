"""Contract of the executable Fake Slurm cluster used by cancellation tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from pydantic import ValidationError

from vs_sim.api import OsProcesses, OutputSink, ProcessOutcome, ProcessRun, ProcessSpec
from vs_sim.api.testing import ManualClock
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitted,
    FakeConnector,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmError,
    SlurmFileArtifact,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmLocalTransport,
    SlurmSshTransport,
    SlurmWorkspaceRootProbeError,
    probe_workspace_root,
)

# test-isolation: the Fake connector is an executable test double outside the library API.
from vs_slurm.fake_connector import (
    HOLD_FILE,
    JOB_ID,
    executing_cluster,
    handle,
    main,
    pending_jobs,
    recorded_commands,
    release_job,
    release_jobs,
)

# test-isolation: public wiring constructs the production implementation for transport contracts.
from vs_slurm.wiring import SlurmCluster

if TYPE_CHECKING:
    from collections.abc import Callable


def _unpaused(seconds: float) -> None:
    """The pause of a wait whose job is terminal at its first poll: it is never called."""
    message = f"the job was not terminal at its first poll (pause of {seconds} s)"
    raise AssertionError(message)


class _InProcessConnector:
    """The executable connector's request handling, answered in-process."""

    def __init__(self, state: Path) -> None:
        self._state = state

    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        del timeout, output
        assert spec.input
        answer = handle(self._state, json.loads(spec.input))
        return ProcessOutcome(0, json.dumps(answer).encode(), b"")


def _runner(state: Path, remote_root: str = "/remote/runs") -> SlurmJobRunner:
    config = SlurmConfig(
        name="fake",
        remote_workspace_root=remote_root,
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
    )
    return SlurmJobRunner(config, processes=_InProcessConnector(state), clock=ManualClock())


def test_a_submitted_job_stays_pending_until_it_is_cancelled(tmp_path: Path) -> None:
    state = tmp_path / "cluster"
    state.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = _runner(state)

    job = runner.submit(SlurmJobRequest(workspace=workspace, command=("run-benchmark",)))

    assert job.job_id == JOB_ID
    assert runner.poll(job) is SlurmJobStatus.PENDING
    assert runner.poll(job) is SlurmJobStatus.PENDING
    runner.cancel(job)
    assert runner.poll(job) is SlurmJobStatus.CANCELLED
    assert recorded_commands(state).count(f"scancel {JOB_ID}") == 1


def test_no_requests_are_recorded_before_the_first_one(tmp_path: Path) -> None:
    assert recorded_commands(tmp_path) == []


def _executing_runner(tmp_path: Path) -> tuple[Path, SlurmJobRunner, Path]:
    state = executing_cluster(tmp_path / "cluster")
    remote = tmp_path / "remote"
    remote.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("staged", encoding="utf-8")
    return state, _runner(state, str(remote)), workspace


def _workspace_probe_runner(
    tmp_path: Path,
) -> tuple[SlurmJobRunner, FakeConnector, Path, Path, str]:
    remote = tmp_path / "remote"
    remote.mkdir()
    connector = FakeConnector(tmp_path / "cluster")
    run_id = "run-with-local-root"
    runner = SlurmJobRunner(
        SlurmConfig(
            name="fake",
            remote_workspace_root=str(remote),
            transport=SlurmLocalTransport(
                kind="local",
                shell_command=("fake-connector", "shell"),
                rsync_command=("fake-connector", "rsync"),
            ),
        ),
        processes=connector,
        scratch_root=tmp_path / "probe-state",
        clock=ManualClock(),
    )
    return runner, connector, remote, tmp_path / "probe-state", run_id


def _probe_operation(run_id: str) -> str:
    return "workspace-root-probe-" + hashlib.sha256(run_id.encode()).hexdigest()[:32]


def _probe(runner: SlurmJobRunner, state: Path, run_id: str) -> None:
    probe_workspace_root(runner, state_root=state, run_id=run_id, pause=_unpaused)


def _assert_probe_remote_material_removed(remote: Path) -> None:
    cluster = remote / "fake"
    assert not tuple(path for path in cluster.iterdir() if path.name != ".vibesys-content-cache")
    assert not tuple((cluster / ".vibesys-content-cache").iterdir())


def test_workspace_root_probe_proves_both_transfer_directions(tmp_path: Path) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)

    _probe(runner, state, run_id)
    _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_names_the_setting_when_the_node_cannot_see_staging(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)

    def hide_staged_workspace(
        request: dict[str, object],
        answer: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        command = str(request.get("command", ""))
        if request["operation"] == "exec" and " sbatch " in f" {command} ":
            for workspace in (remote / "fake").glob("*/workspace"):
                if workspace.is_symlink():
                    workspace.unlink()
                else:
                    shutil.rmtree(workspace)
        return answer(request)

    connector.intercept(hide_staged_workspace)

    with pytest.raises(
        SlurmWorkspaceRootProbeError,
        match=r"remote_workspace_root.*probe job [0-9]+ exited with code 1",
    ):
        _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_refuses_when_submit_cannot_collect_the_node_token(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    connector.refuse_download(".vibesys-node-token")

    with pytest.raises(
        SlurmWorkspaceRootProbeError,
        match=r"remote_workspace_root.*no collectable node token",
    ):
        _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_reconciles_a_lost_submit_reply_without_resubmission(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    connector.script(_probe_operation(run_id), lost_submit_reply=True)

    _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_recovers_rejection_published_before_local_commit(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    stage_failed = False
    rejection_reply_lost = False

    def interrupt_after_remote_rejection(
        request: dict[str, object],
        answer: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        nonlocal stage_failed, rejection_reply_lost
        command = str(request.get("command", ""))
        if (
            request["operation"] == "exec"
            and command.startswith("mkdir -p")
            and ".cluster-operation" not in command
            and not stage_failed
        ):
            stage_failed = True
            raise OSError
        response = answer(request)
        if command.startswith("mv ") and "rejected.json" in command and not rejection_reply_lost:
            rejection_reply_lost = True
            raise KeyboardInterrupt
        return response

    connector.intercept(interrupt_after_remote_rejection)

    with pytest.raises(KeyboardInterrupt):
        _probe(runner, state, run_id)
    assert connector.submitted_job_ids() == ()

    with pytest.raises(SlurmWorkspaceRootProbeError, match="probe submission was rejected"):
        _probe(runner, state, run_id)
    _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_replays_rejection_publication_interrupted_before_effect(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    stage_failed = False
    rejection_interrupted = False

    def interrupt_before_remote_rejection(
        request: dict[str, object],
        answer: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        nonlocal stage_failed, rejection_interrupted
        command = str(request.get("command", ""))
        if (
            request["operation"] == "exec"
            and command.startswith("mkdir -p")
            and ".cluster-operation" not in command
            and not stage_failed
        ):
            stage_failed = True
            raise OSError
        if command.startswith("mv ") and "rejected.json" in command and not rejection_interrupted:
            rejection_interrupted = True
            raise KeyboardInterrupt
        return answer(request)

    connector.intercept(interrupt_before_remote_rejection)

    with pytest.raises(KeyboardInterrupt):
        _probe(runner, state, run_id)
    assert connector.submitted_job_ids() == ()

    with pytest.raises(SlurmWorkspaceRootProbeError, match="probe submission was rejected"):
        _probe(runner, state, run_id)
    _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_retries_when_cancelled_before_claim_effect(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    interrupted = False

    def interrupt_before_claim(
        request: dict[str, object],
        answer: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        nonlocal interrupted
        command = str(request.get("command", ""))
        if ".cluster-operation" in command and command.startswith("mkdir -p") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return answer(request)

    connector.intercept(interrupt_before_claim)

    with pytest.raises(KeyboardInterrupt):
        _probe(runner, state, run_id)
    _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_recovers_a_lost_atomic_claim_reply(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    connector.script(_probe_operation(run_id), lost_claim_reply=True)

    _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_resumes_after_claim_before_submitting_once(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    interrupted = False

    def interrupt_after_claim(
        request: dict[str, object],
        answer: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        nonlocal interrupted
        response = answer(request)
        command = str(request.get("command", ""))
        if "intent.json" in command and response["stdout"] == "CREATED" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return response

    connector.intercept(interrupt_after_claim)

    with pytest.raises(KeyboardInterrupt):
        _probe(runner, state, run_id)
    assert connector.submitted_job_ids() == ()

    _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_settles_and_cleans_after_caller_cancellation(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    operation_id = _probe_operation(run_id)
    connector.script(operation_id)

    def interrupt_after_acceptance() -> None:
        raise KeyboardInterrupt

    connector.on_accept(operation_id, interrupt_after_acceptance)

    with pytest.raises(KeyboardInterrupt):
        _probe(runner, state, run_id)
    with pytest.raises(SlurmWorkspaceRootProbeError, match="infrastructure error"):
        _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_cleans_when_local_acceptance_cannot_be_saved(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    operation_id = _probe_operation(run_id)
    connector.script(operation_id, states=(SlurmJobStatus.PENDING,))

    def break_local_records_after_acceptance() -> None:
        records = state / "records"
        records.rename(state / "records.saved")
        records.write_text("not a directory", encoding="utf-8")

    connector.on_accept(operation_id, break_local_records_after_acceptance)

    with pytest.raises(SlurmWorkspaceRootProbeError, match="infrastructure error"):
        _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    assert runner.inspect_job("5000").status is SlurmJobStatus.CANCELLED
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_cleans_when_local_artifact_cannot_be_cleared(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    (state / "node-marker").mkdir(parents=True)

    with pytest.raises(SlurmWorkspaceRootProbeError, match="infrastructure error"):
        _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    assert runner.inspect_job("5000").status is SlurmJobStatus.COMPLETED
    _assert_probe_remote_material_removed(remote)


@pytest.mark.parametrize("publication_order", ["before_effect", "after_effect"])
def test_workspace_root_probe_settles_when_acceptance_publication_is_cancelled(
    tmp_path: Path,
    publication_order: Literal["before_effect", "after_effect"],
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    operation_id = _probe_operation(run_id)
    connector.script(operation_id, states=(SlurmJobStatus.PENDING,))
    interrupted = False

    def interrupt_acceptance_publication(
        request: dict[str, object],
        answer: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        nonlocal interrupted
        command = str(request.get("command", ""))
        if command.startswith("if ln ") and "accepted.json" in command and not interrupted:
            interrupted = True
            if publication_order == "after_effect":
                answer(request)
            raise KeyboardInterrupt
        return answer(request)

    connector.intercept(interrupt_acceptance_publication)

    with pytest.raises(KeyboardInterrupt):
        _probe(runner, state, run_id)
    with pytest.raises(SlurmWorkspaceRootProbeError, match="infrastructure error"):
        _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    assert runner.inspect_job("5000").status is SlurmJobStatus.CANCELLED
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_retries_settlement_interrupted_during_scancel(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    operation_id = _probe_operation(run_id)
    connector.script(operation_id, states=(SlurmJobStatus.PENDING,))
    interrupted = False

    def interrupt_first_cancel(
        request: dict[str, object],
        answer: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        nonlocal interrupted
        command = str(request.get("command", ""))
        if command.startswith("scancel ") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return answer(request)

    def cancel_wait(_seconds: float) -> None:
        raise KeyboardInterrupt

    connector.intercept(interrupt_first_cancel)

    with pytest.raises(KeyboardInterrupt):
        probe_workspace_root(runner, state_root=state, run_id=run_id, pause=cancel_wait)
    with pytest.raises(SlurmWorkspaceRootProbeError, match="infrastructure error"):
        _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    _assert_probe_remote_material_removed(remote)


def test_workspace_root_probe_retries_cleanup_interrupted_by_caller_cancellation(
    tmp_path: Path,
) -> None:
    runner, connector, remote, state, run_id = _workspace_probe_runner(tmp_path)
    interrupted = False

    def interrupt_first_cleanup(
        request: dict[str, object],
        answer: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        nonlocal interrupted
        command = str(request.get("command", ""))
        if request["operation"] == "exec" and command.startswith("rm -rf --") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return answer(request)

    connector.intercept(interrupt_first_cleanup)

    with pytest.raises(KeyboardInterrupt):
        _probe(runner, state, run_id)
    _probe(runner, state, run_id)

    assert connector.submitted_job_ids() == ("5000",)
    _assert_probe_remote_material_removed(remote)


def test_job_handle_rejects_oversized_scheduler_identity_without_echoing_it(
    tmp_path: Path,
) -> None:
    runner, _connector, _remote, _state, _run_id = _workspace_probe_runner(tmp_path)
    oversized = "9" * 5_000
    workspace = tmp_path / "oversized-job-workspace"
    workspace.mkdir()

    with pytest.raises(ValidationError) as failure:
        runner.recover_handle(
            SlurmJobRequest(workspace=workspace, command=("true",)),
            operation_id="oversized-scheduler-id",
            job_id=oversized,
        )

    assert oversized not in str(failure.value)
    assert len(str(failure.value)) < 1_000


def test_workspace_root_probe_closes_local_state_store_diagnostics(tmp_path: Path) -> None:
    runner, _connector, _remote, _state, run_id = _workspace_probe_runner(tmp_path)
    marker = "private-state-root"
    blocked_state_root = tmp_path / marker
    blocked_state_root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(SlurmWorkspaceRootProbeError) as failure:
        _probe(runner, blocked_state_root, run_id)

    assert str(failure.value) == (
        "Slurm `remote_workspace_root` startup probe failed: infrastructure error"
    )
    assert marker not in str(failure.value)


def test_an_executing_cluster_runs_the_staged_job_before_its_first_poll(tmp_path: Path) -> None:
    _state, runner, workspace = _executing_runner(tmp_path)
    request = SlurmJobRequest(workspace=workspace, command=("cat", "input.txt"))

    job = runner.submit(request)
    assert runner.poll(job) is SlurmJobStatus.COMPLETED
    failed = runner.run(SlurmJobRequest(workspace=workspace, command=("false",)), pause=_unpaused)

    assert failed.exit_code != 0
    assert failed.job_id != job.job_id
    assert "staged" in runner.run(request, pause=_unpaused).output


def _racing_mv(directory: Path) -> Path:
    """Shadow ``mv`` so a competitor creates the destination just before the rename."""
    directory.mkdir()
    shim = directory / "mv"
    shim.write_text(
        "#!/bin/sh\n"
        "for last; do :; done\n"
        'case "$last" in *.vibesys-content-cache/*) mkdir -p "$last" ;; esac\n'
        'exec /bin/mv "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return directory


def test_publishing_never_reports_success_when_a_competitor_took_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state, runner, workspace = _executing_runner(tmp_path)
    shims = _racing_mv(tmp_path / "racing-bin")
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(SlurmError, match="exit code 74"):
        runner.run(SlurmJobRequest(workspace=workspace, command=("true",)), pause=_unpaused)


def test_an_executing_cluster_holds_jobs_until_they_are_cancelled(tmp_path: Path) -> None:
    state, runner, workspace = _executing_runner(tmp_path)
    (state / HOLD_FILE).touch()

    job = runner.submit(SlurmJobRequest(workspace=workspace, command=("true",)))

    assert runner.poll(job) is SlurmJobStatus.PENDING
    runner.cancel(job)
    assert runner.poll(job) is SlurmJobStatus.CANCELLED
    assert recorded_commands(state).count(f"scancel {job.job_id}") == 1


def test_the_ssh_stand_in_answers_like_the_connector(tmp_path: Path) -> None:
    state = tmp_path / "cluster"
    state.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    program = (sys.executable, "-m", "vs_slurm.fake_connector", str(state))
    runner = SlurmJobRunner(
        SlurmConfig(
            name="fake",
            remote_workspace_root="/remote/runs",
            transport=SlurmSshTransport(
                host="fake", ssh_command=(*program, "ssh"), rsync_command=(*program, "rsync")
            ),
        ),
        processes=OsProcesses(),
        clock=ManualClock(),
    )

    job = runner.submit(SlurmJobRequest(workspace=workspace, command=("run-benchmark",)))

    assert job.job_id == JOB_ID
    assert runner.poll(job) is SlurmJobStatus.PENDING
    runner.cancel(job)
    assert runner.poll(job) is SlurmJobStatus.CANCELLED
    assert recorded_commands(state).count(f"scancel {JOB_ID}") == 1


def test_the_ssh_and_rsync_stand_ins_answer_in_process(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "cluster"
    state.mkdir()

    assert main([str(state), "rsync", "-a", "--", "local/", "fake:/remote/runs/x/"]) == 0
    assert main([str(state), "ssh", "--", "fake", f"squeue -h -j {JOB_ID} -o %T"]) == 0
    assert capsys.readouterr().out == "PENDING\n"
    assert main([str(state), "ssh", "--", "fake", f"scancel {JOB_ID}"]) == 0
    assert main([str(state), "ssh", "--", "fake", f"squeue -h -j {JOB_ID} -o %T"]) == 0

    assert capsys.readouterr().out == ""
    assert recorded_commands(state) == [
        f"squeue -h -j {JOB_ID} -o %T",
        f"scancel {JOB_ID}",
        f"squeue -h -j {JOB_ID} -o %T",
    ]


def test_batch_collection_preserves_completed_stage_artifacts_without_allocation_status(
    tmp_path: Path,
) -> None:
    """A node failure after a completed stage cannot erase that stage's evidence."""
    _state, runner, workspace = _executing_runner(tmp_path)
    artifact = tmp_path / "collected" / "evidence.txt"
    batch = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace,
            stages=(
                SlurmBatchStage(
                    name="completed",
                    command=("bash", "-c", "printf evidence > evidence.txt"),
                    file_artifacts=(SlurmFileArtifact("evidence.txt", artifact),),
                ),
            ),
        )
    )
    Path(batch.job.remote_status_path).unlink()

    result = runner.collect_batch(batch)

    assert result.job_exit_code is None
    assert result.collection_failure
    assert len(result.stages) == 1
    assert result.stages[0].exit_code == 0
    assert result.stages[0].artifacts[0].local_path == artifact
    assert artifact.read_text(encoding="utf-8") == "evidence"


def test_pending_transport_retains_remote_operation_identity_across_local_caches(
    tmp_path: Path,
) -> None:
    state = tmp_path / "cluster"
    state.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = _runner(state)
    first = SlurmCluster(runner, state_root=tmp_path / "first-cache")
    second = SlurmCluster(runner, state_root=tmp_path / "second-cache")
    request = SlurmJobRequest(workspace=workspace, command=("benchmark",))

    submitted = first.submit(request, operation_id="pending")
    assert isinstance(submitted, ClusterSubmitted)
    duplicate = second.submit(request, operation_id="pending")
    assert isinstance(duplicate, ClusterSubmitted)
    assert isinstance(duplicate.handle, SlurmJobHandle)
    assert isinstance(submitted.handle, SlurmJobHandle)
    assert duplicate.handle.job_id == submitted.handle.job_id
    first.cancel("pending")
    observed = second.inspect("pending")
    assert isinstance(observed, ClusterObservation)
    assert observed.status is SlurmJobStatus.CANCELLED
    assert len([command for command in recorded_commands(state) if "&& sbatch " in command]) == 1


def test_pending_transport_cancels_only_the_selected_job(tmp_path: Path) -> None:
    state = tmp_path / "cluster"
    state.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = _runner(state)
    request = SlurmJobRequest(workspace=workspace, command=("benchmark",))
    first = runner.submit(request)
    second = runner.submit(request)
    assert first.job_id != second.job_id
    runner.cancel(first)
    assert runner.poll(first) is SlurmJobStatus.CANCELLED
    assert runner.poll(second) is SlurmJobStatus.PENDING


@pytest.mark.parametrize("execute", [False, True])
def test_ssh_cluster_preserves_operation_identity_with_both_fake_modes(
    tmp_path: Path, *, execute: bool
) -> None:
    state = tmp_path / "cluster"
    state.mkdir()
    if execute:
        executing_cluster(state)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    remote = tmp_path / "remote"
    remote.mkdir()
    program = (sys.executable, "-m", "vs_slurm.fake_connector", str(state))
    runner = SlurmJobRunner(
        SlurmConfig(
            name="fake",
            remote_workspace_root=str(remote) if execute else "/remote/runs",
            transport=SlurmSshTransport(
                host="fake", ssh_command=(*program, "ssh"), rsync_command=(*program, "rsync")
            ),
        ),
        processes=OsProcesses(),
        clock=ManualClock(),
    )
    first = SlurmCluster(runner, state_root=tmp_path / "first-cache")
    second = SlurmCluster(runner, state_root=tmp_path / "second-cache")
    request = SlurmJobRequest(workspace=workspace, command=("true",))

    submitted = first.submit(request, operation_id="ssh-operation")
    assert isinstance(submitted, ClusterSubmitted)
    duplicate = second.submit(request, operation_id="ssh-operation")
    assert isinstance(duplicate, ClusterSubmitted)
    assert isinstance(submitted.handle, SlurmJobHandle)
    assert isinstance(duplicate.handle, SlurmJobHandle)
    assert duplicate.handle.job_id == submitted.handle.job_id
    observed = second.inspect("ssh-operation")
    assert isinstance(observed, ClusterObservation)
    assert observed.status is (SlurmJobStatus.COMPLETED if execute else SlurmJobStatus.PENDING)


@pytest.mark.parametrize("exit_code", [0, 3])
def test_held_allocation_runs_its_retained_script_once_when_released(
    tmp_path: Path, exit_code: int
) -> None:
    state, runner, workspace = _executing_runner(tmp_path)
    (state / HOLD_FILE).touch()
    output = tmp_path / "allocation.txt"
    request = SlurmJobRequest(
        workspace=workspace,
        command=("bash", "-c", f'echo "$SLURM_JOB_ID" >> {output}; exit {exit_code}'),
    )
    job = runner.submit(request)
    assert pending_jobs(state) == (job.job_id,)
    assert runner.poll(job) is SlurmJobStatus.PENDING
    assert not output.exists()

    release_job(state, job.job_id)
    release_job(state, job.job_id)

    assert runner.poll(job) is (
        SlurmJobStatus.COMPLETED if exit_code == 0 else SlurmJobStatus.FAILED
    )
    assert output.read_text(encoding="utf-8") == f"{job.job_id}\n"
    assert pending_jobs(state) == ()
    assert len([command for command in recorded_commands(state) if "&& sbatch " in command]) == 1


def test_releasing_held_allocations_never_restarts_a_cancelled_job(tmp_path: Path) -> None:
    state, runner, workspace = _executing_runner(tmp_path)
    (state / HOLD_FILE).touch()
    request = SlurmJobRequest(workspace=workspace, command=("true",))
    cancelled = runner.submit(request)
    held = runner.submit(request)
    runner.cancel(cancelled)

    release_job(state, cancelled.job_id)
    release_jobs(state)

    assert runner.poll(cancelled) is SlurmJobStatus.CANCELLED
    assert runner.poll(held) is SlurmJobStatus.COMPLETED
    assert pending_jobs(state) == ()
    subsequent = runner.submit(request)
    assert runner.poll(subsequent) is SlurmJobStatus.COMPLETED


@pytest.mark.parametrize("release", [False, True])
def test_an_executing_cluster_reconciles_held_operation_by_scheduler_name(
    tmp_path: Path, *, release: bool
) -> None:
    state, runner, workspace = _executing_runner(tmp_path)
    (state / HOLD_FILE).touch()
    first = SlurmCluster(runner, state_root=tmp_path / "first-cache")
    submitted = first.submit(
        SlurmJobRequest(workspace=workspace, command=("true",)), operation_id="held-operation"
    )
    assert isinstance(submitted, ClusterSubmitted)
    assert isinstance(submitted.handle, SlurmJobHandle)
    if release:
        release_job(state, submitted.handle.job_id)
    accepted_paths = list((tmp_path / "remote").rglob("accepted.json"))
    assert len(accepted_paths) == 1
    accepted_paths[0].unlink()
    recovered = SlurmCluster(runner, state_root=tmp_path / "second-cache")

    observed = recovered.inspect("held-operation")

    assert isinstance(observed, ClusterObservation)
    assert isinstance(observed.handle, SlurmJobHandle)
    assert observed.handle.job_id == submitted.handle.job_id
    assert observed.status is (SlurmJobStatus.COMPLETED if release else SlurmJobStatus.PENDING)
    assert len([command for command in recorded_commands(state) if "&& sbatch " in command]) == 1
