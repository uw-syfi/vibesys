from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from vibesys.skypilot.recovery import (
    ArtifactRecord,
    AttemptResourcesRecord,
    InvocationJournal,
    InvocationPhase,
    InvocationProvenance,
    InvocationRecord,
    InvocationResultRecord,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import StateNamespace


class _Slot:
    def __init__(self) -> None:
        self.value: object | None = None

    def load_optional(self) -> object | None:
        return self.value

    def save(self, value: object) -> None:
        self.value = value


class _Namespace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.slots: dict[str, _Slot] = {}

    def slot(self, path: str, _model: object) -> _Slot:
        return self.slots.setdefault(path, _Slot())


def _attempt_resources() -> AttemptResourcesRecord:
    return AttemptResourcesRecord(
        profile_name="test",
        infra="slurm/example/gpu",
        accelerator_type="MI300A",
        nodes=1,
        accelerators_per_node=4,
    )


def _result(artifact_path: Path) -> InvocationResultRecord:
    return InvocationResultRecord(
        status="COMPLETED",
        sky_exit_code=0,
        artifact=ArtifactRecord.create(str(artifact_path), b"{}"),
        provenance=InvocationProvenance(
            profile_name="test",
            infra="slurm/example/gpu",
            cluster_name="lease",
            job_name="vibesys-inv-example-a1",
            remote_job_id=7,
            attempt=1,
            accelerator_type="MI300A",
            nodes=1,
            accelerators_per_node=4,
        ),
    )


def test_journal_writes_prepared_before_crash_and_restores_exact_request(
    tmp_path: Path,
) -> None:
    namespace = _Namespace(tmp_path)

    def crash(phase: InvocationPhase, _record: object) -> None:
        if phase is InvocationPhase.PREPARED:
            _failure_message = "injected crash"
            raise RuntimeError(_failure_message)

    journal = InvocationJournal(cast("StateNamespace", namespace), crash_hook=crash)
    invocation_id = "a" * 32
    digest = "b" * 64
    with pytest.raises(RuntimeError, match="injected crash"):
        journal.prepare(invocation_id, digest, "c" * 64)

    recovered = InvocationJournal(cast("StateNamespace", namespace)).prepare(
        invocation_id, digest, "c" * 64
    )
    assert recovered.phase is InvocationPhase.PREPARED
    assert (
        recovered.job_name
        == InvocationJournal(cast("StateNamespace", namespace))
        .prepare(invocation_id, digest, "c" * 64)
        .job_name
    )

    with pytest.raises(ValueError, match="another request"):
        InvocationJournal(cast("StateNamespace", namespace)).prepare(
            invocation_id, "d" * 64, "c" * 64
        )


def test_completed_unacknowledged_payload_is_self_verifying_and_replayable(
    tmp_path: Path,
) -> None:
    journal = InvocationJournal(cast("StateNamespace", _Namespace(tmp_path)))
    record = journal.prepare("d" * 32, "e" * 64, "f" * 64)
    record = journal.submitting(record, "lease", _attempt_resources())
    record = journal.submitted(record, 7, "lease")
    completed = journal.completed(record, _result(tmp_path / "result.json"))

    assert completed.phase is InvocationPhase.COMPLETED
    assert completed.result is not None
    assert completed.result.artifact is not None
    assert completed.result.artifact.payload() == b"{}"
    assert journal.acknowledge(completed).phase is InvocationPhase.ACKNOWLEDGED


def test_journal_rejects_non_monotonic_delivery_offsets(tmp_path: Path) -> None:
    journal = InvocationJournal(cast("StateNamespace", _Namespace(tmp_path)))
    record = journal.prepare("f" * 32, "0" * 64, "1" * 64)
    record = journal.offsets(record, remote_read=10, client_delivered=8)

    with pytest.raises(ValueError, match="monotonic"):
        journal.offsets(record, remote_read=9, client_delivered=8)


def test_infrastructure_retry_gets_a_new_deterministic_job_name(tmp_path: Path) -> None:
    journal = InvocationJournal(cast("StateNamespace", _Namespace(tmp_path)))
    prepared = journal.prepare("1" * 32, "2" * 64, "3" * 64)
    submitting = journal.submitting(prepared, "expired-lease", _attempt_resources())
    assert submitting.phase is InvocationPhase.SUBMITTING
    assert submitting.attempt == 1
    assert submitting.attempt_resources == _attempt_resources()
    submitted = journal.submitted(submitting, 7, "expired-lease")
    assert submitted.attempt == 1
    submitted = journal.offsets(submitted, remote_read=10, client_delivered=8)

    retry = journal.retry(submitted)

    assert prepared.job_name.endswith("-a1")
    assert retry.job_name.endswith("-a2")
    assert retry.remote_job_id is None
    assert retry.attempt == 1
    assert retry.remote_read_offset == 0
    assert retry.client_delivered_offset == 0


def _base_fields(**overrides: object) -> dict[str, Any]:
    values: dict[str, Any] = {
        "invocation_id": "a" * 32,
        "request_sha256": "b" * 64,
        "snapshot_sha256": "c" * 64,
        "job_name": "vibesys-inv-x-a1",
        "phase": InvocationPhase.PREPARED,
    }
    values.update(overrides)
    return values


def test_artifact_record_rejects_bad_base64_and_digest_mismatch() -> None:
    good = ArtifactRecord.create("out.json", b"{}")

    with pytest.raises(ValueError, match="not valid base64"):
        ArtifactRecord(path="p", size=2, sha256=good.sha256, data_base64="!!not base64!!")
    with pytest.raises(ValueError, match="does not match its digest"):
        ArtifactRecord(path="p", size=3, sha256=good.sha256, data_base64=good.data_base64)
    with pytest.raises(ValueError, match="does not match its digest"):
        ArtifactRecord(path="p", size=2, sha256="0" * 64, data_base64=good.data_base64)


def test_invocation_record_rejects_inconsistent_phase_fields(tmp_path: Path) -> None:
    result = _result(tmp_path / "r.json")
    resources = _attempt_resources()

    with pytest.raises(ValueError, match="client offset exceeds remote-read offset"):
        InvocationRecord(**_base_fields(remote_read_offset=1, client_delivered_offset=2))
    with pytest.raises(ValueError, match="terminal invocation is missing its result"):
        InvocationRecord(
            **_base_fields(phase=InvocationPhase.COMPLETED, attempt_resources=resources)
        )
    with pytest.raises(ValueError, match="nonterminal invocation must not contain a result"):
        InvocationRecord(
            **_base_fields(
                phase=InvocationPhase.RUNNING, attempt_resources=resources, result=result
            )
        )
    with pytest.raises(ValueError, match="prepared invocation must not be bound to an attempt"):
        InvocationRecord(**_base_fields(active_cluster_name="lease"))
    with pytest.raises(ValueError, match="remote invocation is missing attempt resources"):
        InvocationRecord(**_base_fields(phase=InvocationPhase.SUBMITTING))


def test_journal_rejects_out_of_order_transitions(tmp_path: Path) -> None:
    journal = InvocationJournal(cast("StateNamespace", _Namespace(tmp_path)))
    prepared = journal.prepare("4" * 32, "5" * 64, "6" * 64)

    with pytest.raises(ValueError, match="only an attached invocation can be retried"):
        journal.retry(prepared)
    with pytest.raises(ValueError, match="only completed invocations can be acknowledged"):
        journal.acknowledge(prepared)

    submitting = journal.submitting(prepared, "lease", _attempt_resources())
    with pytest.raises(ValueError, match="only a prepared invocation can begin submission"):
        journal.submitting(submitting, "lease", _attempt_resources())
    with pytest.raises(ValueError, match="submitted cluster differs from submission intent"):
        journal.submitted(submitting, 7, "other-lease")
