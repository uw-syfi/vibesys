"""One Cluster contract exercised by the Fake and production transport shell."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, precondition, rule

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
from vs_slurm.fake_connector import FakeConnector, recorded_commands

# test-isolation: public wiring constructs every implementation for the contract suite.
from vs_slurm.wiring import FakeCluster, SlurmCluster

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from subprocess import CompletedProcess


class _ScriptOptions(TypedDict, total=False):
    states: tuple[SlurmJobStatus, ...]
    pending_reason: str | None
    estimated_start: str | None
    lost_submit_reply: bool
    missing_exit_status: bool
    missing_stage_result: bool
    result: SlurmJobResult | SlurmBatchResult
    artifact_contents: dict[str, str]
    rejected_reason: str
    lost_claim_reply: bool
    on_dispatch: Callable[[], None]


@dataclass
class _Case:
    cluster: Cluster
    script: Callable[..., None]
    workspace: Path
    reopen: Callable[[], Cluster]
    fresh: Callable[[], Cluster]
    forget_name_history: Callable[[str], None]
    on_accept: Callable[[str, Callable[[], None]], None]
    commands: Callable[[], Sequence[str]]


def _make_case(implementation: str, tmp_path: Path) -> _Case:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if implementation == "fake":
        cluster = FakeCluster()

        def script(operation_id: str, **values: Unpack[_ScriptOptions]) -> None:
            def lost_claim() -> None:
                message = "claim reply lost before intent publication"
                raise OSError(message)

            cluster.script(
                operation_id,
                # The connector-backed implementation has no COMPLETING lag, and this
                # suite compares both: the lag has its own tests on the Fake.
                teardown_lag=0,
                states=values.get("states", (SlurmJobStatus.PENDING,)),
                pending_reason=values.get("pending_reason"),
                estimated_start=values.get("estimated_start"),
                lost_submit_reply=values.get("lost_submit_reply", False),
                missing_exit_status=values.get("missing_exit_status", False),
                result=values.get("result"),
                artifact_contents=values.get("artifact_contents", {}),
                on_dispatch=lost_claim
                if values.get("lost_claim_reply")
                else values.get("on_dispatch", lambda: None),
            )
            rejection = values.get("rejected_reason")
            if rejection is not None:
                cluster.script(operation_id, rejected_reason=rejection)

        return _Case(
            cluster,
            script,
            workspace,
            cluster.reopen,
            cluster.reopen,
            cluster.forget_name_history,
            cluster.on_accept,
            lambda: (),
        )
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
            on_dispatch=values.get("on_dispatch", lambda: None),
            lost_claim_reply=values.get("lost_claim_reply", False),
        )
        rejection = values.get("rejected_reason")
        if rejection is not None:
            connector.script(operation_id, rejected_reason=rejection)

    def reopen() -> Cluster:
        return SlurmCluster(runner, state_root=tmp_path / "identity")

    cache_ids = count()

    def fresh() -> Cluster:
        return SlurmCluster(runner, state_root=tmp_path / f"fresh-identity-{next(cache_ids)}")

    return _Case(
        reopen(),
        script,
        workspace,
        reopen,
        fresh,
        connector.forget_name_history,
        connector.on_accept,
        lambda: recorded_commands(connector.state),
    )


@pytest.fixture(params=["fake", "slurm"])
def case(request: pytest.FixtureRequest, tmp_path: Path) -> _Case:
    return _make_case(str(request.param), tmp_path)


def _job_id(handle: SlurmJobHandle | SlurmBatchHandle) -> str:
    return handle.job.job_id if isinstance(handle, SlurmBatchHandle) else handle.job_id


def _request(case: _Case, command: tuple[str, ...] = ("true",)) -> SlurmJobRequest:
    return SlurmJobRequest(workspace=case.workspace, command=command)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("locator", ["handle", "operation"])
def test_poll_reads_manifest_at_most_once(
    case: _Case, locator: str, *, batch: bool, fresh: bool
) -> None:
    """Ownership recovery shares evidence with the poll, then retains its proof."""
    states = (SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING, SlurmJobStatus.COMPLETED)
    case.script("poll-bound", states=states)
    request: SlurmJobRequest | SlurmBatchRequest = _request(case)
    if batch:
        request = SlurmBatchRequest(
            workspace=case.workspace,
            stages=(SlurmBatchStage(name="work", command=("true",)),),
        )
    submitted = case.cluster.submit(request, operation_id="poll-bound")
    assert isinstance(submitted, ClusterSubmitted)
    cluster = case.fresh() if fresh else case.cluster
    target = submitted.handle if locator == "handle" else "poll-bound"
    counts = []
    command_counts = []
    for status in states:
        before = len(case.commands())
        observed = cluster.inspect(target)
        commands = case.commands()[before:]
        counts.append(sum("intent.json" in command for command in commands))
        assert isinstance(observed, ClusterObservation)
        assert observed.status is status
        assert observed.job_id == _job_id(submitted.handle)
        command_counts.append(len(commands))
    assert all(count <= 1 for count in counts), counts
    assert all(count <= 3 for count in command_counts), command_counts


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


def test_known_pre_acceptance_rejection_is_persistent_and_payload_guarded(case: _Case) -> None:
    case.script("rejected", rejected_reason="transport unavailable during staging")
    first = case.cluster.submit(_request(case), operation_id="rejected")
    replay = case.reopen().submit(_request(case), operation_id="rejected")
    assert isinstance(first, ClusterRejected)
    assert isinstance(replay, ClusterRejected)
    assert replay.reason == first.reason
    changed = case.reopen().submit(_request(case, ("false",)), operation_id="rejected")
    assert isinstance(changed, ClusterConflict)
    assert isinstance(case.cluster.inspect("rejected"), ClusterUnknown)


def test_fresh_local_cache_retains_global_operation_identity(case: _Case) -> None:
    case.script("global", states=(SlurmJobStatus.PENDING,))
    original = case.cluster.submit(_request(case), operation_id="global")
    duplicate = case.fresh().submit(_request(case), operation_id="global")
    assert isinstance(original, ClusterSubmitted)
    assert isinstance(duplicate, ClusterSubmitted)
    assert _job_id(duplicate.handle) == _job_id(original.handle)
    conflict = case.fresh().submit(_request(case, ("false",)), operation_id="global")
    assert isinstance(conflict, ClusterConflict)


def test_fresh_local_cache_reconciles_a_lost_submit_reply(case: _Case) -> None:
    case.script("global-lost", states=(SlurmJobStatus.PENDING,), lost_submit_reply=True)
    lost = case.cluster.submit(_request(case), operation_id="global-lost")
    assert isinstance(lost, ClusterUnknown)
    reconciled = case.fresh().submit(_request(case), operation_id="global-lost")
    assert isinstance(reconciled, ClusterSubmitted)
    original = case.cluster.inspect("global-lost")
    assert isinstance(original, ClusterObservation)
    assert original.job_id == _job_id(reconciled.handle)


def test_concurrent_local_caches_do_not_allocate_duplicate_jobs(case: _Case) -> None:
    accepted = Event()
    reply = Event()

    def barrier() -> None:
        accepted.set()
        reply.wait()

    case.script("concurrent", states=(SlurmJobStatus.PENDING,))
    case.on_accept("concurrent", barrier)
    first_cluster = case.fresh()
    second_cluster = case.fresh()
    with ThreadPoolExecutor(max_workers=2) as threads:
        first = threads.submit(first_cluster.submit, _request(case), operation_id="concurrent")
        accepted.wait()
        try:
            second = second_cluster.submit(_request(case), operation_id="concurrent")
            assert isinstance(second, ClusterSubmitted | ClusterUnknown)
        finally:
            reply.set()
        original = first.result()
    assert isinstance(original, ClusterSubmitted)
    first_observed = first_cluster.inspect("concurrent")
    second_observed = second_cluster.inspect("concurrent")
    assert isinstance(first_observed, ClusterObservation)
    assert isinstance(second_observed, ClusterObservation)
    assert first_observed.job_id == second_observed.job_id == _job_id(original.handle)


def test_missing_claim_manifest_keeps_replay_unknown_without_submission(case: _Case) -> None:
    case.script("missing-claim", lost_claim_reply=True)
    first = case.cluster.submit(_request(case), operation_id="missing-claim")
    assert isinstance(first, ClusterUnknown)
    assert first.operation_id == "missing-claim"
    assert first.job_id is None
    reopened = case.fresh()
    assert isinstance(reopened.inspect("missing-claim"), ClusterUnknown)
    replay = reopened.submit(_request(case), operation_id="missing-claim")
    assert isinstance(replay, ClusterUnknown)
    assert replay.operation_id == "missing-claim"
    assert replay.job_id is None


@pytest.mark.parametrize("status", [SlurmJobStatus.FAILED, SlurmJobStatus.CANCELLED])
def test_terminal_failure_cannot_turn_a_zero_exit_artifact_into_success(
    case: _Case, status: SlurmJobStatus
) -> None:
    case.script(
        "contradiction",
        states=(status,),
        result=SlurmJobResult(job_id="5000", exit_code=0, output="evidence"),
    )
    submitted = case.cluster.submit(
        _request(case, ("printf", "evidence")), operation_id="contradiction"
    )
    assert isinstance(submitted, ClusterSubmitted)
    collected = case.cluster.collect("contradiction")
    assert isinstance(collected, ClusterUnknown)
    assert isinstance(collected.result, SlurmJobResult)
    assert collected.result.exit_code == 0
    assert collected.result.output == "evidence"


def test_running_scheduler_state_blocks_collection_of_finished_artifacts(case: _Case) -> None:
    case.script(
        "still-running",
        states=(SlurmJobStatus.RUNNING,),
        result=SlurmJobResult(job_id="5000", exit_code=0, output="completed output"),
    )
    assert isinstance(
        case.cluster.submit(
            _request(case, ("printf", "completed output")), operation_id="still-running"
        ),
        ClusterSubmitted,
    )
    collected = case.cluster.collect("still-running")
    assert isinstance(collected, ClusterUnknown)
    assert collected.operation_id == "still-running"


def test_durable_acceptance_recovers_identity_after_name_history_expires(case: _Case) -> None:
    accepted = Event()
    reply = Event()

    def barrier() -> None:
        accepted.set()
        reply.wait()

    case.script("accepted-record", states=(SlurmJobStatus.PENDING,))
    case.on_accept("accepted-record", barrier)
    first_cluster = case.fresh()
    second_cluster = case.fresh()
    with ThreadPoolExecutor(max_workers=2) as threads:
        first = threads.submit(first_cluster.submit, _request(case), operation_id="accepted-record")
        accepted.wait()
        try:
            case.forget_name_history("accepted-record")
            unresolved = second_cluster.submit(_request(case), operation_id="accepted-record")
            assert isinstance(unresolved, ClusterUnknown)
        finally:
            reply.set()
        submitted = first.result()
    assert isinstance(submitted, ClusterSubmitted)
    observed = second_cluster.inspect("accepted-record")
    assert isinstance(observed, ClusterObservation)
    assert observed.job_id == _job_id(submitted.handle)


@pytest.mark.parametrize("reopened", [False, True])
def test_pre_cancelled_request_replays_rejection_and_preserves_payload_identity(
    case: _Case, *, reopened: bool
) -> None:
    cancel = Event()
    cancel.set()
    request = replace(_request(case), cancel_event=cancel)
    assert isinstance(case.cluster.submit(request, operation_id="pre-cancelled"), ClusterRejected)
    cluster = case.reopen() if reopened else case.cluster
    assert isinstance(cluster.submit(request, operation_id="pre-cancelled"), ClusterRejected)
    assert isinstance(
        cluster.submit(replace(request, command=("false",)), operation_id="pre-cancelled"),
        ClusterConflict,
    )
    assert isinstance(cluster.inspect("pre-cancelled"), ClusterUnknown)


def test_cancellation_without_scheduler_evidence_remains_unknown(case: _Case) -> None:
    case.script("unknown-cancel", states=(SlurmJobStatus.UNKNOWN,))
    assert isinstance(
        case.cluster.submit(_request(case), operation_id="unknown-cancel"), ClusterSubmitted
    )
    assert isinstance(case.cluster.cancel("unknown-cancel"), ClusterUnknown)
    assert isinstance(case.cluster.inspect("unknown-cancel"), ClusterUnknown)
    assert isinstance(case.reopen().inspect("unknown-cancel"), ClusterUnknown)


def test_local_rejection_survives_lost_remote_rejection_publication(tmp_path: Path) -> None:
    connector = FakeConnector(tmp_path / "connector")
    connector.script("rejected-publication", rejected_reason="staging rejected")

    def process(argv: Sequence[str], *, stdin: str | None, timeout: float) -> CompletedProcess[str]:
        if stdin is not None and "rejected.json.pending." in stdin:
            message = "rejection publication unavailable"
            raise OSError(message)
        return connector(argv, stdin=stdin, timeout=timeout)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = SlurmJobRunner(
        SlurmConfig(
            name="rejection",
            remote_workspace_root=str(tmp_path / "remote"),
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        ),
        process=process,
    )
    state = tmp_path / "state"
    request = SlurmJobRequest(workspace=workspace, command=("true",))
    first = SlurmCluster(runner, state_root=state).submit(
        request, operation_id="rejected-publication"
    )
    assert isinstance(first, ClusterUnknown)
    reopened = SlurmCluster(runner, state_root=state)
    assert isinstance(
        reopened.submit(request, operation_id="rejected-publication"), ClusterRejected
    )
    assert isinstance(
        reopened.submit(replace(request, command=("false",)), operation_id="rejected-publication"),
        ClusterConflict,
    )


@pytest.mark.parametrize("action", ["inspect", "cancel"])
@pytest.mark.parametrize(
    "error",
    [
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid UTF8"),
        subprocess.TimeoutExpired("transport", 1),
    ],
)
def test_fresh_operation_transport_failures_are_typed_unknown(
    tmp_path: Path, action: str, error: Exception
) -> None:
    def process(argv: Sequence[str], *, stdin: str | None, timeout: float) -> CompletedProcess[str]:
        del argv, stdin, timeout
        raise error

    runner = SlurmJobRunner(
        SlurmConfig(
            name="boundary",
            remote_workspace_root=str(tmp_path / "remote"),
            transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
        ),
        process=process,
    )
    cluster = SlurmCluster(runner, state_root=tmp_path / "state")
    outcome = cluster.inspect("fresh") if action == "inspect" else cluster.cancel("fresh")
    assert isinstance(outcome, ClusterUnknown)
    assert outcome.operation_id == "fresh"
    assert outcome.reason


def test_artifact_destination_failure_preserves_typed_unknown_evidence(case: _Case) -> None:
    obstacle = case.workspace.parent / "blocked-parent"
    obstacle.write_text("not a directory", encoding="utf-8")
    case.script(
        "bad-destination",
        states=(SlurmJobStatus.COMPLETED,),
        result=SlurmJobResult(job_id="42", exit_code=0, output="retained output"),
        artifact_contents={"artifact.txt": "evidence"},
    )
    request = SlurmJobRequest(
        workspace=case.workspace,
        command=("sh", "-c", "printf evidence > artifact.txt"),
        file_artifacts=(
            SlurmFileArtifact(remote_path="artifact.txt", local_path=obstacle / "artifact.txt"),
        ),
    )
    assert isinstance(
        case.cluster.submit(request, operation_id="bad-destination"), ClusterSubmitted
    )
    outcome = case.cluster.collect("bad-destination")
    assert isinstance(outcome, ClusterUnknown)
    assert isinstance(outcome.result, SlurmJobResult)
    assert outcome.result.exit_code == 0
    assert outcome.result.collection_failure


def test_cancellation_reconciles_a_job_that_completed_after_last_observation(case: _Case) -> None:
    case.script(
        "late-completion",
        states=(SlurmJobStatus.PENDING, SlurmJobStatus.COMPLETED),
        result=SlurmJobResult(job_id="42", exit_code=0, output=""),
    )
    assert isinstance(
        case.cluster.submit(_request(case), operation_id="late-completion"), ClusterSubmitted
    )
    first = case.cluster.inspect("late-completion")
    assert isinstance(first, ClusterObservation)
    assert first.status is SlurmJobStatus.PENDING
    assert isinstance(case.cluster.cancel("late-completion"), ClusterCancelRequested)
    final = case.cluster.inspect("late-completion")
    assert isinstance(final, ClusterObservation)
    assert final.status is SlurmJobStatus.COMPLETED


@pytest.mark.parametrize("active", [SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING])
@pytest.mark.parametrize(
    "terminal", [SlurmJobStatus.COMPLETED, SlurmJobStatus.FAILED, SlurmJobStatus.UNKNOWN]
)
def test_cancel_does_not_overwrite_scheduler_transition_during_request(
    case: _Case, active: SlurmJobStatus, terminal: SlurmJobStatus
) -> None:
    """The scheduler may finish between cancel's inspection and scancel."""
    case.script("cancel-transition", states=(active, terminal))
    assert isinstance(
        case.cluster.submit(_request(case), operation_id="cancel-transition"), ClusterSubmitted
    )
    assert isinstance(case.cluster.cancel("cancel-transition"), ClusterCancelRequested)
    observed = case.cluster.inspect("cancel-transition")
    if terminal is SlurmJobStatus.UNKNOWN:
        assert isinstance(observed, ClusterUnknown)
    else:
        assert isinstance(observed, ClusterObservation)
        assert observed.status is terminal


