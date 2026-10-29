"""Durable stable-identity cluster operations over the Slurm transport."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, TypeAlias

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from vs_sim.api import ProcessTimeoutError

from .cluster_store import OperationStore
from .cluster_types import (
    ANOTHER_JOB,
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
    live_observation,
    observation_names,
    validate_operation_id,
)
from .errors import SlurmError, SlurmSubmissionRejectedError
from .remote_operations import RemoteOperationError, RemoteOperationEvidence
from .runner import (
    _BATCH_RESULT_ROOT,
    SchedulerReading,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmPhase,
    _validate_batch_request,
    _validate_request,
)
from .staging import _ContentStageError, tree_content_identity

if TYPE_CHECKING:
    from vs_sim.api import ByteSource, OutputStream

Request: TypeAlias = SlurmJobRequest | SlurmBatchRequest


@dataclass(frozen=True, slots=True)
class _SubmissionAbsent:
    """One successful scheduler lookup found no job for a dispatched intent."""

    operation_id: str


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


def recorded_job_ids(state_root: Path) -> frozenset[str]:
    """The scheduler job ids of the operations a ``SlurmCluster`` recorded under ``state_root``.

    These are the jobs this cluster's owner submitted, whatever else the same user has
    queued. An operation not yet given a scheduler job contributes nothing; a missing
    directory means none was recorded. Reads only: it takes no lock and writes nothing.
    """
    if not state_root.is_dir():
        return frozenset()
    ids: set[str] = set()
    for raw in OperationStore(state_root).records():
        try:
            record = Operation.model_validate_json(raw)
        except ValidationError:
            continue  # a record another version wrote; it names no job this reader can trust
        if record.handle is None:
            continue
        job_id = job_handle(record.handle).job_id
        if job_id.isdigit() and int(job_id) > 0:
            ids.add(job_id)
    return frozenset(ids)


def replace_job_id(handle: ClusterHandle, job_id: str) -> ClusterHandle:
    """Attach reconciled scheduler identity to a prepared locator."""
    job = job_handle(handle).model_copy(update={"job_id": job_id})
    return handle.model_copy(update={"job": job}) if isinstance(handle, SlurmBatchHandle) else job


def _remote_layout(handle: SlurmBatchHandle) -> object:
    return tuple(
        (
            stage.name,
            tuple(
                (item.remote_path, item.kind, item.collect_on_failure)
                for item in (*stage.file_artifacts, *stage.tree_artifacts)
            ),
        )
        for stage in handle.stages
    )


def collection_locator(
    target: ClusterTarget, recorded: ClusterHandle | None
) -> ClusterHandle | None:
    """The job's recorded handle, with its artifacts landing where the caller's handle says.

    A run that attaches to a job another run submitted (#2757) holds a handle
    whose artifacts land in its own workspace, while the cluster's record names
    the submitter's. The caller's paths apply only when its handle declares the
    same stages and remote artifacts; the remote side always comes from the record.
    """
    if not isinstance(target, SlurmBatchHandle) or not isinstance(recorded, SlurmBatchHandle):
        return recorded
    if _remote_layout(target) != _remote_layout(recorded):
        return recorded
    return recorded.model_copy(update={"stages": target.stages})


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
            ProcessTimeoutError,
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
            ProcessTimeoutError,
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
            ProcessTimeoutError,
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
        if record.rejected is not None or (record.cancelled and not record.dispatched):
            return self._existing_confirmed(record, digest)
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
        handle = record.handle
        if handle is not None and job_handle(handle).job_id != "0":
            record = self._with_remote_cancel(record, job_handle(handle).job_id)
            if not record.cancelled:
                return self._known_submission(record.operation_id, handle)
        return self._recover_submission(record)

    def _with_remote_cancel(self, record: Operation, job_id: str) -> Operation:
        """The record, marked cancelled if any cache of this operation recorded a cancel.

        The local store is a cache. A cancel recorded through another one (another
        process, or a state root since replaced) is visible only as the remote
        marker, so a replay answered from this cache alone would call the job
        settled where a replay through the cancelling cache must confirm it.
        """
        if record.cancelled:
            return record
        evidence = self._remote_evidence(record, job_id, None)
        if evidence is None or not evidence.cancelled:
            return record
        cancelled = record.model_copy(update={"cancelled": True})
        self._save(cancelled)
        return cancelled

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
        except (
            SlurmError,
            OSError,
            RemoteOperationError,
            ValidationError,
            UnicodeError,
            ProcessTimeoutError,
        ) as exc:
            operation_id = target if isinstance(target, str) and not by_job_id else None
            return ClusterUnknown(operation_id=operation_id, reason=str(exc))

    def _inspect(self, target: ClusterTarget, *, by_job_id: bool) -> ClusterInspectOutcome:
        with self._store.lock():
            record, evidence = self._record(target, by_job_id=by_job_id)
            if record is not None:
                return self._reconcile(record, evidence)
            if by_job_id and isinstance(target, str):
                try:
                    reading = self._runner.inspect_job(target)
                except (
                    SlurmError,
                    OSError,
                    UnicodeError,
                    ProcessTimeoutError,
                    RemoteOperationError,
                    ValidationError,
                ) as exc:
                    return ClusterUnknown(operation_id=None, job_id=target, reason=str(exc))
                return self._observation(None, target, reading, None)
            if not isinstance(target, str):  # a handle's record is proved or raises
                return ClusterUnknown(operation_id=None, reason="operation not found")
            # Neither this host's store nor the cluster showed the operation's intent,
            # which every submission publishes before its sbatch. A missing intent
            # cannot be told from one whose stat failed (ESTALE, EACCES, EIO), so the
            # scheduler's name lookup decides.
            return self._absent_unless_named(target, "operation not found")

    def _absent_unless_named(self, operation_id: str, reason: str) -> ClusterUnknown:
        """No job is known for the operation: absent only if no scheduler job has its name.

        Raises ``SlurmError`` when the name lookup fails or finds two jobs, which
        proves nothing either way.
        """
        if self._runner.find_operation(operation_id) is not None:
            return ClusterUnknown(
                operation_id=operation_id,
                reason="a scheduler job has the operation's name, but no record of it was read",
            )
        return ClusterUnknown(operation_id=operation_id, reason=reason, job_absent=True)

    def cancel(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCancelOutcome:
        """Record cancellation intent, leaving confirmation to inspect.

        Given ``observed`` showing the job live, the scancel goes out first, by
        the job id it names, before any other read or write and before the
        operation store's lock, which other threads of the run hold across
        slow scheduler reads: nothing the caller has not budgeted delays it. A
        scancel that fails is Unknown at once. The cancel then goes on as
        without it, and its own scheduler read may send a second scancel to
        the same job, which changes nothing.
        """
        operation_id = target if isinstance(target, str) and not by_job_id else None
        if observed is not None and not observation_names(target, observed, by_job_id=by_job_id):
            return ClusterUnknown(operation_id=operation_id, reason=ANOTHER_JOB)
        try:
            live = live_observation(observed)
            if live is not None:
                # It reads and writes no local state, so it needs no lock.
                self._runner.cancel_job(live.job_id)
            return self._cancel(target, by_job_id=by_job_id)
        except (
            SlurmError,
            OSError,
            RemoteOperationError,
            ValidationError,
            UnicodeError,
            ProcessTimeoutError,
        ) as exc:
            return ClusterUnknown(operation_id=operation_id, reason=str(exc))

    def _cancel(self, target: ClusterTarget, *, by_job_id: bool) -> ClusterCancelOutcome:
        with self._store.lock():
            record, evidence = self._record(target, by_job_id=by_job_id)
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
            if not record.dispatched:
                self._runner.cancel_operation(record.operation_id)
                return ClusterCancelRequested(operation_id=record.operation_id)
            # Reconciliation cancels the live job. The remote tombstone costs
            # four round trips and only guards a replay, which also finds the
            # job cancelled, so it follows the scancel instead of delaying it.
            try:
                observation = self._reconcile_record(record, evidence)
            finally:
                self._runner.cancel_operation(record.operation_id)
            if isinstance(observation, ClusterUnknown):
                return observation
            return ClusterCancelRequested(
                operation_id=record.operation_id,
                job_id=None if isinstance(observation, _SubmissionAbsent) else observation.job_id,
            )

    def _cancel_job(self, job_id: str) -> ClusterCancelOutcome:
        status = self._runner.inspect_job(job_id).status
        if status == SlurmJobStatus.UNKNOWN:
            return ClusterUnknown(
                operation_id=None, job_id=job_id, reason="scheduler state unknown"
            )
        if status.active:
            self._runner.cancel_job(job_id)
        return ClusterCancelRequested(operation_id=None, job_id=job_id)

    def collect(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterInspectOutcome | None = None,
    ) -> ClusterCollectOutcome:
        """Collect terminal evidence, preserving partial results as Unknown.

        Partial results are result files that arrived whole but are missing or
        malformed. A download that did not complete is Unknown with no result:
        nothing is read from a partial copy, and a later collection reads the
        remote files again.

        ``observed`` is a reading the caller just took of this target; passing it saves
        the scheduler and manifest queries a fresh inspection would make.
        """
        if observed is None:
            observed = self.inspect(target, by_job_id=by_job_id)
        if isinstance(observed, ClusterUnknown):
            return observed
        if observed.status.active:
            return ClusterUnknown(
                operation_id=observed.operation_id,
                job_id=observed.job_id,
                reason="job is not terminal",
            )
        locator = collection_locator(target, observed.handle)
        if locator is None:
            return ClusterUnknown(
                operation_id=observed.operation_id,
                job_id=observed.job_id,
                reason="collection locator unavailable",
            )
        return self._collect_terminal(observed, locator)

    def stage_output(
        self, handle: SlurmBatchHandle, index: int, stream: OutputStream
    ) -> ByteSource:
        """The file stage ``index`` writes ``stream`` to (the batch script's layout), read live."""
        stage = PurePosixPath(handle.job.remote_workspace) / _BATCH_RESULT_ROOT / f"{index:04d}"
        return self._runner.remote_file(stage / f"{stream.value}.txt")

    def _collect_terminal(
        self, observed: ClusterObservation, handle: ClusterHandle
    ) -> ClusterCollectOutcome:
        try:
            result = (
                self._runner.collect_batch(handle, observed=observed.status)
                if isinstance(handle, SlurmBatchHandle)
                else self._runner.collect_evidence(handle, observed=observed.status)
            )
        except (
            SlurmError,
            OSError,
            UnicodeError,
            ProcessTimeoutError,
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

    def _record(
        self, target: ClusterTarget, *, by_job_id: bool
    ) -> tuple[Operation | None, RemoteOperationEvidence | None]:
        if not isinstance(target, str):
            return self._handle_record(target)
        if not by_job_id:
            validate_operation_id(target)
            local = self._load(target)
            if local is not None:
                return local, None
            evidence = self._runner.inspect_operation(target)
            remote = self._from_remote(target, evidence)
            if remote is not None:
                self._save(remote)
            return remote, evidence
        for raw in self._store.records():
            record = Operation.model_validate_json(raw)
            if record.handle is not None and job_handle(record.handle).job_id == target:
                return record, None
        return None, None

    def _handle_record(
        self, target: ClusterHandle
    ) -> tuple[Operation, RemoteOperationEvidence | None]:
        """Prove locator ownership with read-only evidence before publishing intent."""
        job = job_handle(target)
        self._runner.validate_handle(target)
        local = self._load(job.invocation_id)
        record = local
        evidence = None
        if record is None or record.handle is None or job_handle(record.handle).job_id == "0":
            evidence = self._runner.inspect_operation(job.invocation_id)
            remote = self._from_remote(job.invocation_id, evidence)
            if remote is not None:
                record = remote.model_copy(
                    update={
                        "cancelled": remote.cancelled or (local is not None and local.cancelled)
                    }
                )
        identity = (
            job_handle(record.handle).job_id
            if record is not None and record.handle is not None
            else None
        )
        if identity in {None, "0"}:
            identity = self._runner.find_operation(job.invocation_id)
        if identity != job.job_id:
            raise SlurmError.invalid_handle()
        if record is None:
            record = Operation(operation_id=job.invocation_id, handle=target, dispatched=True)
        if record.handle is None:
            raise SlurmError.invalid_handle()
        if job_handle(record.handle).job_id == "0":
            record = record.model_copy(update={"handle": replace_job_id(record.handle, job.job_id)})
        # Cache only after canonical identity matches; negative or ambiguous
        # ownership evidence must be retried on the next request.
        if record != local:
            self._save(record)
        return record, evidence

    def _reconcile(
        self,
        record: Operation,
        evidence: RemoteOperationEvidence | None = None,
    ) -> ClusterInspectOutcome:
        if record.handle is None or not record.dispatched:
            return self._never_dispatched(record, evidence)
        observed = self._reconcile_record(record, evidence)
        if isinstance(observed, _SubmissionAbsent):
            return ClusterUnknown(
                operation_id=observed.operation_id,
                reason="submission acceptance unresolved",
                job_absent=True,
            )
        return observed

    def _never_dispatched(
        self, record: Operation, evidence: RemoteOperationEvidence | None
    ) -> ClusterUnknown:
        """An operation this host never dispatched: cancelled first, or known by its tombstone.

        Only an intent another host published stands for a job then. Without one,
        none starts later either: a submitter that publishes its intent after the
        tombstone sees it and rejects itself, and one that published before left an
        intent. So the scheduler's name lookup decides, as for an unknown operation.
        """
        remote = (
            evidence
            if evidence is not None
            else self._runner.inspect_operation(record.operation_id)
        )
        if remote.intent is not None:
            return ClusterUnknown(
                operation_id=record.operation_id, reason="operation has no accepted scheduler job"
            )
        return self._absent_unless_named(record.operation_id, "operation started no scheduler job")

    def _reconcile_record(
        self,
        record: Operation,
        evidence: RemoteOperationEvidence | None = None,
    ) -> ClusterInspectOutcome | _SubmissionAbsent:
        """Reconcile the operation with one fresh scheduler observation."""
        handle = record.handle
        if handle is None or not record.dispatched:
            return ClusterUnknown(
                operation_id=record.operation_id, reason="operation has no accepted scheduler job"
            )
        job = job_handle(handle)
        try:
            self._runner.validate_handle(handle)
            remote = self._remote_evidence(record, job.job_id, evidence)
            original = None if remote is None else self._from_remote(record.operation_id, remote)
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
            if remote is not None and remote.cancelled and not record.cancelled:
                record = record.model_copy(update={"cancelled": True})
                self._save(record)
            if job.job_id == "0":
                identity = self._runner.find_operation(record.operation_id)
                if identity is None:
                    return _SubmissionAbsent(operation_id=record.operation_id)
                handle = replace_job_id(handle, identity)
                record = record.model_copy(update={"handle": handle})
                self._save(record)
                job = job_handle(handle)
                self._runner.record_operation_acceptance(
                    record.operation_id, record.model_dump_json()
                )
            reading = self._runner.inspect_job(job.job_id)
            # A job already tearing down (COMPLETING) cannot be cancelled again;
            # a scancel per inspection would only add round trips.
            if record.cancelled and reading.phase in {SlurmPhase.PENDING, SlurmPhase.RUNNING}:
                self._runner.cancel(job)
        except (
            SlurmError,
            OSError,
            UnicodeError,
            ProcessTimeoutError,
            RemoteOperationError,
            ValidationError,
        ) as exc:
            return ClusterUnknown(
                operation_id=record.operation_id, job_id=job.job_id, reason=str(exc)
            )
        return self._with_progress(
            self._observation(record.operation_id, job.job_id, reading, handle), reading, handle
        )

    def _remote_evidence(
        self, record: Operation, job_id: str, evidence: RemoteOperationEvidence | None
    ) -> RemoteOperationEvidence | None:
        """The scheduler-wide evidence of an operation; ``None`` if a known job's cannot be read.

        The evidence resolves an unresolved job identity and carries a cancel marker
        written by another process. Caller-supplied evidence is reused. Without it,
        a known job already cancelled in this cache cannot learn another fact from
        the marker, and the scheduler reading remains authoritative.
        """
        if evidence is not None:
            return evidence
        if record.cancelled and job_id != "0":
            return None
        try:
            return self._runner.inspect_operation(record.operation_id)
        except (
            SlurmError,
            OSError,
            UnicodeError,
            ProcessTimeoutError,
            RemoteOperationError,
        ):
            if job_id == "0":
                raise
            return None

    def _with_progress(
        self,
        observed: ClusterInspectOutcome,
        reading: SchedulerReading,
        handle: ClusterHandle | None,
    ) -> ClusterInspectOutcome:
        """Add the finished-stage count to the observation of a computing multi-stage batch."""
        if (
            isinstance(observed, ClusterObservation)
            and reading.phase is SlurmPhase.RUNNING
            and isinstance(handle, SlurmBatchHandle)
            and len(handle.stages) > 1
        ):
            return observed.model_copy(update={"completed_stages": self._completed_stages(handle)})
        return observed

    def _completed_stages(self, handle: SlurmBatchHandle) -> int | None:
        try:
            return self._runner.completed_stages(handle)
        except (SlurmError, OSError, UnicodeError, ProcessTimeoutError):
            # Stage progress is advisory: failing to read it must not hide the
            # scheduler reading that was already obtained.
            return None

    @staticmethod
    def _observation(
        operation_id: str | None,
        job_id: str,
        reading: SchedulerReading,
        handle: ClusterHandle | None,
    ) -> ClusterInspectOutcome:
        status = reading.status
        if status == SlurmJobStatus.UNKNOWN:
            return ClusterUnknown(
                operation_id=operation_id, job_id=job_id, reason="scheduler state unknown"
            )
        return ClusterObservation(
            operation_id=operation_id,
            job_id=job_id,
            life=reading.life,
            pending_reason=reading.reason if status == SlurmJobStatus.PENDING else None,
            estimated_start=(reading.estimated_start if status == SlurmJobStatus.PENDING else None),
            handle=handle,
        )
