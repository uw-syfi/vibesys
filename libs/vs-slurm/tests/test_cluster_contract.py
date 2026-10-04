"""One Cluster contract exercised by the Fake and production transport shell."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

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
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStage,
    SlurmBatchStageResult,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobRequest,
    SlurmJobResult,
    SlurmJobRunner,
    SlurmJobStatus,
)

# test-isolation: public executable transport Fake configures production contract scenarios.
from vs_slurm.fake_connector import FakeConnector

# test-isolation: public wiring constructs every implementation for the contract suite.
from vs_slurm.wiring import FakeCluster, SlurmCluster

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class _Case:
    cluster: Cluster
    script: Callable[..., None]
    workspace: Path


def _make_case(implementation: str, tmp_path: Path) -> _Case:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if implementation == "fake":
        cluster = FakeCluster()
        return _Case(cluster, cluster.script, workspace)
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

    def script(operation_id: str, **values: object) -> None:
        values.pop("result", None)
        values.pop("artifact_contents", None)
        connector.script(operation_id, **values)

    return _Case(SlurmCluster(runner, state_root=tmp_path / "identity"), script, workspace)


@pytest.fixture(params=["fake", "slurm"])
def case(request: pytest.FixtureRequest, tmp_path: Path) -> _Case:
    return _make_case(str(request.param), tmp_path)


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
    assert observed.job_id == original.handle.job_id


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
    assert replayed.handle.job_id == observed.job_id


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
    running = case.cluster.inspect(submitted.handle.job_id, by_job_id=True)
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
    )
    request = SlurmBatchRequest(
        workspace=case.workspace,
        stages=(SlurmBatchStage(name="completed", command=("printf", "evidence")),),
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
