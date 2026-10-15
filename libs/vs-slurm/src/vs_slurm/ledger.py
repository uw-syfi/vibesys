"""The project-scoped evaluation ledger: records, keys and the pure lease policy.

A ledger record names one evaluation request of one project: the job that
serves it, and where that job's result lives on the cluster. It is stored on
the cluster's shared filesystem, under the operator's staging root, so it
outlives the VibeSys process and the run that asked for it. A later process
of the same project, a resume or a fresh relaunch on new code, finds the job
by the request's idempotency key and attaches to it instead of submitting
again.

Every stored document carries its own ``format_version``, independent of any
run-state schema, so a record written by an older build stays readable.

Layout under ``<remote_workspace_root>/<config.name>/ledger/<project>/<key>/``:

- ``attempt-<n>.json``: the record of attempt ``n``. Attempt ``n`` is created
  with an atomic create-only link, so two processes asking for one key get one
  attempt, and one job. Its state then advances by atomic replacement. The
  highest attempt is the current one: a withdrawn attempt is followed by the
  next one when the request is asked for again.
- ``leases/<holder>.json``: one lease per attached run, written only by that
  run, so renewals never contend.

Phase 2 (slot jobs, #2757) adds states and a second job kind to this record;
``job.kind`` and ``format_version`` keep that addition from breaking a reader.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from vs_decode.api import Unreadable, decode_json

from .runner import (
    SlurmArtifactTarget,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchStageHandle,
    SlurmJobHandle,
    _validate_batch_request,
)

if TYPE_CHECKING:
    from .config import SlurmConfig

LEDGER_FORMAT_VERSION = 1
"""The version of every ledger document this build writes."""

BATCH_RESULT_LAYOUT = 1
"""The layout of the result files a batch job writes, which collection reads.

A record names the layout its job writes. A build collects only layouts it
knows, so a change to the batch script's result files must bump this and keep
reading the older layout.
"""

_KEY = re.compile(r"[0-9a-f]{64}")
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_HOLDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class LedgerError(RuntimeError):
    """The ledger could not be read or written; the request's job is unknown, not absent."""

    @classmethod
    def unreadable(cls, path: str, detail: str) -> LedgerError:
        """Name the document that could not be decoded and why."""
        return cls(f"evaluation ledger document {path} is unreadable: {detail}")

    @classmethod
    def unsupported_version(cls, path: str, version: object) -> LedgerError:
        """Name a document written by a newer build, which this build must not guess at."""
        return cls(f"evaluation ledger document {path} has unsupported format_version {version!r}")

    @classmethod
    def invalid_name(cls, kind: str, value: str) -> LedgerError:
        """Name an identifier that cannot be a ledger path component."""
        return cls(f"invalid evaluation ledger {kind} {value!r}")

    @classmethod
    def unrelocatable(cls, path: Path) -> LedgerError:
        """Name an artifact destination outside the workspace the ledger rebases onto."""
        return cls(f"artifact destination {path} is outside the evaluated workspace")

    @classmethod
    def transport(cls, error: BaseException) -> LedgerError:
        """Describe a failed remote ledger operation."""
        return cls(f"evaluation ledger operation failed: {error}")

    @classmethod
    def malformed_reply(cls, operation: str) -> LedgerError:
        """Describe a remote reply that was cut off or not in the expected shape."""
        return cls(f"evaluation ledger {operation} returned a malformed reply")

    @classmethod
    def claim_unreadable(cls, key: str) -> LedgerError:
        """Describe a claim that lost to an attempt this process cannot read back."""
        return cls(f"evaluation ledger request {key} was claimed but cannot be read")


def validate_key(value: str) -> str:
    """Require an idempotency key: a SHA-256 hex digest."""
    if _KEY.fullmatch(value) is None:
        raise LedgerError.invalid_name("key", value)
    return value


def validate_project(value: str) -> str:
    """Require a project identity that is one safe path component."""
    if _PROJECT.fullmatch(value) is None:
        raise LedgerError.invalid_name("project", value)
    return value


def validate_holder(value: str) -> str:
    """Require a lease holder identity that is one safe path component."""
    if _HOLDER.fullmatch(value) is None:
        raise LedgerError.invalid_name("holder", value)
    return value