def _observable(outcome: object) -> tuple[object, ...]:
    """Compare semantics while implementations own locators and diagnostics."""
    assert isinstance(
        outcome,
        ClusterSubmitted
        | ClusterRejected
        | ClusterConflict
        | ClusterUnknown
        | ClusterObservation
        | ClusterCancelRequested
        | ClusterCollected,
    )
    values: tuple[object, ...] = (outcome.kind, outcome.operation_id)
    if isinstance(outcome, ClusterObservation):
        return (*values, outcome.status, outcome.pending_reason, outcome.estimated_start)
    if isinstance(outcome, ClusterUnknown):
        assert outcome.reason
    if isinstance(outcome, ClusterUnknown | ClusterCollected):
        result = outcome.result
        if isinstance(result, SlurmJobResult):
            return (*values, result.exit_code, result.output, bool(result.collection_failure))
        if isinstance(result, SlurmBatchResult):
            return (
                *values,
                result.job_exit_code,
                result.job_output,
                bool(result.collection_failure),
                tuple(
                    (
                        stage.name,
                        stage.exit_code,
                        stage.stdout,
                        stage.stderr,
                        stage.skipped,
                        bool(stage.collection_failure),
                        tuple(
                            (artifact.remote_path, artifact.kind, artifact.collect_on_failure)
                            for artifact in stage.artifacts
                        ),
                    )
                    for stage in result.stages
                ),
            )
        return (*values, None)
    return values


