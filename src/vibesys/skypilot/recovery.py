"""Machine-local journal for recoverable SkyPilot evaluator invocations."""

from __future__ import annotations

import base64
import binascii
import hashlib
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_project.api import StateNamespace, StateSlot

InvocationId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Sha256Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class InvocationPhase(StrEnum):
    """Durable invocation lifecycle."""

    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ACKNOWLEDGED = "ACKNOWLEDGED"


class ArtifactRecord(BaseModel):
    """Digest-verified client-delivered artifact payload."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    size: Annotated[int, Field(ge=0)]
    sha256: Sha256Digest
    data_base64: str

    @model_validator(mode="after")
    def _valid_payload(self) -> ArtifactRecord:
        try:
            payload = base64.b64decode(self.data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("artifact payload is not valid base64") from exc
        if len(payload) != self.size or hashlib.sha256(payload).hexdigest() != self.sha256:
            raise ValueError("artifact payload does not match its digest")
        return self

    @classmethod
    def create(cls, path: str, payload: bytes) -> ArtifactRecord:
        """Create a self-verifying artifact record."""
        return cls(
            path=path,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            data_base64=base64.b64encode(payload).decode("ascii"),
        )

    def payload(self) -> bytes:
        """Return the already validated artifact bytes."""
        return base64.b64decode(self.data_base64, validate=True)


class InvocationProvenance(BaseModel):
    """Concrete execution provenance captured with a terminal result."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_name: str
    infra: str
    cluster_name: str
    job_name: str
    remote_job_id: int
    attempt: Annotated[int, Field(gt=0)]
    accelerator_type: str
    nodes: Annotated[int, Field(gt=0)]
    accelerators_per_node: Annotated[int, Field(gt=0)]
    runtime_image: str | None = None


class InvocationResultRecord(BaseModel):
    """Replayable semantic result."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["COMPLETED", "APPLICATION_FAILED", "CANCELLED"]
    sky_exit_code: int
    artifact: ArtifactRecord | None = None
    provenance: InvocationProvenance


class AttemptResourcesRecord(BaseModel):
    """Effective resources and policy bound to one remote submission attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_name: str
    infra: str
    accelerator_type: str
    nodes: Annotated[int, Field(gt=0)]
    accelerators_per_node: Annotated[int, Field(gt=0)]
    runtime_image: str | None = None


