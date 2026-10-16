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

- ``attempt-<n>.<r>.json``: revision ``r`` of the record of attempt ``n``.
  Every revision is created with an atomic create-only link and never changed,
  so two processes asking for one key get one attempt, and one job, and a state
  change is a compare-and-swap: the writer of revision ``r + 1`` read revision
  ``r``, and of two writers from one revision exactly one wins. The highest
  revision of the highest attempt is the current record. Only the transitions
  in ``check_transition`` are written; a withdrawn or failed attempt is final,
  and the next ask for the request claims attempt ``n + 1``.
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
from typing import TYPE_CHECKING, Annotated, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from vs_decode.api import Unreadable, decode_json

from .cluster_types import ClusterCollected, ClusterUnknown
from .runner import (
    SERVICE_NOT_READY_EXIT_CODE,
    STAGE_TIMEOUT_EXIT_CODES,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStageResult,
    SlurmJobHandle,
    _validate_batch_request,
    batch_stage_handles,
)
from .scheduler_states import SlurmJobStatus

if TYPE_CHECKING:
    from pathlib import Path

    from .cluster_types import ClusterCollectOutcome
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
    def illegal_transition(cls, before: LedgerRecord, after: LedgerRecord, why: str) -> LedgerError:
        """Name a record change the attempt's state machine forbids."""
        return cls(
            f"evaluation ledger request {before.key[:12]} attempt {before.attempt} cannot go "
            f"from {before.state.value} to {after.state.value}: {why}"
        )

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