def _outcome_job_id(outcome: object) -> str | None:
    """Require every locator in one public outcome to name the same allocation."""
    if isinstance(outcome, ClusterSubmitted):
        return _job_id(outcome.handle)
    if isinstance(outcome, ClusterObservation):
        if outcome.handle is not None:
            assert _job_id(outcome.handle) == outcome.job_id
        return outcome.job_id
    if isinstance(outcome, ClusterUnknown):
        if outcome.result is not None:
            assert outcome.job_id in {None, outcome.result.job_id}
        return outcome.result.job_id if outcome.result is not None else outcome.job_id
    if isinstance(outcome, ClusterCollected):
        return outcome.result.job_id
    if isinstance(outcome, ClusterCancelRequested):
        return outcome.job_id
    return None


@dataclass(frozen=True)
class _ClusterScenario:
    active: SlurmJobStatus | None
    terminal: SlurmJobStatus
    lost_reply: bool
    fault: Literal["none", "missing-exit", "missing-artifact", "blocked-parent"]
    batch: bool
    reason: str | None
    early_cancel: bool
    active_observations: int


class ClusterContractMachine(RuleBasedStateMachine):
    """Drive both implementations with one generated scenario and operation sequence.

    FakeConnector answers real sbatch/squeue/sacct/scancel commands and runs
    production staging and collection. No Cluster method is replaced.
    """

    def __init__(self) -> None:
        super().__init__()
        self.directory = TemporaryDirectory(prefix="cluster-stateful-contract-")
        self.cases: list[_Case] = []
        self.requests: list[SlurmJobRequest | SlurmBatchRequest] = []
        self.handles: list[SlurmJobHandle | SlurmBatchHandle | None] = [None, None]
        self.job_ids: list[str | None] = [None, None]
        self.early_cancel = False
        self.collect_must_be_unknown = False
        self.collect_must_succeed = False
        self.complete_evidence = False

    @initialize(
        scenario=st.builds(
            _ClusterScenario,
            active=st.sampled_from([None, SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING]),
            terminal=st.sampled_from(
                [
                    SlurmJobStatus.COMPLETED,
                    SlurmJobStatus.FAILED,
                    SlurmJobStatus.CANCELLED,
                    SlurmJobStatus.UNKNOWN,
                ]
            ),
            lost_reply=st.booleans(),
            fault=st.sampled_from(["none", "missing-exit", "missing-artifact", "blocked-parent"]),
            batch=st.booleans(),
            reason=st.sampled_from([None, "Resources", "Priority", "Dependency"]),
            early_cancel=st.booleans(),
            active_observations=st.integers(min_value=1, max_value=3),
        )
    )
    def begin(self, scenario: _ClusterScenario) -> None:
        self.early_cancel = scenario.early_cancel
        self.collect_must_be_unknown = (
            scenario.terminal is not SlurmJobStatus.COMPLETED
            or scenario.fault != "none"
            or scenario.early_cancel
        )
        self.complete_evidence = scenario.fault == "none" and not scenario.early_cancel
        self.collect_must_succeed = (
            scenario.active is None
            and scenario.terminal is SlurmJobStatus.COMPLETED
            and self.complete_evidence
        )
        active, terminal = scenario.active, scenario.terminal
        lost_reply, fault = scenario.lost_reply, scenario.fault
        batch, reason, early_cancel = scenario.batch, scenario.reason, scenario.early_cancel
        states = (
            (active,) * scenario.active_observations + (terminal,)
            if active is not None
            else (terminal,)
        )
        for implementation in ("fake", "slurm"):
            root = Path(self.directory.name) / implementation
            root.mkdir()
            case = _make_case(implementation, root)
            destination = root / "artifacts" / "evidence.txt"
            if fault == "blocked-parent":
                destination.parent.write_text("obstacle", encoding="utf-8")
            artifact = SlurmFileArtifact("evidence.txt", destination)
            write = "" if fault == "missing-artifact" else "printf evidence > evidence.txt; "
            command = ("sh", "-c", write + "printf evidence")
            if batch:
                request: SlurmJobRequest | SlurmBatchRequest = SlurmBatchRequest(
                    workspace=case.workspace,
                    stages=(
                        SlurmBatchStage(name="work", command=command, file_artifacts=(artifact,)),
                    ),
                )
                result: SlurmJobResult | SlurmBatchResult = SlurmBatchResult(
                    job_id="scenario",
                    job_exit_code=0,
                    job_output="",
                    stages=(
                        SlurmBatchStageResult(
                            name="work",
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
            else:
                request = SlurmJobRequest(
                    workspace=case.workspace, command=command, file_artifacts=(artifact,)
                )
                result = SlurmJobResult(job_id="scenario", exit_code=0, output="evidence")
            case.script(
                "sequence",
                states=states,
                pending_reason=reason,
                estimated_start="2026-10-04T06:00:00" if reason else None,
                lost_submit_reply=lost_reply,
                missing_exit_status=fault == "missing-exit",
                result=result,
                artifact_contents={}
                if fault == "missing-artifact"
                else {"evidence.txt": "evidence"},
            )
            self.cases.append(case)
            self.requests.append(request)
        if early_cancel:
            self.cancel()
        outcomes = self._submit()
        for outcome in outcomes:
            assert isinstance(
                outcome,
                ClusterRejected
                if early_cancel
                else ClusterUnknown
                if lost_reply
                else ClusterSubmitted,
            )

    def _compare(self, outcomes: Sequence[object]) -> None:
        observed = [_observable(outcome) for outcome in outcomes]
        assert observed[0] == observed[1]
        assert all(value[1] == "sequence" for value in observed)
        for index, outcome in enumerate(outcomes):
            if (
                isinstance(outcome, ClusterObservation)
                and outcome.status is SlurmJobStatus.COMPLETED
                and self.complete_evidence
            ):
                self.collect_must_succeed = True
            handle = (
                outcome.handle
                if isinstance(outcome, ClusterSubmitted | ClusterObservation)
                else None
            )
            if handle is not None:
                previous = self.handles[index]
                if previous is not None:
                    assert _job_id(handle) == _job_id(previous)
                self.handles[index] = handle
            identity = _outcome_job_id(outcome)
            if identity is not None:
                previous_id = self.job_ids[index]
                if previous_id is not None:
                    assert identity == previous_id
                self.job_ids[index] = identity
            if isinstance(outcome, ClusterCollected):
                result = outcome.result
                code = (
                    result.job_exit_code
                    if isinstance(result, SlurmBatchResult)
                    else result.exit_code
                )
                assert type(code) is int
                assert not result.collection_failure

    def _submit(self) -> list[object]:
        outcomes: list[object] = [
            case.cluster.submit(request, operation_id="sequence")
            for case, request in zip(self.cases, self.requests, strict=True)
        ]
        self._compare(outcomes)
        return outcomes

    @rule()
    def submit(self) -> None:
        self._submit()

    @rule()
    def changed_payload(self) -> None:
        requests = [
            replace(request, command=("false",))
            if isinstance(request, SlurmJobRequest)
            else replace(request, stages=(SlurmBatchStage(name="changed", command=("false",)),))
            for request in self.requests
        ]
        outcomes = [
            case.cluster.submit(request, operation_id="sequence")
            for case, request in zip(self.cases, requests, strict=True)
        ]
        self._compare(outcomes)
        if not self.early_cancel:
            assert all(isinstance(outcome, ClusterConflict) for outcome in outcomes)

    @rule()
    def inspect(self) -> None:
        outcomes = []
        for case in self.cases:
            before = len(case.commands())
            outcomes.append(case.cluster.inspect("sequence"))
            commands = case.commands()[before:]
            assert sum("intent.json" in command for command in commands) <= 1
        self._compare(outcomes)

    @rule()
    def cancel(self) -> None:
        self._compare([case.cluster.cancel("sequence") for case in self.cases])

    @rule()
    def collect(self) -> None:
        outcomes = [case.cluster.collect("sequence") for case in self.cases]
        self._compare(outcomes)
        if self.collect_must_be_unknown:
            assert all(isinstance(outcome, ClusterUnknown) for outcome in outcomes)
        if self.collect_must_succeed:
            assert all(isinstance(outcome, ClusterCollected) for outcome in outcomes)

    @rule()
    def reopen(self) -> None:
        for case in self.cases:
            case.cluster = case.reopen()

    @precondition(lambda self: all(handle is not None for handle in self.handles))
    @rule()
    def poll_handle_with_fresh_cache(self) -> None:
        outcomes = []
        for case, handle in zip(self.cases, self.handles, strict=True):
            assert handle is not None
            case.cluster = case.fresh()
            before = len(case.commands())
            outcomes.append(case.cluster.inspect(handle))
            commands = case.commands()[before:]
            assert sum("intent.json" in command for command in commands) <= 1
        self._compare(outcomes)

    @precondition(lambda self: all(handle is not None for handle in self.handles))
    @rule(action=st.sampled_from(["inspect", "cancel", "collect"]), by_job_id=st.booleans())
    def target_accepted_job(
        self, action: Literal["inspect", "cancel", "collect"], *, by_job_id: bool
    ) -> None:
        outcomes = []
        for case, handle in zip(self.cases, self.handles, strict=True):
            assert handle is not None
            target = _job_id(handle) if by_job_id else handle
            match action:
                case "inspect":
                    before = len(case.commands())
                    outcome = case.cluster.inspect(target, by_job_id=by_job_id)
                    commands = case.commands()[before:]
                    assert sum("intent.json" in command for command in commands) <= 1
                case "cancel":
                    outcome = case.cluster.cancel(target, by_job_id=by_job_id)
                case "collect":
                    outcome = case.cluster.collect(target, by_job_id=by_job_id)
                case _:
                    raise AssertionError(action)
            outcomes.append(outcome)
        self._compare(outcomes)
        if action == "collect" and self.collect_must_be_unknown:
            assert all(isinstance(outcome, ClusterUnknown) for outcome in outcomes)
        if action == "collect" and self.collect_must_succeed:
            assert all(isinstance(outcome, ClusterCollected) for outcome in outcomes)

    def teardown(self) -> None:
        self.directory.cleanup()


TestClusterContractMachine = ClusterContractMachine.TestCase
TestClusterContractMachine.settings = settings(
    max_examples=30,
    stateful_step_count=20,
    report_multiple_bugs=False,
)


@pytest.mark.parametrize("status", list(SlurmJobStatus))
def test_complete_stage_evidence_does_not_override_aggregate_scheduler_state(
    case: _Case, status: SlurmJobStatus
) -> None:
    """Complete stage files alone cannot establish allocation success."""
    case.script(
        "aggregate",
        states=(status,),
        result=SlurmBatchResult(
            job_id="scenario",
            job_exit_code=0,
            job_output="",
            stages=(
                SlurmBatchStageResult(
                    name="work",
                    exit_code=0,
                    stdout="evidence",
                    stderr="",
                    elapsed_seconds=0.0,
                    skipped=False,
                ),
            ),
            phase_timings_seconds={},
            content_cache_hits=0,
        ),
    )
    request = SlurmBatchRequest(
        workspace=case.workspace,
        stages=(SlurmBatchStage(name="work", command=("printf", "evidence")),),
    )
    assert isinstance(case.cluster.submit(request, operation_id="aggregate"), ClusterSubmitted)
    collected = case.cluster.collect("aggregate")
    if status is SlurmJobStatus.COMPLETED:
        assert isinstance(collected, ClusterCollected)
    else:
        assert isinstance(collected, ClusterUnknown)
        assert collected.operation_id == "aggregate"
    if status in {SlurmJobStatus.COMPLETED, SlurmJobStatus.FAILED, SlurmJobStatus.CANCELLED}:
        assert isinstance(collected.result, SlurmBatchResult)
        assert collected.result.stages[0].exit_code == 0
        assert collected.result.stages[0].stdout == "evidence"


@pytest.mark.parametrize("batch", [False, True])
def test_missing_exit_file_marks_retained_evidence_incomplete(case: _Case, *, batch: bool) -> None:
    result: SlurmJobResult | SlurmBatchResult = SlurmJobResult(
        job_id="scenario", exit_code=0, output="evidence"
    )
    request: SlurmJobRequest | SlurmBatchRequest = _request(case, ("printf", "evidence"))
    if batch:
        result = SlurmBatchResult(
            job_id="scenario",
            job_exit_code=0,
            job_output="",
            stages=(
                SlurmBatchStageResult(
                    name="work",
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
        request = SlurmBatchRequest(
            workspace=case.workspace,
            stages=(SlurmBatchStage(name="work", command=("printf", "evidence")),),
        )
    case.script(
        "incomplete-exit",
        states=(SlurmJobStatus.COMPLETED,),
        result=result,
        missing_exit_status=True,
    )
    assert isinstance(
        case.cluster.submit(request, operation_id="incomplete-exit"), ClusterSubmitted
    )
    collected = case.cluster.collect("incomplete-exit")
    assert isinstance(collected, ClusterUnknown)
    assert isinstance(collected.result, SlurmBatchResult if batch else SlurmJobResult)
    assert collected.result.collection_failure


def _non_submit(
    cluster: Cluster, action: Literal["inspect", "cancel", "collect"], operation_id: str
) -> object:
    match action:
        case "inspect":
            return cluster.inspect(operation_id)
        case "cancel":
            return cluster.cancel(operation_id)
        case "collect":
            return cluster.collect(operation_id)


@pytest.mark.parametrize("action", ["inspect", "cancel", "collect"])
def test_malformed_identity_is_a_typed_unknown_for_non_submit_calls(
    case: _Case, action: Literal["inspect", "cancel", "collect"]
) -> None:
    outcome = _non_submit(case.cluster, action, "../unsafe")
    assert isinstance(outcome, ClusterUnknown)
    assert outcome.operation_id == "../unsafe"
    assert outcome.reason


@pytest.mark.parametrize("implementation", ["fake", "slurm"])
@pytest.mark.parametrize("action", ["inspect", "cancel", "collect"])
@settings(max_examples=20)
@given(
    operation_id=st.one_of(
        st.just(""),
        st.text(alphabet=" /:@\t\n☃", min_size=1, max_size=20),
        st.text(alphabet="abc012", min_size=129, max_size=135),
    )
)
def test_generated_malformed_non_submit_identities_never_escape_typed_outcomes(
    implementation: str, action: Literal["inspect", "cancel", "collect"], operation_id: str
) -> None:
    with TemporaryDirectory(prefix="cluster-invalid-identity-") as directory:
        case = _make_case(implementation, Path(directory))
        outcome = _non_submit(case.cluster, action, operation_id)
        assert isinstance(outcome, ClusterUnknown)
        assert outcome.operation_id == operation_id
        assert outcome.reason


def test_replay_after_cancel_requires_fresh_scheduler_confirmation(case: _Case) -> None:
    """An accepted handle cannot hide unresolved termination after cancellation."""
    case.script("cancel-replay", states=(SlurmJobStatus.PENDING, SlurmJobStatus.UNKNOWN))
    assert isinstance(
        case.cluster.submit(_request(case), operation_id="cancel-replay"), ClusterSubmitted
    )
    assert isinstance(case.cluster.cancel("cancel-replay"), ClusterCancelRequested)
    replay = case.cluster.submit(_request(case), operation_id="cancel-replay")
    assert isinstance(replay, ClusterUnknown)
    assert replay.operation_id == "cancel-replay"
    assert replay.job_id is not None


@pytest.mark.parametrize("action", ["inspect", "cancel", "collect"])
@pytest.mark.parametrize("fresh_cache", [False, True])
def test_a_handle_cannot_mix_one_operations_paths_with_another_jobs_identity(
    case: _Case, action: Literal["inspect", "cancel", "collect"], *, fresh_cache: bool
) -> None:
    owner_status = SlurmJobStatus.PENDING if action == "cancel" else SlurmJobStatus.COMPLETED
    case.script(
        "owner",
        states=(owner_status,),
        result=SlurmJobResult(job_id="scenario", exit_code=0, output="owner"),
    )
    sibling_status = SlurmJobStatus.COMPLETED if action == "collect" else SlurmJobStatus.PENDING
    case.script("sibling", states=(sibling_status,))
    owner = case.cluster.submit(_request(case, ("printf", "owner")), operation_id="owner")
    sibling = case.cluster.submit(_request(case), operation_id="sibling")
    assert isinstance(owner, ClusterSubmitted)
    assert isinstance(sibling, ClusterSubmitted)
    assert isinstance(owner.handle, SlurmJobHandle)
    assert _job_id(owner.handle) != _job_id(sibling.handle)
    mixed = owner.handle.model_copy(update={"job_id": _job_id(sibling.handle)})
    cluster = case.fresh() if fresh_cache else case.cluster
    match action:
        case "inspect":
            outcome = cluster.inspect(mixed)
        case "cancel":
            outcome = cluster.cancel(mixed)
        case "collect":
            outcome = cluster.collect(mixed)
    assert isinstance(outcome, ClusterUnknown)
    for operation_id, submitted, expected in (
        ("owner", owner, owner_status),
        ("sibling", sibling, sibling_status),
    ):
        for observer, target in (
            (case.cluster, operation_id),
            (cluster, submitted.handle),
        ):
            observed = observer.inspect(target)
            assert isinstance(observed, ClusterObservation)
            assert observed.status is expected
            assert observed.job_id == _job_id(submitted.handle)


@pytest.mark.parametrize("sibling_status", list(SlurmJobStatus))
@pytest.mark.parametrize("by_job_id", [False, True])
def test_cancelling_one_operation_preserves_its_siblings_identity_and_state(
    case: _Case, sibling_status: SlurmJobStatus, *, by_job_id: bool
) -> None:
    case.script("cancel-owner", states=(SlurmJobStatus.PENDING,))
    case.script("keep-sibling", states=(sibling_status,))
    owner = case.cluster.submit(_request(case), operation_id="cancel-owner")
    sibling = case.cluster.submit(_request(case), operation_id="keep-sibling")
    assert isinstance(owner, ClusterSubmitted)
    assert isinstance(sibling, ClusterSubmitted)
    assert _job_id(owner.handle) != _job_id(sibling.handle)
    target = _job_id(owner.handle) if by_job_id else owner.handle
    assert isinstance(case.cluster.cancel(target, by_job_id=by_job_id), ClusterCancelRequested)
    assert isinstance(case.cluster.cancel(target, by_job_id=by_job_id), ClusterCancelRequested)
    cancelled = case.cluster.inspect("cancel-owner")
    assert isinstance(cancelled, ClusterObservation)
    assert cancelled.status is SlurmJobStatus.CANCELLED
    assert cancelled.job_id == _job_id(owner.handle)
    observed = case.cluster.inspect("keep-sibling")
    assert observed.job_id == _job_id(sibling.handle)
    if sibling_status is SlurmJobStatus.UNKNOWN:
        assert isinstance(observed, ClusterUnknown)
    else:
        assert isinstance(observed, ClusterObservation)
        assert observed.status is sibling_status
