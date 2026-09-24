from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from vibesys.skypilot.recovery import (
    ArtifactRecord,
    AttemptResourcesRecord,
    InvocationJournal,
    InvocationPhase,
    InvocationProvenance,
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
