"""One Cluster contract exercised by the Fake and production transport shell."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import TYPE_CHECKING, TypedDict, Unpack

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_slurm.api import (
    Cluster,
    ClusterCancelRequested,
    ClusterCollected,
    ClusterConflict,
    ClusterObservation,
    ClusterRejected,
    ClusterSubmitted,
    ClusterUnknown,
    SlurmArtifactTarget,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStage,
    SlurmBatchStageResult,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmFileArtifact,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobResult,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmTreeArtifact,
)

# test-isolation: public executable transport Fake configures production contract scenarios.
from vs_slurm.fake_connector import FakeConnector

# test-isolation: public wiring constructs every implementation for the contract suite.
from vs_slurm.wiring import FakeCluster, SlurmCluster

if TYPE_CHECKING:
    from collections.abc import Callable


class _ScriptOptions(TypedDict, total=False):
    states: tuple[SlurmJobStatus, ...]
    pending_reason: str | None
    estimated_start: str | None
    lost_submit_reply: bool
    missing_exit_status: bool
    missing_stage_result: bool
    result: SlurmJobResult | SlurmBatchResult
    artifact_contents: dict[str, str]


@dataclass
class _Case:
    cluster: Cluster
    script: Callable[..., None]
    workspace: Path
    reopen: Callable[[], Cluster]
    on_accept: Callable[[str, Callable[[], None]], None]


def _make_case(implementation: str, tmp_path: Path) -> _Case:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if implementation == "fake":
        cluster = FakeCluster()

        def script(operation_id: str, **values: Unpack[_ScriptOptions]) -> None:
            cluster.script(
                operation_id,
                states=values.get("states", (SlurmJobStatus.PENDING,)),
                pending_reason=values.get("pending_reason"),
                estimated_start=values.get("estimated_start"),
                lost_submit_reply=values.get("lost_submit_reply", False),
                missing_exit_status=values.get("missing_exit_status", False),
                result=values.get("result"),
                artifact_contents=values.get("artifact_contents", {}),
            )

        return _Case(cluster, script, workspace, cluster.reopen, cluster.on_accept)
    connector = FakeConnector(tmp_path / "connector")
    remote = tmp_path / "remote"
    remote.mkdir()
    runner = SlurmJobRunner(
        SlurmConfig(
            name="contract",
            remote_workspace_root=str(remote),
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        ),
        process=connector,
        clock=lambda: 0.0,
    )

    def script(operation_id: str, **values: Unpack[_ScriptOptions]) -> None:
        connector.script(
            operation_id,
            states=values.get("states", (SlurmJobStatus.PENDING,)),
            pending_reason=values.get("pending_reason"),
            estimated_start=values.get("estimated_start"),
            lost_submit_reply=values.get("lost_submit_reply", False),
            missing_exit_status=values.get("missing_exit_status", False),
            missing_stage_result=values.get("missing_stage_result", False),
        )

    def reopen() -> Cluster:
        return SlurmCluster(runner, state_root=tmp_path / "identity")

    return _Case(reopen(), script, workspace, reopen, connector.on_accept)


@pytest.fixture(params=["fake", "slurm"])
def case(request: pytest.FixtureRequest, tmp_path: Path) -> _Case:
    return _make_case(str(request.param), tmp_path)


def _job_id(handle: SlurmJobHandle | SlurmBatchHandle) -> str:
    return handle.job.job_id if isinstance(handle, SlurmBatchHandle) else handle.job_id


def _request(case: _Case, command: tuple[str, ...] = ("true",)) -> SlurmJobRequest:
    return SlurmJobRequest(workspace=case.workspace, command=command)


def test_duplicate_identity_returns_the_original_job(case: _Case) -> None:
    case.script("duplicate", states=(SlurmJobStatus.PENDING,))
    first = case.cluster.submit(_request(case), operation_id="duplicate")
    duplicate = case.cluster.submit(_request(case), operation_id="duplicate")

    assert isinstance(first, ClusterSubmitted)
    assert isinstance(duplicate, ClusterSubmitted)
    assert duplicate.handle == first.handle


def test_reusing_an_identity_for_changed_payload_is_a_typed_conflict(case: _Case) -> None:
    case.script("conflict", states=(SlurmJobStatus.PENDING,))
    original = case.cluster.submit(_request(case), operation_id="conflict")
    changed = case.cluster.submit(_request(case, ("false",)), operation_id="conflict")

    assert isinstance(original, ClusterSubmitted)
    assert isinstance(changed, ClusterConflict)
    assert changed.operation_id == "conflict"
    observed = case.cluster.inspect("conflict")
    assert isinstance(observed, ClusterObservation)
    assert observed.job_id == _job_id(original.handle)


def test_lost_submit_reply_requires_inspection_and_retains_identity(case: _Case) -> None:
    case.script("lost", states=(SlurmJobStatus.PENDING,), lost_submit_reply=True)
    lost = case.cluster.submit(_request(case), operation_id="lost")

    assert isinstance(lost, ClusterUnknown)
    assert lost.operation_id == "lost"
    observed = case.cluster.inspect("lost")
    assert isinstance(observed, ClusterObservation)
    assert observed.operation_id == "lost"
    assert observed.status is SlurmJobStatus.PENDING
    replayed = case.cluster.submit(_request(case), operation_id="lost")
    assert isinstance(replayed, ClusterSubmitted)
    assert _job_id(replayed.handle) == observed.job_id


def test_pending_running_and_terminal_observations_include_queue_details(case: _Case) -> None:
    case.script(
        "transition",
        states=(SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING, SlurmJobStatus.COMPLETED),
        pending_reason="Resources",
        estimated_start="2026-10-04T06:00:00",
    )
    submitted = case.cluster.submit(_request(case), operation_id="transition")
    assert isinstance(submitted, ClusterSubmitted)

    pending = case.cluster.inspect("transition")
    assert isinstance(pending, ClusterObservation)
    assert pending.status is SlurmJobStatus.PENDING
    assert pending.pending_reason == "Resources"
    assert pending.estimated_start == "2026-10-04T06:00:00"
    running = case.cluster.inspect(_job_id(submitted.handle), by_job_id=True)
    assert isinstance(running, ClusterObservation)
    assert running.status is SlurmJobStatus.RUNNING
    completed = case.cluster.inspect("transition")
    assert isinstance(completed, ClusterObservation)
    assert completed.status is SlurmJobStatus.COMPLETED


def test_unknown_identity_stays_unknown(case: _Case) -> None:
    observed = case.cluster.inspect("absent")
    assert isinstance(observed, ClusterUnknown)
    assert observed.operation_id == "absent"


@pytest.mark.parametrize("state", [SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING])
@pytest.mark.parametrize("lost_reply", [False, True])
def test_cancel_accepted_job_is_idempotent_and_inspected_separately(
    case: _Case, state: SlurmJobStatus, *, lost_reply: bool
) -> None:
    case.script("cancel", states=(state,), lost_submit_reply=lost_reply)
    accepted = case.cluster.submit(_request(case), operation_id="cancel")
    assert isinstance(accepted, ClusterUnknown if lost_reply else ClusterSubmitted)

    cancelled = case.cluster.cancel("cancel")
    assert isinstance(cancelled, ClusterCancelRequested)
    assert isinstance(case.cluster.cancel("cancel"), ClusterCancelRequested)
    observed = case.cluster.inspect("cancel")
    assert isinstance(observed, ClusterObservation)
    assert observed.status is SlurmJobStatus.CANCELLED


def test_cancel_before_acceptance_prevents_later_submission(case: _Case) -> None:
    assert isinstance(case.cluster.cancel("early"), ClusterCancelRequested)
    submitted = case.cluster.submit(_request(case), operation_id="early")
    assert isinstance(submitted, ClusterRejected)
    assert submitted.operation_id == "early"


def test_cancel_after_completion_preserves_terminal_result(case: _Case) -> None:
    case.script(
        "finished",
        states=(SlurmJobStatus.COMPLETED,),
        result=SlurmJobResult(job_id="5000", exit_code=0, output=""),
    )
    assert isinstance(
        case.cluster.submit(_request(case), operation_id="finished"), ClusterSubmitted
    )
    assert isinstance(case.cluster.cancel("finished"), ClusterCancelRequested)
    observed = case.cluster.inspect("finished")
    assert isinstance(observed, ClusterObservation)
    assert observed.status is SlurmJobStatus.COMPLETED
    collected = case.cluster.collect("finished")
    assert isinstance(collected, ClusterCollected)
    assert isinstance(collected.result, SlurmJobResult)
    assert collected.result.exit_code == 0


def test_missing_allocation_exit_status_is_unknown(case: _Case) -> None:
    case.script("missing", states=(SlurmJobStatus.COMPLETED,), missing_exit_status=True)
    assert isinstance(case.cluster.submit(_request(case), operation_id="missing"), ClusterSubmitted)

    collected = case.cluster.collect("missing")
    assert isinstance(collected, ClusterUnknown)
    assert collected.operation_id == "missing"


def test_collect_preserves_completed_stages_after_allocation_failure(case: _Case) -> None:
    destination = case.workspace.parent / "collected" / "evidence.txt"
    artifact = SlurmArtifactTarget(remote_path="evidence.txt", local_path=destination, kind="file")
    tree_destination = case.workspace.parent / "collected" / "profile"
    tree_artifact = SlurmArtifactTarget(
        remote_path="profile", local_path=tree_destination, kind="tree"
    )
    result = SlurmBatchResult(
        job_id="5000",
        job_exit_code=None,
        job_output="",
        stages=(
            SlurmBatchStageResult(
                name="completed",
                exit_code=0,
                stdout="evidence",
                stderr="",
                elapsed_seconds=0.0,
                skipped=False,
                artifacts=(artifact, tree_artifact),
            ),
        ),
        phase_timings_seconds={},
        content_cache_hits=0,
        collection_failure="missing allocation exit status",
    )
    case.script(
        "partial",
        states=(SlurmJobStatus.FAILED,),
        result=result,
        missing_exit_status=True,
        artifact_contents={"evidence.txt": "evidence", "profile/trace.txt": "trace"},
    )
    request = SlurmBatchRequest(
        workspace=case.workspace,
        stages=(
            SlurmBatchStage(
                name="completed",
                command=(
                    "bash",
                    "-c",
                    "mkdir profile; printf trace > profile/trace.txt; printf evidence | tee evidence.txt",
                ),
                file_artifacts=(SlurmFileArtifact("evidence.txt", destination),),
                tree_artifacts=(SlurmTreeArtifact("profile", tree_destination),),
            ),
        ),
    )
    submitted = case.cluster.submit(request, operation_id="partial")
    assert isinstance(submitted, ClusterSubmitted)

    collected = case.cluster.collect("partial")
    assert isinstance(collected, ClusterUnknown)
    assert isinstance(collected.result, SlurmBatchResult)
    assert collected.result.job_exit_code is None
    assert collected.result.collection_failure
    assert len(collected.result.stages) == 1
    assert collected.result.stages[0].exit_code == 0
    assert collected.result.stages[0].stdout == "evidence"
    assert collected.result.stages[0].artifacts == (artifact, tree_artifact)
    assert destination.read_text(encoding="utf-8") == "evidence"
    assert (tree_destination / "trace.txt").read_text(encoding="utf-8") == "trace"


@pytest.mark.parametrize("operation_id", ["", "../unsafe", "with space", "x" * 129])
def test_invalid_operation_identity_is_rejected_before_submission(
    case: _Case, operation_id: str
) -> None:
    rejected = case.cluster.submit(_request(case), operation_id=operation_id)
    assert isinstance(rejected, ClusterRejected)


def test_invalid_request_is_rejected_by_every_implementation(case: _Case) -> None:
    rejected = case.cluster.submit(_request(case, ()), operation_id="bad-request")
    assert isinstance(rejected, ClusterRejected)


@pytest.mark.parametrize("implementation", ["fake", "slurm"])
@settings(max_examples=20)
@given(
    payload=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        min_size=1,
        max_size=20,
    )
)
def test_payload_changes_cannot_replace_an_accepted_operation(
    implementation: str, payload: str
) -> None:
    with TemporaryDirectory(prefix="slurm-contract-property-") as directory:
        case = _make_case(implementation, Path(directory))
        case.script("property", states=(SlurmJobStatus.PENDING,))
        original = case.cluster.submit(
            _request(case, ("printf", "%s", payload)), operation_id="property"
        )
        changed = case.cluster.submit(
            _request(case, ("printf", "%s", payload + "changed")), operation_id="property"
        )
        assert isinstance(original, ClusterSubmitted)
        assert isinstance(changed, ClusterConflict)


def test_reopened_cluster_reconciles_lost_reply_without_resubmission(case: _Case) -> None:
    case.script("restart", states=(SlurmJobStatus.PENDING,), lost_submit_reply=True)
    lost = case.cluster.submit(_request(case), operation_id="restart")
    assert isinstance(lost, ClusterUnknown)
    reopened = case.reopen()

    observed = reopened.inspect("restart")
    assert isinstance(observed, ClusterObservation)
    replay = reopened.submit(_request(case), operation_id="restart")
    assert isinstance(replay, ClusterSubmitted)
    assert _job_id(replay.handle) == observed.job_id
    repeated = case.cluster.inspect("restart")
    assert isinstance(repeated, ClusterObservation)
    assert repeated.job_id == observed.job_id


def test_two_cluster_instances_share_stable_operation_identity(case: _Case) -> None:
    case.script("shared", states=(SlurmJobStatus.PENDING,))
    first = case.cluster.submit(_request(case), operation_id="shared")
    second = case.reopen().submit(_request(case), operation_id="shared")
    assert isinstance(first, ClusterSubmitted)
    assert isinstance(second, ClusterSubmitted)
    assert first.handle == second.handle


def test_cancel_during_scheduler_acceptance_remains_reconcilable(case: _Case) -> None:
    accepted = Event()
    return_reply = Event()
    cancel_started = Event()

    def barrier() -> None:
        accepted.set()
        return_reply.wait()

    def cancel() -> object:
        cancel_started.set()
        return case.cluster.cancel("during")

    case.script("during", states=(SlurmJobStatus.PENDING,))
    case.on_accept("during", barrier)
    with ThreadPoolExecutor(max_workers=2) as threads:
        submission = threads.submit(case.cluster.submit, _request(case), operation_id="during")
        accepted.wait()
        cancellation = threads.submit(cancel)
        cancel_started.wait()
        return_reply.set()
        assert isinstance(submission.result(), ClusterSubmitted)
        assert isinstance(cancellation.result(), ClusterCancelRequested)

    observed = case.reopen().inspect("during")
    assert isinstance(observed, ClusterObservation)
    assert observed.status is SlurmJobStatus.CANCELLED


def test_missing_stage_evidence_preserves_completed_stages_and_stays_unknown(case: _Case) -> None:
    result = SlurmBatchResult(
        job_id="5000",
        job_exit_code=0,
        job_output="",
        stages=(
            SlurmBatchStageResult(
                name="completed",
                exit_code=0,
                stdout="evidence",
                stderr="",
                elapsed_seconds=0.0,
                skipped=False,
            ),
        ),
        phase_timings_seconds={},
        content_cache_hits=0,
    )
    case.script(
        "missing-stage",
        states=(SlurmJobStatus.FAILED,),
        result=result,
        missing_stage_result=True,
    )
    request = SlurmBatchRequest(
        workspace=case.workspace,
        stages=(
            SlurmBatchStage(name="completed", command=("printf", "evidence")),
            SlurmBatchStage(name="missing", command=("bash", "-c", "exit 7")),
        ),
    )
    assert isinstance(case.cluster.submit(request, operation_id="missing-stage"), ClusterSubmitted)

    collected = case.cluster.collect("missing-stage")
    assert isinstance(collected, ClusterUnknown)
    assert isinstance(collected.result, SlurmBatchResult)
    assert collected.result.job_exit_code == 0
    assert collected.result.stages[0].name == "completed"
    assert collected.result.stages[0].exit_code == 0
    assert collected.result.stages[0].stdout == "evidence"


@pytest.mark.parametrize("method", ["inspect", "cancel", "collect"])
def test_foreign_cluster_handle_is_unknown(case: _Case, method: str) -> None:
    case.script("foreign", states=(SlurmJobStatus.PENDING,))
    submitted = case.cluster.submit(_request(case), operation_id="foreign")
    assert isinstance(submitted, ClusterSubmitted)
    assert isinstance(submitted.handle, SlurmJobHandle)
    foreign = submitted.handle.model_copy(update={"config_identity": "f" * 64})
    operations = {
        "inspect": case.cluster.inspect,
        "cancel": case.cluster.cancel,
        "collect": case.cluster.collect,
    }
    assert isinstance(operations[method](foreign), ClusterUnknown)


def test_duplicate_with_unknown_scheduler_state_retains_accepted_identity(case: _Case) -> None:
    case.script("unknown-state", states=(SlurmJobStatus.UNKNOWN,))
    accepted = case.cluster.submit(_request(case), operation_id="unknown-state")
    duplicate = case.cluster.submit(_request(case), operation_id="unknown-state")
    assert isinstance(accepted, ClusterSubmitted)
    assert isinstance(duplicate, ClusterSubmitted)
    assert duplicate.handle == accepted.handle
    assert isinstance(case.cluster.inspect("unknown-state"), ClusterUnknown)


def test_content_change_conflicts_with_the_original_operation(case: _Case) -> None:
    source = case.workspace / "input.txt"
    source.write_text("original", encoding="utf-8")
    case.script("content", states=(SlurmJobStatus.PENDING,))
    original = case.cluster.submit(_request(case), operation_id="content")
    assert isinstance(original, ClusterSubmitted)
    source.write_text("changed", encoding="utf-8")
    conflict = case.cluster.submit(_request(case), operation_id="content")
    assert isinstance(conflict, ClusterConflict)


def test_git_metadata_change_does_not_change_the_submitted_payload(case: _Case) -> None:
    metadata = case.workspace / ".git"
    metadata.mkdir()
    (metadata / "index").write_text("original", encoding="utf-8")
    case.script("metadata", states=(SlurmJobStatus.PENDING,))
    original = case.cluster.submit(_request(case), operation_id="metadata")
    assert isinstance(original, ClusterSubmitted)
    (metadata / "index").write_text("changed", encoding="utf-8")
    duplicate = case.cluster.submit(_request(case), operation_id="metadata")
    assert isinstance(duplicate, ClusterSubmitted)
    assert duplicate.handle == original.handle