def evaluation_key(
    project: str,
    request: SlurmBatchRequest,
    config: SlurmConfig,
    *,
    setup_script_sha256: str | None,
) -> str:
    """The idempotency key: a hash of the project, the content the job reads and its spec.

    What the key covers, so that two requests with one key run the same job:

    - the project;
    - the staged content: the candidate workspace (without ``.git``) and every
      support tree, by relative path, file kind, executable bit and bytes;
    - the setup script the job sources on the cluster: its path and the SHA-256
      of its bytes when the key is computed (``setup_script_sha256``, read from
      the cluster by the caller; None when the file is absent there);
    - the spec: each stage's name, command, time limit and declared artifacts,
      the stop policy, the service (command, readiness URL, startup bound), and
      the cluster settings that choose where and how the job is submitted (name,
      staging root, transport, sbatch command and arguments).

    It does not cover files outside these that a command or the setup script
    reads on the cluster by absolute path (site modules, datasets), nor the
    environment ``sbatch`` passes to the job; an operator who changes those
    must remove the affected entries (``vibesys evals remove``). Local paths
    are not part of the key, so a fresh relaunch, whose candidate worktree has
    a new path, asks for the same key. The key names nothing a run owns.
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
        "setup_script": (
            None
            if request.setup_script is None
            else {"path": request.setup_script, "sha256": setup_script_sha256}
        ),
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
    document = {"key_format": 2, "project": project, "content": content, "spec": spec}
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


_EXECUTABLE = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def content_digest(root: Path, *, exclude: str | None = None) -> str:
    """A digest of a tree's content: each entry's relative path, kind, and bytes or target.

    Of a file's mode only the executable bit counts, so the same checkout under
    another umask has the same digest. Unlike the staging cache identity, it
    ignores modification times, so a new checkout of the same revision has the
    same digest. File contents are streamed, never read whole. ``exclude`` names
    one top-level entry to leave out (the candidate's ``.git``).

    Only regular files, symbolic links and directories count. Special files
    (FIFOs, sockets, character and block devices) are skipped and never opened:
    opening a FIFO blocks until a writer appears, and none of them holds
    content a job could read back.
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
        executable = False
        if stat.S_ISLNK(metadata.st_mode):
            kind, payload = "link", hashlib.sha256(os.fsencode(path.readlink())).digest()
        elif stat.S_ISDIR(metadata.st_mode):
            kind, payload = "dir", hashlib.sha256(b"").digest()
        elif stat.S_ISREG(metadata.st_mode):
            kind, executable = "file", bool(metadata.st_mode & _EXECUTABLE)
            with path.open("rb") as stream:
                payload = hashlib.file_digest(stream, "sha256").digest()
        else:
            continue  # a FIFO, socket or device: never opened (see the docstring)
        header = json.dumps([path.relative_to(normalized).as_posix(), kind, executable]).encode(
            "utf-8", errors="surrogateescape"
        )
        for field in (header, payload):
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
    """One declared artifact: where the job writes it, relative to its remote workspace.

    Where it lands locally is not recorded: every attaching run collects into its
    own workspace, at the destination its own request declares.
    """

    remote_path: str
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

    ``submitting`` records the intent before sbatch, until ``claim_expires_at``;
    ``submitted`` names the job; ``completed`` means a run collected the job's
    results and every stage left its own (``settlement``), so later asks reuse
    it; ``failed`` means the job ended any other way, its results could not be
    collected, a stage timed out or was killed, or the submission never
    produced a job, so the next ask starts a new attempt; ``withdrawn`` means an
    operator removed it or no run leased it for its grace period. ``failed`` and
    ``withdrawn`` are final. Phase 2 adds the slot states.
    """

    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    FAILED = "failed"
    WITHDRAWN = "withdrawn"

    @property
    def final(self) -> bool:
        """Whether nothing may follow this state in its attempt."""
        return not _NEXT_STATES[self]

    @property
    def settled(self) -> bool:
        """Whether the attempt's job, if any, has ended: the cluster need not be asked again."""
        return self in {LedgerState.COMPLETED, LedgerState.FAILED, LedgerState.WITHDRAWN}


_NEXT_STATES: dict[LedgerState, frozenset[LedgerState]] = {
    LedgerState.SUBMITTING: frozenset(
        {LedgerState.SUBMITTED, LedgerState.FAILED, LedgerState.WITHDRAWN}
    ),
    LedgerState.SUBMITTED: frozenset(
        {LedgerState.COMPLETED, LedgerState.FAILED, LedgerState.WITHDRAWN}
    ),
    # An operator may remove a finished entry, so that the next ask runs it again.
    LedgerState.COMPLETED: frozenset({LedgerState.WITHDRAWN}),
    LedgerState.FAILED: frozenset(),
    LedgerState.WITHDRAWN: frozenset(),
}
_ENDED_WITH_REASON = frozenset({LedgerState.FAILED, LedgerState.WITHDRAWN})
_NAMES_A_JOB = frozenset({LedgerState.SUBMITTED, LedgerState.COMPLETED})


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
    # Until when the claim holds while ``submitting``; then another asker may take over.
    claim_expires_at: float
    labels: LedgerLabels = LedgerLabels()
    state: LedgerState
    job: SubmitBoundJob | None = None
    # Why a failed or withdrawn attempt ended; only those, and every one of them, say.
    reason: str | None = None

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
        if self.state in _NAMES_A_JOB and self.job is None:
            message = "a submitted or completed record must name its job"
            raise ValueError(message)
        if (self.state in _ENDED_WITH_REASON) != (self.reason is not None):
            message = "a failed or withdrawn record, and only one, states why"
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
class UnreadableLease:
    """A lease this build cannot decode: its holder and when its file was last written.

    ``written_at`` is the file's modification time on the ledger's filesystem,
    in epoch seconds: a live holder renews its lease, and so rewrites the file,
    every third of a lease period, whatever build it runs.
    """

    holder: str
    written_at: float


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """The current attempt of a request, the revision it was read at, and every lease on it.

    ``revision`` is what a compare-and-swap (``EvaluationLedger.replace``) is
    against: revision 0 is the claim. ``unreadable_leases`` are the leases
    this build cannot decode (a newer ``format_version``, or a damaged file):
    each counts as a holder of unknown kind, renewed when its file was last
    written, so reading never fails on one and it holds the request for one
    claim window after its last write (``lease_verdict``, ``claim_expired``).
    ``earlier`` holds the current record of every earlier attempt, oldest first;
    each is final, since a new attempt is claimed only after the last one ended.
    """

    record: LedgerRecord
    leases: tuple[LedgerLease, ...] = ()
    revision: int = 0
    unreadable_leases: tuple[UnreadableLease, ...] = ()
    earlier: tuple[LedgerRecord, ...] = ()

    @property
    def claim_window(self) -> float:
        """How long, in seconds, the current attempt's claim holds without a renewal."""
        return self.record.claim_expires_at - self.record.created_at


def check_transition(before: LedgerRecord, after: LedgerRecord) -> None:
    """Refuse a record change outside the attempt's state machine. Pure.

    Only the state, the job (once, when it is first named) and the reason
    change; a final state never changes.
    """
    fixed = ("key", "project", "attempt", "operation_id", "owner", "created_at", "labels")
    if any(getattr(before, name) != getattr(after, name) for name in fixed):
        raise LedgerError.illegal_transition(before, after, "it changes the attempt's identity")
    if (
        before.claim_expires_at != after.claim_expires_at
        or after.state not in _NEXT_STATES[before.state]
    ):
        raise LedgerError.illegal_transition(before, after, "the state machine does not allow it")
    if before.job is not None and after.job != before.job:
        raise LedgerError.illegal_transition(before, after, "it changes the attempt's job")


def transitioned(
    record: LedgerRecord,
    state: LedgerState,
    *,
    job: SubmitBoundJob | None = None,
    reason: str | None = None,
) -> LedgerRecord:
    """The record moved to ``state``, keeping its job unless it names one now. Pure."""
    after = record.model_copy(
        update={"state": state, "job": job if job is not None else record.job, "reason": reason}
    )
    after = LedgerRecord.model_validate(after.model_dump())
    check_transition(record, after)
    return after


# A stage stopped from outside rather than by its own command: by its time limit
# (``STAGE_TIMEOUT_EXIT_CODES``) or by SIGTERM (143), which Slurm sends on preemption
# and on a scancel; or one that reports the service it measures never became ready
# (``SERVICE_NOT_READY_EXIT_CODE``), which a node fault causes as often as the
# candidate. Its result is not reused: the next ask runs it again.
_INTERRUPTED_EXIT_CODES = STAGE_TIMEOUT_EXIT_CODES | {143, SERVICE_NOT_READY_EXIT_CODE}


@dataclass(frozen=True, slots=True)
class Settlement:
    """The settled state of an attempt whose job ended, and why it failed, if it did."""

    state: Literal[LedgerState.COMPLETED, LedgerState.FAILED]
    reason: str | None = None


def settlement(
    status: SlurmJobStatus, collected: ClusterCollectOutcome | None, *, job: SubmitBoundJob
) -> Settlement | None:
    """How an attempt whose job reached ``status`` settles; None when it cannot settle yet. Pure.

    The batch script exits 0 whatever its stages do, so Slurm's COMPLETED says
    only that the allocation ran to its end. An attempt is reused only once a
    run collected its results (``collected``) and every stage left its own: a
    stage that ran its command to an exit code, failing ones included, is a
    valid evaluation result, and a stage skipped after a failure is part of it.
    A stage that timed out, was killed, or left no result file, and result
    files that are missing or malformed, settle it ``failed``, so the next ask
    runs it again. A job that ended any other way than COMPLETED is ``failed``
    with no collection. A COMPLETED job not collected yet (``collected`` None),
    or whose collection read no result (a ``ClusterUnknown`` without a
    result: the connection dropped before, during or after a download, the
    scheduler did not answer),
    cannot settle: its result files may be whole, and a run that attaches to it
    collects them again.
    """
    if not status.terminal:
        return None
    if status is not SlurmJobStatus.COMPLETED:
        return Settlement(LedgerState.FAILED, f"job {job.job_id} ended {status.value}")
    if collected is None or (isinstance(collected, ClusterUnknown) and collected.result is None):
        return None
    problem = _unreusable(collected, job)
    if problem is None:
        return Settlement(LedgerState.COMPLETED)
    return Settlement(LedgerState.FAILED, f"job {job.job_id} finished, but {problem}")


def _unreusable(collected: ClusterCollectOutcome, job: SubmitBoundJob) -> str | None:
    """Why collected results are no evaluation result to reuse; None when they are one."""
    result = collected.result if isinstance(collected, ClusterCollected) else None
    if not isinstance(result, SlurmBatchResult) or result.collection_failure is not None:
        if isinstance(collected, ClusterUnknown):
            detail = collected.reason
        elif isinstance(result, SlurmBatchResult):
            detail = str(result.collection_failure)
        else:
            detail = "they are not a batch's"
        return f"its results could not be collected: {detail}"
    if result.job_exit_code != 0:
        return f"its batch exited with code {result.job_exit_code}"
    by_name = {stage.name: stage for stage in result.stages}
    problems = (_stage_problem(stage.name, by_name.get(stage.name)) for stage in job.stages)
    return next((problem for problem in problems if problem is not None), None)


def _stage_problem(name: str, item: SlurmBatchStageResult | None) -> str | None:
    """Why one stage's collected result is no result of its command; None when it is one."""
    if (
        item is None
        or item.collection_failure is not None
        or (item.exit_code is None and not item.skipped)
    ):
        return f"stage {name!r} left no result"
    if item.exit_code in _INTERRUPTED_EXIT_CODES:
        return f"stage {name!r} timed out or was killed (exit code {item.exit_code})"
    return None


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


def job_of_handle(handle: SlurmBatchHandle) -> SubmitBoundJob:
    """The versioned locator of a submitted batch: where the job and its results are."""
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
                        kind=item.kind,
                        collect_on_failure=item.collect_on_failure,
                    )
                    for item in (*stage.file_artifacts, *stage.tree_artifacts)
                ),
            )
            for stage in handle.stages
        ),
    )