def evaluation_key(project: str, request: SlurmBatchRequest, config: SlurmConfig) -> str:
    """The idempotency key: a hash of the project, the staged content and the evaluation spec.

    The staged content is the content identity of the candidate workspace and of
    every support tree, the same identity staging uploads under. The spec is what
    shapes the job: each stage's name, command, time limit and declared artifacts,
    the stop policy, setup script and service, and the cluster settings that
    choose where and how the job is submitted. Local paths are not part of it, so
    a fresh relaunch, whose candidate worktree has a new path, asks for the same
    key. The key names nothing a run owns.
    """
    validate_project(project)
    stages = _validate_batch_request(request)
    spec = {
        "stages": [
            {
                "name": stage.name,
                "command": list(stage.command),
                "timeout_seconds": stage.timeout_seconds,
                "file_artifacts": [
                    [item.remote_path, item.collect_on_failure] for item in stage.file_artifacts
                ],
                "tree_artifacts": [item.remote_path for item in stage.tree_artifacts],
            }
            for stage in stages
        ],
        "stop_on_failure": request.stop_on_failure,
        "setup_script": request.setup_script,
        "service": None if request.service is None else request.service.model_dump(mode="json"),
        "cluster": {
            "name": config.name,
            "remote_workspace_root": config.remote_workspace_root,
            # The transport reaches the cluster; a job submitted through another
            # one is not this cluster's (it is part of every job's config identity).
            "transport": config.transport.model_dump(mode="json"),
            "sbatch_command": list(config.sbatch_command),
            "sbatch_arguments": list(config.sbatch_arguments),
        },
    }
    content = {
        "workspace": content_digest(request.workspace, exclude=".git"),
        "support": {
            name: content_digest(path)
            for name, path in sorted((request.support_trees or {}).items())
        },
    }
    document = {"key_format": 1, "project": project, "content": content, "spec": spec}
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def content_digest(root: Path, *, exclude: str | None = None) -> str:
    """A digest of a tree's content: each entry's relative path, kind, mode, and bytes or target.

    Unlike the staging cache identity, it ignores modification times, so a new
    checkout of the same revision has the same digest. ``exclude`` names one
    top-level entry to leave out (the candidate's ``.git``).
    """
    normalized = root.resolve(strict=True)
    digest = hashlib.sha256()
    entries = sorted(
        (
            path
            for path in normalized.rglob("*")
            if exclude is None or path.relative_to(normalized).parts[0] != exclude
        ),
        key=lambda path: path.relative_to(normalized).as_posix(),
    )
    for path in entries:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            kind, payload = "link", os.fsencode(path.readlink())
        elif stat.S_ISDIR(metadata.st_mode):
            kind, payload = "dir", b""
        else:
            kind, payload = "file", path.read_bytes()
        header = json.dumps(
            [path.relative_to(normalized).as_posix(), kind, stat.S_IMODE(metadata.st_mode)]
        ).encode("utf-8", errors="surrogateescape")
        for field in (header, hashlib.sha256(payload).digest()):
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
    return digest.hexdigest()


def ledger_operation_id(key: str, attempt: int) -> str:
    """The cluster operation identity of one attempt; the job's Slurm name derives from it.

    Deriving it from the key alone lets a process that lost the record write find
    the job by name (``Cluster.inspect`` of this identity).
    """
    validate_key(key)
    return f"ev-{key[:48]}-{attempt}"


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class LedgerArtifact(_Document):
    """One declared artifact: where the job writes it, and where it lands, relative to the workspace."""

    remote_path: str
    local_relative: str
    kind: Literal["file", "tree"]
    collect_on_failure: bool = False


class LedgerStage(_Document):
    """One stage of a submitted batch, in order."""

    name: str
    artifacts: tuple[LedgerArtifact, ...] = ()


class SubmitBoundJob(_Document):
    """A job that was bound to this request when it was submitted (Phase 1).

    The locator fields are plain strings and numbers rather than a dump of the
    runner's handle, so a newer build reads them even if that handle changes.
    """

    kind: Literal["submit_bound"] = "submit_bound"
    job_id: Annotated[str, Field(pattern=r"^[0-9]+$")]
    config_identity: str
    remote_workspace: str
    remote_status_path: str
    remote_log_path: str
    stop_on_failure: bool
    stages: tuple[LedgerStage, ...] = Field(min_length=1)
    result_layout: int = BATCH_RESULT_LAYOUT


class LedgerState(StrEnum):
    """Where one attempt of a request stands.

    ``submitting`` records the intent before sbatch; ``submitted`` names the job;
    ``withdrawn`` means no run leased the request for its grace period, and its
    job was cancelled. Phase 2 adds the slot states.
    """

    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    WITHDRAWN = "withdrawn"


class LedgerLabels(_Document):
    """What a person reading the ledger needs to recognize a request; never part of its key."""

    candidate: str | None = None
    workstream: str | None = None