class InvocationRecord(BaseModel):
    """Strict version-1 invocation journal document."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    invocation_id: InvocationId
    request_sha256: Sha256Digest
    snapshot_sha256: Sha256Digest
    job_name: str
    phase: InvocationPhase
    attempt: Annotated[int, Field(ge=0)] = 0
    remote_job_id: int | None = None
    active_cluster_name: str | None = None
    attempt_resources: AttemptResourcesRecord | None = None
    remote_read_offset: Annotated[int, Field(ge=0)] = 0
    client_delivered_offset: Annotated[int, Field(ge=0)] = 0
    result: InvocationResultRecord | None = None

    @model_validator(mode="after")
    def _consistent_phase(self) -> InvocationRecord:
        if self.client_delivered_offset > self.remote_read_offset:
            raise ValueError("client offset exceeds remote-read offset")
        if self.phase in {InvocationPhase.COMPLETED, InvocationPhase.ACKNOWLEDGED}:
            if self.result is None:
                raise ValueError("terminal invocation is missing its result")
        elif self.result is not None:
            raise ValueError("nonterminal invocation must not contain a result")
        if self.phase is InvocationPhase.PREPARED:
            if self.active_cluster_name is not None or self.attempt_resources is not None:
                raise ValueError("prepared invocation must not be bound to an attempt")
        elif self.attempt_resources is None:
            raise ValueError("remote invocation is missing attempt resources")
        return self


def deterministic_job_name(invocation_id: InvocationId, attempt: int = 1) -> str:
    """Return the caller-owned stable SkyPilot task name for one attempt."""
    return f"vibesys-inv-{hashlib.sha256(invocation_id.encode()).hexdigest()[:12]}-a{attempt}"


class InvocationJournal:
    """Atomic typed transitions over a machine-local state capability."""

    def __init__(
        self,
        namespace: StateNamespace,
        *,
        crash_hook: Callable[[InvocationPhase, InvocationRecord], None] | None = None,
    ) -> None:
        """Bind journal transitions to one caller-provided namespace."""
        self._namespace = namespace
        self._crash_hook = crash_hook

    def load(self, invocation_id: InvocationId) -> InvocationRecord | None:
        """Load one invocation if it exists."""
        return self._slot(invocation_id).load_optional()

    def prepare(
        self,
        invocation_id: InvocationId,
        request_sha256: Sha256Digest,
        snapshot_sha256: Sha256Digest,
    ) -> InvocationRecord:
        """Write PREPARED before any remote side effect, or restore an exact request."""
        existing = self.load(invocation_id)
        if existing is not None:
            if (
                existing.request_sha256 != request_sha256
                or existing.snapshot_sha256 != snapshot_sha256
            ):
                raise ValueError("invocation ID was reused for another request")
            return existing
        return self._save(
            InvocationRecord(
                invocation_id=invocation_id,
                request_sha256=request_sha256,
                snapshot_sha256=snapshot_sha256,
                job_name=deterministic_job_name(invocation_id, 1),
                phase=InvocationPhase.PREPARED,
            )
        )

    def submitting(
        self,
        record: InvocationRecord,
        cluster_name: str,
        resources: AttemptResourcesRecord,
    ) -> InvocationRecord:
        """Persist submission intent before invoking the non-idempotent CLI call."""
        if record.phase is not InvocationPhase.PREPARED:
            raise ValueError("only a prepared invocation can begin submission")
        return self._save(
            record.model_copy(
                update={
                    "phase": InvocationPhase.SUBMITTING,
                    "attempt": record.attempt + 1,
                    "active_cluster_name": cluster_name,
                    "attempt_resources": resources,
                }
            )
        )

    def submitted(
        self, record: InvocationRecord, job_id: int, cluster_name: str
    ) -> InvocationRecord:
        """Record the reconciled remote job identity."""
        if (
            record.remote_job_id == job_id
            and record.active_cluster_name == cluster_name
            and record.phase in {InvocationPhase.SUBMITTED, InvocationPhase.RUNNING}
        ):
            return record
        if record.active_cluster_name not in {None, cluster_name}:
            raise ValueError("submitted cluster differs from submission intent")
        return self._save(
            record.model_copy(
                update={
                    "phase": InvocationPhase.SUBMITTED,
                    "remote_job_id": job_id,
                    "active_cluster_name": cluster_name,
                }
            )
        )

    def offsets(
        self, record: InvocationRecord, *, remote_read: int, client_delivered: int
    ) -> InvocationRecord:
        """Advance independent decoded remote-read and client-delivered offsets."""
        if (
            remote_read < record.remote_read_offset
            or client_delivered < record.client_delivered_offset
        ):
            raise ValueError("invocation offsets must be monotonic")
        return self._save(
            record.model_copy(
                update={
                    "phase": InvocationPhase.RUNNING,
                    "remote_read_offset": remote_read,
                    "client_delivered_offset": client_delivered,
                }
            )
        )

    def completed(
        self, record: InvocationRecord, result: InvocationResultRecord
    ) -> InvocationRecord:
        """Persist the complete replay payload before client delivery."""
        return self._save(
            record.model_copy(update={"phase": InvocationPhase.COMPLETED, "result": result})
        )

    def retry(self, record: InvocationRecord) -> InvocationRecord:
        """Prepare the next bounded infrastructure-recovery attempt."""
        if record.phase not in {
            InvocationPhase.SUBMITTING,
            InvocationPhase.SUBMITTED,
            InvocationPhase.RUNNING,
        }:
            raise ValueError("only an attached invocation can be retried")
        next_attempt = record.attempt + 1
        return self._save(
            record.model_copy(
                update={
                    "phase": InvocationPhase.PREPARED,
                    "job_name": deterministic_job_name(record.invocation_id, next_attempt),
                    "remote_job_id": None,
                    "active_cluster_name": None,
                    "attempt_resources": None,
                    "remote_read_offset": 0,
                    "client_delivered_offset": 0,
                }
            )
        )

    def acknowledge(self, record: InvocationRecord) -> InvocationRecord:
        """Mark a terminal payload delivered without deleting replay provenance."""
        if record.phase is not InvocationPhase.COMPLETED:
            raise ValueError("only completed invocations can be acknowledged")
        return self._save(record.model_copy(update={"phase": InvocationPhase.ACKNOWLEDGED}))

    def _save(self, record: InvocationRecord) -> InvocationRecord:
        self._slot(record.invocation_id).save(record)
        if self._crash_hook is not None:
            self._crash_hook(record.phase, record)
        return record

    @staticmethod
    def _path(invocation_id: InvocationId) -> str:
        return f"invocations/{invocation_id}.json"

    def _slot(self, invocation_id: InvocationId) -> StateSlot[InvocationRecord]:
        return self._namespace.slot(self._path(invocation_id), InvocationRecord)
