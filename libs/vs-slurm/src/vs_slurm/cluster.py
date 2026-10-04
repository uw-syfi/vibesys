"""Durable stable-identity cluster operations over the Slurm transport."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from .cluster_store import OperationStore
from .cluster_types import (
    ClusterCancelOutcome,
    ClusterCancelRequested,
    ClusterCollected,
    ClusterCollectOutcome,
    ClusterConflict,
    ClusterHandle,
    ClusterInspectOutcome,
    ClusterObservation,
    ClusterRejected,
    ClusterSubmitOutcome,
    ClusterSubmitted,
    ClusterTarget,
    ClusterUnknown,
    collection_problem,
    validate_operation_id,
)
from .remote_operations import RemoteOperationError, RemoteOperationEvidence
from .runner import (
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmError,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmSubmissionRejectedError,
    _validate_batch_request,
    _validate_request,
)
from .staging import _ContentStageError, tree_content_identity

Request: TypeAlias = SlurmJobRequest | SlurmBatchRequest


class Operation(BaseModel):
    """Persisted intent, recorded before any scheduler submission."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str
    payload_digest: str | None = None
    handle: ClusterHandle | None = None
    dispatched: bool = False
    cancelled: bool = False
    rejected: str | None = None

    @field_validator("operation_id")
    @classmethod
    def _safe_identity(cls, value: str) -> str:
        validate_operation_id(value)
        return value

    @field_validator("payload_digest")
    @classmethod
    def _valid_digest(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            message = "payload_digest must be a SHA256 hexadecimal digest"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _legal_intent(self) -> Operation:
        if self.handle is not None:
            job = job_handle(self.handle)
            if job.invocation_id != self.operation_id or (
                job.job_id != "0" and not self.dispatched
            ):
                message = "handle identity must match its dispatched operation_id"
                raise ValueError(message)
        elif self.dispatched or not self.cancelled or self.payload_digest is not None:
            message = "an operation without a handle must be a cancellation tombstone"
            raise ValueError(message)
        return self


def _value(value: object) -> object:
    if isinstance(value, Path):
        return str(value.resolve())
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _value(getattr(value, f.name))
            for f in fields(value)
            if f.name != "cancel_event"
        }
    if isinstance(value, Mapping):
        return {str(k): _value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    return value


def payload_digest(request: Request) -> str:
    """Validate production inputs and identify their content, commands and outputs."""
    if isinstance(request, SlurmBatchRequest):
        _validate_batch_request(request)
    else:
        _validate_request(request)
    payload = {
        "request": _value(request),
        "workspace_content": tree_content_identity(request.workspace, excludes=(".git/",)),
        "support_content": {
            name: tree_content_identity(path)
            for name, path in (request.support_trees or {}).items()
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def job_handle(handle: ClusterHandle) -> SlurmJobHandle:
    """Return the allocation locator shared by job and batch handles."""
    return handle.job if isinstance(handle, SlurmBatchHandle) else handle


def replace_job_id(handle: ClusterHandle, job_id: str) -> ClusterHandle:
    """Attach reconciled scheduler identity to a prepared locator."""
    job = job_handle(handle).model_copy(update={"job_id": job_id})
    return handle.model_copy(update={"job": job}) if isinstance(handle, SlurmBatchHandle) else job


class SlurmCluster:
    """Persist intent, submit once, and reconcile ambiguous scheduler acceptance.

    ``state_root`` is a caller-owned durable directory outside the submitted
    workspace. Recreate with the same directory after restart. A dispatched
    operation without scheduler evidence stays Unknown and is never resubmitted.
    """

    def __init__(self, runner: SlurmJobRunner, *, state_root: Path) -> None:
        """Create the implementation with its owned operation storage."""
        self._runner = runner
        self._store = OperationStore(state_root)

    def submit(self, request: Request, *, operation_id: str) -> ClusterSubmitOutcome:
        """Validate and submit once under a caller-supplied stable identity."""
        try:
            return self._submit(request, operation_id=operation_id)
        except (
            OSError,
            SlurmError,
            RemoteOperationError,
            ValidationError,
            UnicodeError,
            subprocess.SubprocessError,
        ) as exc:
            return ClusterUnknown(operation_id=operation_id, reason=str(exc))

    def _submit(self, request: Request, *, operation_id: str) -> ClusterSubmitOutcome:
        try:
            validate_operation_id(operation_id)
            digest = payload_digest(request)
            prepared = self._runner.recover_handle(request, operation_id=operation_id, job_id="0")
        except (SlurmError, OSError, ValueError, _ContentStageError) as exc:
            return ClusterRejected(operation_id=operation_id, reason=str(exc))
        with self._store.lock():
            record = self._load(operation_id)
            if record is not None:
                return self._existing(record, digest)
            record = Operation(operation_id=operation_id, payload_digest=digest, handle=prepared)
            self._save(record)
            if request.cancel_event is not None and request.cancel_event.is_set():
                self._save(record.model_copy(update={"cancelled": True}))
                return ClusterRejected(
                    operation_id=operation_id, reason="operation cancelled before submission"
                )
            record = record.model_copy(update={"dispatched": True})
            self._save(record)
        return self._dispatch(request, record)

    def _dispatch(self, request: Request, record: Operation) -> ClusterSubmitOutcome:
        operation_id = record.operation_id
        try:
            claim = self._runner.claim_operation(operation_id, record.model_dump_json())
            if claim.kind == "unknown":
                return ClusterUnknown(
                    operation_id=operation_id, reason="remote identity claim unresolved"
                )
            remote = self._from_remote(operation_id, claim.evidence)
            if remote is None:
                return ClusterUnknown(
                    operation_id=operation_id, reason="remote identity manifest unavailable"
                )
            if claim.kind == "existing":
                with self._store.lock():
                    local = self._load(operation_id) or record
                    remote = remote.model_copy(
                        update={"cancelled": remote.cancelled or local.cancelled}
                    )
                    self._save(remote)
                    return self._existing(remote, record.payload_digest or "")
            if remote.cancelled:
                reason = "operation cancelled before submission"
                with self._store.lock():
                    self._save(remote.model_copy(update={"rejected": reason}))
                self._runner.record_operation_rejection(operation_id, reason)
                return ClusterRejected(operation_id=operation_id, reason=reason)
        except (
            SlurmError,
            OSError,
            RemoteOperationError,
            ValidationError,
            UnicodeError,
            subprocess.SubprocessError,
        ) as exc:
            return ClusterUnknown(operation_id=operation_id, reason=str(exc))
        return self._dispatch_claimed(request, record)

    def _dispatch_claimed(self, request: Request, record: Operation) -> ClusterSubmitOutcome:
        operation_id = record.operation_id
        try:
            handle = (
                self._runner.submit_batch(request, operation_id=operation_id)
                if isinstance(request, SlurmBatchRequest)
                else self._runner.submit(request, operation_id=operation_id)
            )
        except SlurmSubmissionRejectedError as exc:
            with self._store.lock():
                current = self._load(operation_id) or record
                self._save(current.model_copy(update={"rejected": str(exc)}))
            self._runner.record_operation_rejection(operation_id, str(exc))
            return ClusterRejected(operation_id=operation_id, reason=str(exc))
        except (
            SlurmError,
            OSError,
            UnicodeError,
            subprocess.SubprocessError,
            RemoteOperationError,
            ValidationError,
        ) as exc:
            return ClusterUnknown(operation_id=operation_id, reason=str(exc))
        with self._store.lock():
            current = self._load(operation_id) or record
            current = current.model_copy(update={"handle": handle})
            self._save(current)
            self._runner.record_operation_acceptance(operation_id, current.model_dump_json())
            remote = self._runner.inspect_operation(operation_id)
            if remote.cancelled:
                current = current.model_copy(update={"cancelled": True})
                self._save(current)
            if current.cancelled:
                observed = self._reconcile(current)
                if isinstance(observed, ClusterUnknown):
                    return observed
        return ClusterSubmitted(operation_id=operation_id, handle=handle)

    def _existing(self, record: Operation, digest: str) -> ClusterSubmitOutcome:
        operation_id = record.operation_id
        if record.handle is not None and job_handle(record.handle).job_id == "0":
            self._runner.validate_handle(record.handle)
            remote = self._from_remote(operation_id, self._runner.inspect_operation(operation_id))
            if remote is None:
                return ClusterUnknown(
                    operation_id=operation_id, reason="remote identity manifest unavailable"
                )
            record = remote.model_copy(update={"cancelled": record.cancelled or remote.cancelled})
        return self._existing_confirmed(record, digest)

    def _existing_confirmed(self, record: Operation, digest: str) -> ClusterSubmitOutcome:
        operation_id = record.operation_id
        if record.payload_digest is not None and record.payload_digest != digest:
            return ClusterConflict(
                operation_id=operation_id, reason="operation_id already names another payload"
            )
        self._save(record)
        if record.cancelled and not record.dispatched:
            return ClusterRejected(
                operation_id=operation_id, reason="operation cancelled before submission"
            )
        if record.rejected is not None:
            return ClusterRejected(operation_id=operation_id, reason=record.rejected)
        if record.dispatched and record.payload_digest is None:
            return ClusterUnknown(operation_id=operation_id, reason="original payload unavailable")
        if (
            record.handle is not None
            and job_handle(record.handle).job_id != "0"
            and not record.cancelled
        ):
            return self._known_submission(record.operation_id, record.handle)
        return self._recover_submission(record)

    def _known_submission(self, operation_id: str, handle: ClusterHandle) -> ClusterSubmitOutcome:
        try:
            self._runner.validate_handle(handle)
        except SlurmError as exc:
            return ClusterUnknown(operation_id=operation_id, reason=str(exc))
        return ClusterSubmitted(operation_id=operation_id, handle=handle)

    def _recover_submission(self, record: Operation) -> ClusterSubmitOutcome:
        operation_id = record.operation_id
        observed = self._reconcile(record)
        if isinstance(observed, ClusterUnknown):
            return observed
        if observed.handle is None:
            return ClusterUnknown(
                operation_id=operation_id, reason="collection locator unavailable"
            )
        return ClusterSubmitted(operation_id=operation_id, handle=observed.handle)

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        """Observe scheduler evidence without submitting work."""
        try:
            return self._inspect(target, by_job_id=by_job_id)
        except (SlurmError, OSError, RemoteOperationError, ValidationError) as exc:
            operation_id = target if isinstance(target, str) and not by_job_id else None
            return ClusterUnknown(operation_id=operation_id, reason=str(exc))

    def _inspect(self, target: ClusterTarget, *, by_job_id: bool) -> ClusterInspectOutcome:
        with self._store.lock():
            record = self._record(target, by_job_id=by_job_id)
            if record is not None:
                return self._reconcile(record)
            if by_job_id and isinstance(target, str):
                try:
                    status, reason, start = self._runner.inspect_job(target)
                except (
                    SlurmError,
                    OSError,
                    UnicodeError,
                    subprocess.SubprocessError,
                    RemoteOperationError,
                    ValidationError,
                ) as exc:
                    return ClusterUnknown(operation_id=None, job_id=target, reason=str(exc))
                return self._observation(None, target, (status, reason, start), None)
            return ClusterUnknown(
                operation_id=target if isinstance(target, str) else None,
                reason="operation not found",
            )

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        """Record cancellation intent, leaving confirmation to inspect."""
        try:
            return self._cancel(target, by_job_id=by_job_id)
        except (SlurmError, OSError, RemoteOperationError, ValidationError) as exc:
            operation_id = target if isinstance(target, str) and not by_job_id else None
            return ClusterUnknown(operation_id=operation_id, reason=str(exc))

    def _cancel(self, target: ClusterTarget, *, by_job_id: bool) -> ClusterCancelOutcome:
        with self._store.lock():
            record = self._record(target, by_job_id=by_job_id)
            if record is None:
                if isinstance(target, str) and not by_job_id:
                    validate_operation_id(target)
                    self._save(Operation(operation_id=target, cancelled=True))
                    self._runner.cancel_operation(target)
                    return ClusterCancelRequested(operation_id=target)
                if isinstance(target, str) and by_job_id:
                    return self._cancel_job(target)
                return ClusterUnknown(operation_id=None, reason="operation not found")
            record = record.model_copy(update={"cancelled": True})
            self._save(record)
            self._runner.cancel_operation(record.operation_id)
            if not record.dispatched:
                return ClusterCancelRequested(operation_id=record.operation_id)
            observation = self._reconcile(record)
            if isinstance(observation, ClusterUnknown):
                return observation
            return ClusterCancelRequested(
                operation_id=record.operation_id, job_id=observation.job_id
            )

    def _cancel_job(self, job_id: str) -> ClusterCancelOutcome:
        status, _, _ = self._runner.inspect_job(job_id)
        if status == SlurmJobStatus.UNKNOWN:
            return ClusterUnknown(
                operation_id=None, job_id=job_id, reason="scheduler state unknown"
            )
        if status in {SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING}:
            self._runner.cancel_job(job_id)
        return ClusterCancelRequested(operation_id=None, job_id=job_id)

    def collect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCollectOutcome:
        """Collect terminal evidence, preserving partial results as Unknown."""
        observed = self.inspect(target, by_job_id=by_job_id)
        if isinstance(observed, ClusterUnknown):
            return observed
        if observed.status in {SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING}:
            return ClusterUnknown(
                operation_id=observed.operation_id,
                job_id=observed.job_id,
                reason="job is not terminal",
            )
        if observed.handle is None:
            return ClusterUnknown(
                operation_id=observed.operation_id,
                job_id=observed.job_id,
                reason="collection locator unavailable",
            )
        return self._collect_terminal(observed, observed.handle)

    def _collect_terminal(
        self, observed: ClusterObservation, handle: ClusterHandle
    ) -> ClusterCollectOutcome:
        try:
            result = (
                self._runner.collect_batch(handle)
                if isinstance(handle, SlurmBatchHandle)
                else self._runner.collect_evidence(handle)
            )
        except (
            SlurmError,
            OSError,
            UnicodeError,
            subprocess.SubprocessError,
            RemoteOperationError,
            ValidationError,
        ) as exc:
            return ClusterUnknown(
                operation_id=observed.operation_id, job_id=observed.job_id, reason=str(exc)
            )
        code = result.job_exit_code if isinstance(result, SlurmBatchResult) else result.exit_code
        if code == 0 and observed.status in {SlurmJobStatus.FAILED, SlurmJobStatus.CANCELLED}:
            return ClusterUnknown(
                operation_id=observed.operation_id,
                job_id=observed.job_id,
                reason="scheduler terminal state contradicts zero allocation exit status",
                result=result,
            )
        missing_stage = isinstance(result, SlurmBatchResult) and (
            not isinstance(handle, SlurmBatchHandle)
            or len(result.stages) != len(handle.stages)
            or any(
                (stage.exit_code is None and not stage.skipped)
                or stage.collection_failure is not None
                for stage in result.stages
            )
        )
        problem = collection_problem(result)
        if problem is not None or missing_stage:
            return ClusterUnknown(
                operation_id=observed.operation_id,
                job_id=observed.job_id,
                reason=problem or "missing stage evidence",
                result=result,
            )
        return ClusterCollected(operation_id=observed.operation_id, result=result)

    @staticmethod
    def _from_remote(operation_id: str, evidence: RemoteOperationEvidence) -> Operation | None:
        if evidence.intent is None:
            return (
                Operation(operation_id=operation_id, cancelled=True) if evidence.cancelled else None
            )
        intent = Operation.model_validate_json(json.dumps(evidence.intent))
        record = (
            Operation.model_validate_json(json.dumps(evidence.accepted))
            if evidence.accepted is not None
            else intent
        )
        if evidence.accepted is not None and (
            record.handle is None or job_handle(record.handle).job_id == "0"
        ):
            raise SlurmError.invalid_operation_record(operation_id)
        if (
            record.operation_id != operation_id
            or intent.operation_id != operation_id
            or record.payload_digest != intent.payload_digest
        ):
            raise SlurmError.invalid_operation_record(operation_id)
        return record.model_copy(
            update={
                "cancelled": record.cancelled or evidence.cancelled,
                "rejected": evidence.rejected,
            }
        )

    def _load(self, operation_id: str) -> Operation | None:
        raw = self._store.read(operation_id)
        if raw is None:
            return None
        record = Operation.model_validate_json(raw)
        if record.operation_id != operation_id:
            raise SlurmError.invalid_operation_record(operation_id)
        return record

    def _save(self, record: Operation) -> None:
        self._store.write(record.operation_id, record.model_dump_json())

    def _record(self, target: ClusterTarget, *, by_job_id: bool) -> Operation | None:
        if not isinstance(target, str):
            job = job_handle(target)
            self._runner.validate_handle(target)
            record = self._load(job.invocation_id)
            return record or Operation(
                operation_id=job.invocation_id, handle=target, dispatched=True
            )
        if not by_job_id:
            validate_operation_id(target)
            local = self._load(target)
            if local is not None:
                return local
            remote = self._from_remote(target, self._runner.inspect_operation(target))
            if remote is not None:
                self._save(remote)
            return remote
        for raw in self._store.records():
            record = Operation.model_validate_json(raw)
            if record.handle is not None and job_handle(record.handle).job_id == target:
                return record
        return None

    def _reconcile(self, record: Operation) -> ClusterInspectOutcome:
        handle = record.handle
        if handle is None or not record.dispatched:
            return ClusterUnknown(
                operation_id=record.operation_id, reason="operation has no accepted scheduler job"
            )
        job = job_handle(handle)
        try:
            self._runner.validate_handle(handle)
            remote = self._runner.inspect_operation(record.operation_id)
            original = self._from_remote(record.operation_id, remote)
            if (
                original is not None
                and original.payload_digest is not None
                and record.payload_digest is not None
                and original.payload_digest != record.payload_digest
            ):
                return ClusterUnknown(
                    operation_id=record.operation_id,
                    reason="remote operation payload conflicts with local intent",
                )
            if job.job_id == "0" and original is not None and original.handle is not None:
                accepted = job_handle(original.handle)
                if accepted.job_id != "0":
                    self._runner.validate_handle(original.handle)
                    record = original.model_copy(
                        update={"cancelled": record.cancelled or original.cancelled}
                    )
                    handle = original.handle
                    job = accepted
                    self._save(record)
            if remote.cancelled and not record.cancelled:
                record = record.model_copy(update={"cancelled": True})
                self._save(record)
            if job.job_id == "0":
                identity = self._runner.find_operation(record.operation_id)
                if identity is None:
                    return ClusterUnknown(
                        operation_id=record.operation_id, reason="submission acceptance unresolved"
                    )
                handle = replace_job_id(handle, identity)
                record = record.model_copy(update={"handle": handle})
                self._save(record)
                job = job_handle(handle)
                self._runner.record_operation_acceptance(
                    record.operation_id, record.model_dump_json()
                )
            status, reason, start = self._runner.inspect_job(job.job_id)
            if record.cancelled and status in {SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING}:
                self._runner.cancel(job)
        except (
            SlurmError,
            OSError,
            UnicodeError,
            subprocess.SubprocessError,
            RemoteOperationError,
            ValidationError,
        ) as exc:
            return ClusterUnknown(
                operation_id=record.operation_id, job_id=job.job_id, reason=str(exc)
            )
        return self._observation(record.operation_id, job.job_id, (status, reason, start), handle)

    @staticmethod
    def _observation(
        operation_id: str | None,
        job_id: str,
        details: tuple[SlurmJobStatus, str | None, str | None],
        handle: ClusterHandle | None,
    ) -> ClusterInspectOutcome:
        status, reason, start = details
        if status == SlurmJobStatus.UNKNOWN:
            return ClusterUnknown(
                operation_id=operation_id, job_id=job_id, reason="scheduler state unknown"
            )
        return ClusterObservation(
            operation_id=operation_id,
            job_id=job_id,
            status=status,
            pending_reason=reason if status == SlurmJobStatus.PENDING else None,
            estimated_start=start if status == SlurmJobStatus.PENDING else None,
            handle=handle,
        )