class LedgerRecord(_Document):
    """One attempt of one evaluation request of one project.

    ``owner`` is the holder (a run) that claimed the attempt; ``created_at`` is
    when, in epoch seconds, and counts as its first lease.
    """

    format_version: Literal[1] = 1
    key: str
    project: str
    attempt: int = Field(ge=0)
    operation_id: str
    owner: str
    created_at: float
    labels: LedgerLabels = LedgerLabels()
    state: LedgerState
    job: SubmitBoundJob | None = None
    withdrawn_reason: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> LedgerRecord:
        validate_key(self.key)
        validate_project(self.project)
        validate_holder(self.owner)
        if self.operation_id != ledger_operation_id(self.key, self.attempt):
            message = "operation_id must derive from the key and attempt"
            raise ValueError(message)
        if self.state is LedgerState.SUBMITTING and self.job is not None:
            message = "a submitting record names no job yet"
            raise ValueError(message)
        if self.state is LedgerState.SUBMITTED and self.job is None:
            message = "a submitted record must name its job"
            raise ValueError(message)
        if (self.state is LedgerState.WITHDRAWN) != (self.withdrawn_reason is not None):
            message = "a withdrawn record, and only one, states why"
            raise ValueError(message)
        return self


class LedgerLease(_Document):
    """One attached run's interest in a request, renewed at ``renewed_at`` until ``expires_at``.

    Both are epoch seconds.
    """

    format_version: Literal[1] = 1
    holder: str
    renewed_at: float
    expires_at: float

    @model_validator(mode="after")
    def _safe_holder(self) -> LedgerLease:
        validate_holder(self.holder)
        return self


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """The current attempt of a request and every lease on it."""

    record: LedgerRecord
    leases: tuple[LedgerLease, ...] = ()


def decode_record(text: str, *, path: str) -> LedgerRecord:
    """Read a record of any format this build knows; a newer one is an error, never a guess."""
    return _decode(text, path=path, model=LedgerRecord)


def decode_lease(text: str, *, path: str) -> LedgerLease:
    """Read a lease of any format this build knows."""
    return _decode(text, path=path, model=LedgerLease)


# A record or lease is a few hundred bytes; anything near this bound is not one.
_MAX_DOCUMENT_BYTES = 1 << 20
_DOCUMENT = TypeAdapter(dict[str, object])


def _decode[T: BaseModel](text: str, *, path: str, model: type[T]) -> T:
    document = decode_json(text, _DOCUMENT, source=path, max_bytes=_MAX_DOCUMENT_BYTES)
    if isinstance(document, Unreadable):
        raise LedgerError.unreadable(path, f"not a JSON object ({document.kind.value})")
    version = document.get("format_version")
    if version != LEDGER_FORMAT_VERSION:
        raise LedgerError.unsupported_version(path, version)
    try:
        return model.model_validate(document, strict=False)
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
        raise LedgerError.unreadable(path, f"invalid fields {', '.join(fields)}") from error


def encode(document: LedgerRecord | LedgerLease) -> str:
    """The stored form of a ledger document: one line of JSON."""
    return document.model_dump_json() + "\n"


def job_of_handle(handle: SlurmBatchHandle, workspace: Path) -> SubmitBoundJob:
    """The versioned locator of a submitted batch, its artifacts relative to ``workspace``."""
    root = workspace.resolve()

    def relative(path: Path) -> str:
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise LedgerError.unrelocatable(path)
        return resolved.relative_to(root).as_posix()

    return SubmitBoundJob(
        job_id=handle.job.job_id,
        config_identity=handle.job.config_identity,
        remote_workspace=handle.job.remote_workspace,
        remote_status_path=handle.job.remote_status_path,
        remote_log_path=handle.job.remote_log_path,
        stop_on_failure=handle.stop_on_failure,
        stages=tuple(
            LedgerStage(
                name=stage.name,
                artifacts=tuple(
                    LedgerArtifact(
                        remote_path=item.remote_path,
                        local_relative=relative(item.local_path),
                        kind=item.kind,
                        collect_on_failure=item.collect_on_failure,
                    )
                    for item in (*stage.file_artifacts, *stage.tree_artifacts)
                ),
            )
            for stage in handle.stages
        ),
    )