def handle_of_job(
    job: SubmitBoundJob, *, operation_id: str, request: SlurmBatchRequest
) -> SlurmBatchHandle:
    """The handle that reaches ``job``, its artifacts landing where ``request`` declares them.

    The job locator comes from the ledger; every local path comes from the
    attaching run's own request, so a run in another worktree than the submitter
    collects into its own. The request asks for the same key, so its stages and
    remote artifacts are the job's; a mismatch is an unreadable record.
    """
    stages = batch_stage_handles(request)
    declared = tuple(
        (
            stage.name,
            tuple(
                (item.remote_path, item.kind, item.collect_on_failure)
                for item in (*stage.file_artifacts, *stage.tree_artifacts)
            ),
        )
        for stage in stages
    )
    recorded = tuple(
        (
            stage.name,
            tuple(
                (item.remote_path, item.kind, item.collect_on_failure) for item in stage.artifacts
            ),
        )
        for stage in job.stages
    )
    if declared != recorded:
        raise LedgerError.unreadable(operation_id, "its stages differ from the request's")
    return SlurmBatchHandle(
        job=SlurmJobHandle(
            job_id=job.job_id,
            invocation_id=operation_id,
            config_identity=job.config_identity,
            remote_workspace=job.remote_workspace,
            remote_status_path=job.remote_status_path,
            remote_log_path=job.remote_log_path,
        ),
        stages=stages,
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
    # How long a claim holds a request while its submitter stages and submits the
    # job. A claim older than this with no job under its name is taken over by
    # the next asker, so a submitter that died before sbatch blocks no one longer.
    claim_seconds: Annotated[float, Field(gt=0)] = 900.0
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
    """Whether a request is still wanted. Pure: the record's creation counts as its first lease.

    A lease this build cannot read has an unknown expiry, and a newer build's
    holder may well be alive: it counts as expiring one claim window after its
    file was last written. A live holder of any build rewrites it far sooner;
    one that crashed stops blocking the request then.
    """
    expired_at = max(
        (
            *(lease.expires_at for lease in entry.leases),
            *(lease.written_at + entry.claim_window for lease in entry.unreadable_leases),
        ),
        default=entry.record.created_at,
    )
    until = expired_at + grace_seconds
    return Leased(until) if now <= until else Unleased(expired_at)


@dataclass(frozen=True, slots=True)
class AttachJob:
    """The request's job finished successfully: attach to it and collect its results."""

    record: LedgerRecord


@dataclass(frozen=True, slots=True)
class CheckJob:
    """The request names a job: attach to it unless it has ended.

    The shell asks the cluster. A job that ended unsuccessfully settles the
    attempt ``failed`` (``settlement``, a swap against ``entry``) and the request
    is read again; a COMPLETED one is attached to, and collected, like a live one.
    """

    entry: LedgerEntry


@dataclass(frozen=True, slots=True)
class ResolveSubmission:
    """An attempt was claimed but names no job yet: find its job by name.

    Its submitter may have crashed between sbatch and the record write, so the
    job is looked up by the attempt's operation identity. With no job found,
    a live claim (``expired`` false) is waited on; an expired one is taken over:
    the attempt fails and the next read claims a new one. ``expired`` is
    ``claim_expired``: the claim holds while its submitter renews it.
    """

    entry: LedgerEntry
    expired: bool


def claim_expired(entry: LedgerEntry, *, now: float) -> bool:
    """Whether a ``submitting`` attempt's claim has run out on its latest renewal. Pure.

    A submitter renews its claim as it renews a lease: while it stages and
    submits the job, it keeps its own lease on the request (the claim's
    ``owner``) current, and each renewal extends the claim by the claim's own
    window (``claim_expires_at - created_at``) from ``renewed_at``. So a slow
    but live submitter is never taken over, and a dead one is taken over one
    window after its last renewal. An owner lease this build cannot read counts
    as renewed when its file was last written.
    """
    record = entry.record
    renewed = max(
        (
            record.created_at,
            *(lease.renewed_at for lease in entry.leases if lease.holder == record.owner),
            *(
                lease.written_at
                for lease in entry.unreadable_leases
                if lease.holder == record.owner
            ),
        )
    )
    return now > max(record.claim_expires_at, renewed + entry.claim_window)


@dataclass(frozen=True, slots=True)
class ClaimAttempt:
    """No usable attempt exists: claim attempt ``attempt``, then submit it."""

    attempt: int


SubmitStep = AttachJob | CheckJob | ResolveSubmission | ClaimAttempt


def submit_step(entry: LedgerEntry | None, *, now: float) -> SubmitStep:
    """What a submission of a request does, given its ledger entry, at ``now``. Pure.

    Only an absent request, or one whose current attempt failed or was
    withdrawn, is submitted; every other state attaches or waits.
    """
    if entry is None:
        return ClaimAttempt(0)
    record = entry.record
    match record.state:
        case LedgerState.COMPLETED:
            return AttachJob(record)
        case LedgerState.SUBMITTED:
            return CheckJob(entry)
        case LedgerState.SUBMITTING:
            return ResolveSubmission(entry, expired=claim_expired(entry, now=now))
        case LedgerState.FAILED | LedgerState.WITHDRAWN:
            return ClaimAttempt(record.attempt + 1)
        case _:
            assert_never(record.state)


__all__ = [
    "BATCH_RESULT_LAYOUT",
    "LEDGER_FORMAT_VERSION",
    "AttachJob",
    "CheckJob",
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
    "Settlement",
    "SubmitBoundJob",
    "SubmitStep",
    "Unleased",
    "UnreadableLease",
    "check_transition",
    "claim_expired",
    "content_digest",
    "decode_lease",
    "decode_record",
    "encode",
    "evaluation_key",
    "handle_of_job",
    "job_of_handle",
    "lease_verdict",
    "ledger_operation_id",
    "settlement",
    "submit_step",
    "transitioned",
    "validate_holder",
    "validate_key",
    "validate_project",
]