def handle_of_job(job: SubmitBoundJob, *, operation_id: str, workspace: Path) -> SlurmBatchHandle:
    """The handle that reaches ``job``, its artifacts landing under this run's ``workspace``."""

    def target(item: LedgerArtifact) -> SlurmArtifactTarget:
        local = workspace.joinpath(*PurePosixPath(item.local_relative).parts)
        return SlurmArtifactTarget(
            remote_path=item.remote_path,
            local_path=local,
            kind=item.kind,
            collect_on_failure=item.collect_on_failure,
        )

    return SlurmBatchHandle(
        job=SlurmJobHandle(
            job_id=job.job_id,
            invocation_id=operation_id,
            config_identity=job.config_identity,
            remote_workspace=job.remote_workspace,
            remote_status_path=job.remote_status_path,
            remote_log_path=job.remote_log_path,
        ),
        stages=tuple(
            SlurmBatchStageHandle(
                name=stage.name,
                file_artifacts=tuple(target(a) for a in stage.artifacts if a.kind == "file"),
                tree_artifacts=tuple(target(a) for a in stage.artifacts if a.kind == "tree"),
            )
            for stage in job.stages
        ),
        stop_on_failure=job.stop_on_failure,
        submission_seconds=0.0,
    )


class LedgerPolicy(_Document):
    """Operator policy for the ledger: how long a lease lasts and how long an unleased job lives.

    A run renews its lease about every third of ``lease_seconds``. A job whose
    newest lease expired more than ``grace_seconds`` ago is cancelled.
    """

    lease_seconds: Annotated[float, Field(gt=0)] = 300.0
    grace_seconds: Annotated[float, Field(ge=0)] = 1800.0
    # Whether submitted evaluations keep running while no VibeSys process is attached.
    # Submit-bound jobs (Phase 1) always do: a stop or a crash cancels nothing, and the
    # grace period bounds how long an unleased job lives. Phase 2 slot jobs read it: a
    # slot that starts while no run holds a live lease exits without claiming when false.
    run_while_detached: bool = True


@dataclass(frozen=True, slots=True)
class Leased:
    """A lease holds the request now, or its grace period has not run out."""

    until: float


@dataclass(frozen=True, slots=True)
class Unleased:
    """No lease has held the request since ``since``, more than the grace period ago."""

    since: float


LeaseVerdict = Leased | Unleased


def lease_verdict(entry: LedgerEntry, *, now: float, grace_seconds: float) -> LeaseVerdict:
    """Whether a request is still wanted. Pure: the record's creation counts as its first lease."""
    expired_at = max((lease.expires_at for lease in entry.leases), default=entry.record.created_at)
    until = expired_at + grace_seconds
    return Leased(until) if now <= until else Unleased(expired_at)


@dataclass(frozen=True, slots=True)
class AttachJob:
    """The request has a job: attach to it, whatever its scheduler state."""

    record: LedgerRecord


@dataclass(frozen=True, slots=True)
class ResolveSubmission:
    """An attempt was claimed but names no job yet: find it by name, else submit it.

    Its submitter may have crashed between sbatch and the record write, so the job
    is looked up by the attempt's operation identity before anything is submitted.
    """

    record: LedgerRecord


@dataclass(frozen=True, slots=True)
class ClaimAttempt:
    """No live attempt exists: claim attempt ``attempt``, then submit it."""

    attempt: int


SubmitStep = AttachJob | ResolveSubmission | ClaimAttempt


def submit_step(entry: LedgerEntry | None) -> SubmitStep:
    """What a submission of a request does, given its ledger entry. Pure.

    Only an absent or withdrawn request is submitted; every other state attaches.
    """
    if entry is None:
        return ClaimAttempt(0)
    record = entry.record
    match record.state:
        case LedgerState.SUBMITTED:
            return AttachJob(record)
        case LedgerState.SUBMITTING:
            return ResolveSubmission(record)
        case LedgerState.WITHDRAWN:
            return ClaimAttempt(record.attempt + 1)
        case _:
            assert_never(record.state)


__all__ = [
    "BATCH_RESULT_LAYOUT",
    "LEDGER_FORMAT_VERSION",
    "AttachJob",
    "ClaimAttempt",
    "LeaseVerdict",
    "Leased",
    "LedgerArtifact",
    "LedgerEntry",
    "LedgerError",
    "LedgerLabels",
    "LedgerLease",
    "LedgerPolicy",
    "LedgerRecord",
    "LedgerStage",
    "LedgerState",
    "ResolveSubmission",
    "SubmitBoundJob",
    "SubmitStep",
    "Unleased",
    "content_digest",
    "decode_lease",
    "decode_record",
    "encode",
    "evaluation_key",
    "handle_of_job",
    "job_of_handle",
    "lease_verdict",
    "ledger_operation_id",
    "submit_step",
    "validate_holder",
    "validate_key",
    "validate_project",
]
