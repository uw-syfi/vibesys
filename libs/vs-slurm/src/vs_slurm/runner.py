"""Stage workspaces and execute commands on a remote Slurm cluster."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from vs_decode.api import Unreadable, decode_json
from vs_sim.api import (
    DISCARD,
    ProcessGroupUnreaped,
    ProcessOutcome,
    ProcessOutputTooLarge,
    ProcessSpec,
    ProcessTimedOut,
    ProcessTimeoutError,
)

from . import connector_protocol
from .config import (
    PORT_PLACEHOLDER,
    SlurmConfig,
    SlurmConfigError,
    SlurmConnectorTransport,
    SlurmJobResources,
    SlurmLocalTransport,
    SlurmService,
    SlurmSshTransport,
    minimal_job,
    shell_join_with_port,
)
from .errors import (
    SlurmError,
    SlurmSbatchFailedError,
    SlurmSubmissionRejectedError,
    SlurmTransferInterruptedError,
    SlurmTransportFailedError,
)
from .job_teardown import start_in_own_session, teardown_shell
from .queue_view import QueueSnapshot, parse_queue_snapshot, queue_snapshot_command
from .remote_file import SlurmFileSource
from .remote_operations import (
    RemoteOperationClaim,
    RemoteOperationEvidence,
    RemoteOperations,
    operation_job_name,
)
from .scheduler_read import framed, unframed
from .scheduler_states import (
    INITIAL_LIFE,
    JobLife,
    NotInQueue,
    QueueRow,
    SlurmJobStatus,
    SlurmPhase,
    Unknown,
    needs_accounting,
    observe,
    parse_accounting_row,
    parse_queue_row,
    phase_of,
    status_of,
)
from .staging import _ContentStageError, _stage_tree, _TreeStageRequest

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_sim.api import Clock, Event, Processes

# The whole line, newline included: an answer cut off mid-number ("Submitted batch
# job 50" of job 5000) would otherwise name another job.
_JOB_ID = re.compile(r"^Submitted batch job ([0-9]{1,32})\r?\n", re.MULTILINE)
# The allocation's own exit status when its server never answered the readiness probe
# (it exited or hung while starting), so no stage ran. The job script writes it.
SERVICE_NOT_READY_EXIT_CODE = 70
# A stage's exit status when its time limit stopped it: ``timeout(1)`` exits 124 after
# its TERM, and 137 when the stage outlived the TERM and ``--kill-after`` killed it.
STAGE_TIMEOUT_EXIT_CODES = frozenset({124, 137})
_MAX_PROCESS_EXIT_CODE = 255
_BATCH_RESULT_ROOT = ".vibesys-slurm-results"
_SERVICE_LOG_TAIL = "service-log-tail.txt"
_SERVICE_LOG_TAIL_LINES = 400
_SERVICE_LOG_REPORTED_LINES = 40
_SERVICE_LOG_LINE_CHARS = 400
_SERVICE_LOG_TAIL_CHARS = 8_000
_CONTENT_CACHE_ROOT = ".vibesys-content-cache"
_STAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")


@dataclass(frozen=True)
class _TransportResponse:
    stdout: str
    stderr: str


@dataclass(frozen=True)
class SlurmFileArtifact:
    """A candidate-relative remote file copied to an explicit local path.

    Artifacts are copied when the job succeeds. ``collect_on_failure`` also
    copies this one when the job ran and exited nonzero, if the job wrote it:
    a failed job may have reported why in it.
    """

    remote_path: str
    local_path: Path
    collect_on_failure: bool = False


@dataclass(frozen=True)
class SlurmTreeArtifact:
    """A candidate-relative remote directory synchronized to a local directory."""

    remote_path: str
    local_path: Path


@dataclass(frozen=True)
class SlurmJobRequest:
    """One command, its staged inputs, and requested output artifacts."""

    workspace: Path
    command: Sequence[str]
    setup_script: str | None = None
    service: SlurmService | None = None
    support_trees: Mapping[str, Path] | None = None
    file_artifacts: tuple[SlurmFileArtifact, ...] = ()
    tree_artifacts: tuple[SlurmTreeArtifact, ...] = ()
    cancel_event: Event | None = None
    resources: SlurmJobResources = SlurmJobResources.CONFIGURED


@dataclass(frozen=True)
class SlurmJobResult:
    """Trusted scheduler identity, process status, and combined job output."""

    job_id: str
    exit_code: int | None
    output: str
    collection_failure: str | None = None


@dataclass(frozen=True)
class SlurmBatchStage:
    """One ordered command in an allocation-scoped batch evaluation."""

    name: str
    command: Sequence[str]
    timeout_seconds: int | None = None
    file_artifacts: tuple[SlurmFileArtifact, ...] = ()
    tree_artifacts: tuple[SlurmTreeArtifact, ...] = ()


@dataclass(frozen=True)
class _JobScriptRequest:
    workspace: PurePosixPath
    status_path: PurePosixPath
    command: Sequence[str]
    setup_script: str | None
    service: SlurmService | None
    service_stop_grace_seconds: int
    process_exit_wait_seconds: int
    phase_timing_root: PurePosixPath | None = None


@dataclass(frozen=True)
class SlurmBatchRequest:
    """Ordered stages sharing one staged workspace, setup, service, and job."""

    workspace: Path
    stages: tuple[SlurmBatchStage, ...]
    stop_on_failure: bool = True
    setup_script: str | None = None
    service: SlurmService | None = None
    support_trees: Mapping[str, Path] | None = None
    cancel_event: Event | None = None


@dataclass(frozen=True)
class SchedulerReading:
    """One scheduler observation: a job life and the queue's evidence for it.

    ``status``, ``phase`` and ``attempt`` are projections of ``life``.
    """

    life: JobLife
    reason: str | None = None
    estimated_start: str | None = None

    @property
    def status(self) -> SlurmJobStatus:
        """The public status of the life."""
        return status_of(self.life)

    @property
    def phase(self) -> SlurmPhase:
        """The phase of the life."""
        return phase_of(self.life)

    @property
    def attempt(self) -> int:
        """The scheduler attempt of the life."""
        return self.life.attempt


class SlurmArtifactTarget(BaseModel):
    """Artifact mapping retained in a recoverable operation handle."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    remote_path: str
    local_path: Path
    kind: Literal["file", "tree"]
    collect_on_failure: bool = False


class SlurmJobHandle(BaseModel):
    """Credential-free durable locator for one submitted Slurm job.

    Persist this model with the caller's run state. A new ``SlurmJobRunner``
    built from the same operator configuration can poll, cancel, wait for, or
    collect the operation without submitting it again.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    job_id: str
    invocation_id: str
    config_identity: str
    remote_workspace: str
    remote_status_path: str
    remote_log_path: str
    artifacts: tuple[SlurmArtifactTarget, ...] = ()
    staging_seconds: float = 0.0
    content_cache_hits: int = 0

    @field_validator("job_id")
    @classmethod
    def _numeric_job_id(cls, value: str) -> str:
        if re.fullmatch(r"[0-9]{1,32}", value) is None:
            raise ValueError
        return value

    @field_validator("invocation_id")
    @classmethod
    def _safe_invocation_id(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
            raise ValueError
        return value

    @field_validator("config_identity")
    @classmethod
    def _valid_config_identity(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError
        return value

    @model_validator(mode="after")
    def _safe_remote_paths(self) -> SlurmJobHandle:
        workspace = _normalized_remote_path(self.remote_workspace)
        status = _normalized_remote_path(self.remote_status_path)
        log = _normalized_remote_path(self.remote_log_path)
        if (
            workspace.name != "workspace"
            or status.parent != workspace.parent
            or log.parent != workspace.parent
            or status == log
        ):
            raise ValueError
        for artifact in self.artifacts:
            _safe_relative_path(artifact.remote_path, SlurmError.invalid_artifact_path)
        if not math.isfinite(self.staging_seconds) or self.staging_seconds < 0:
            raise ValueError
        if self.content_cache_hits < 0:
            raise ValueError
        return self


class SlurmBatchStageHandle(BaseModel):
    """Durable result and artifact locations for one batch stage."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    file_artifacts: tuple[SlurmArtifactTarget, ...] = ()
    tree_artifacts: tuple[SlurmArtifactTarget, ...] = ()

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        if _STAGE_NAME.fullmatch(value) is None:
            raise ValueError
        return value

    @model_validator(mode="after")
    def _safe_artifacts(self) -> SlurmBatchStageHandle:
        for artifact in (*self.file_artifacts, *self.tree_artifacts):
            path = _safe_relative_path(artifact.remote_path, SlurmError.invalid_artifact_path)
            if path.parts[0] == _BATCH_RESULT_ROOT:
                raise ValueError
        return self


class SlurmBatchHandle(BaseModel):
    """Recoverable handle for a batch and its stage result contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    job: SlurmJobHandle
    stages: tuple[SlurmBatchStageHandle, ...]
    stop_on_failure: bool = True
    submission_seconds: float
    waited_seconds: float = 0.0

    @model_validator(mode="after")
    def _unique_stages(self) -> SlurmBatchHandle:
        names = tuple(stage.name for stage in self.stages)
        if not names or len(set(names)) != len(names):
            raise ValueError
        if (
            not math.isfinite(self.submission_seconds)
            or self.submission_seconds < 0
            or not math.isfinite(self.waited_seconds)
            or self.waited_seconds < 0
        ):
            raise ValueError
        return self


@dataclass(frozen=True)
class SlurmBatchStageResult:
    """Stage evidence, with unknown outcomes and incomplete collection explicit."""

    name: str
    exit_code: int | None
    stdout: str
    stderr: str
    elapsed_seconds: float | None
    skipped: bool
    artifacts: tuple[SlurmArtifactTarget, ...] = ()
    collection_failure: str | None = None


@dataclass(frozen=True)
class SlurmBatchResult:
    """One allocation result with ordered stage outcomes and phase timings."""

    job_id: str
    job_exit_code: int | None
    job_output: str
    stages: tuple[SlurmBatchStageResult, ...]
    phase_timings_seconds: Mapping[str, float]
    content_cache_hits: int
    service_log_tail: str = ""
    """Last distinct lines of the shared service's log, empty without a service."""
    collection_failure: str | None = None


@dataclass(frozen=True)
class SlurmBatchWaitResult:
    """Updated durable batch handle after one bounded wait observation."""

    handle: SlurmBatchHandle
    status: SlurmJobStatus
    timed_out: bool


class SlurmJobWaitResult(BaseModel):
    """Result of observing a job for a bounded interval.

    ``timed_out`` means the observation interval elapsed while the job was
    still nonterminal. It never means the scheduler job was cancelled.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    handle: SlurmJobHandle
    status: SlurmJobStatus
    timed_out: bool
    waited_seconds: float
    """How long this observation lasted, on the runner's clock."""

    @property
    def terminal(self) -> bool:
        """Whether the scheduler reported a terminal job state."""
        return self.status.terminal


@dataclass
class _SubmissionBoundary:
    started: bool = False


class SlurmJobRunner:
    """Run a command in Slurm without knowing cluster credentials or transport."""

    def __init__(  # noqa: PLR0913  # LW-951301; each keyword is an independent injected effect or local resource.
        # > A settings object would group unrelated effects (process, clock,
        # > identity, scratch) into a shallow carrier; carrying scratch on each
        # > request cannot reach collect(), which receives only a durable handle.
        self,
        config: SlurmConfig,
        *,
        processes: Processes,
        clock: Clock,
        invocation_id: Callable[[], str] = lambda: uuid.uuid4().hex,
        scratch_root: Path | None = None,
        host_environment: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        """Create a runner with injectable process, clock, and identity effects.

        ``processes`` starts every transport program: ssh and rsync, the local shell, or a
        connector command. ``clock`` times staging, submission and the job deadline. ``host_environment``
        returns this process's environment, from which the local transport takes its
        login-like one (``login_environment``); this process's own when omitted.

        ``scratch_root`` holds the runner's own transfer files (the job script,
        collected status, log, and batch results); the system temporary
        directory when omitted. A caller whose transport only moves files under
        declared roots, such as the host-owned Slurm broker, passes one of them.
        """
        self._config = config
        self._scratch_root = scratch_root
        self._transport = _make_transport(
            config,
            processes=processes,
            host_environment=host_environment or (lambda: os.environ),
        )
        self._operations = RemoteOperations(self._transport, config, scratch_root)
        self._clock = clock
        self._invocation_id = invocation_id

    @property
    def config(self) -> SlurmConfig:
        """The cluster settings this runner submits with."""
        return self._config

    def run(self, request: SlurmJobRequest, *, pause: Callable[[float], None]) -> SlurmJobResult:
        """Stage, submit, wait, and collect as a blocking compatibility API.

        ``pause`` waits between polls, as in :meth:`wait`.
        """
        if request.cancel_event is not None and request.cancel_event.is_set():
            raise SlurmError.cancelled_before_submission()
        handle = self.submit(request)
        try:
            outcome = self._await_terminal(handle, request.cancel_event, pause)
        except BaseException as failure:
            # This call submitted the job, so no other owner can cancel it:
            # whatever ends the wait (a cancel, a timeout, a failed poll, an
            # interrupt) cancels the job, and a failed cancel travels with it.
            self._cancel_abandoned(handle, failure)
            raise
        if outcome.status == SlurmJobStatus.CANCELLED:
            raise SlurmError.cancelled(handle.job_id)
        return self.collect(handle)

    def submit(
        self, request: SlurmJobRequest, *, operation_id: str | None = None
    ) -> SlurmJobHandle:
        """Stage a workspace and submit once, returning a recoverable handle."""
        return self._stage_and_submit(request, operation_id=operation_id)

    def _stage_and_submit(
        self,
        request: SlurmJobRequest,
        *,
        phase_timing_root: PurePosixPath | None = None,
        operation_id: str | None = None,
    ) -> SlurmJobHandle:
        boundary = _SubmissionBoundary()
        try:
            return self._stage_and_submit_at_boundary(
                request,
                phase_timing_root=phase_timing_root,
                boundary=boundary,
                operation_id=operation_id,
            )
        except (SlurmError, OSError, UnicodeError, ProcessTimeoutError) as error:
            if boundary.started:
                raise
            raise SlurmSubmissionRejectedError(str(error)) from error

    def _stage_and_submit_at_boundary(
        self,
        request: SlurmJobRequest,
        *,
        phase_timing_root: PurePosixPath | None,
        boundary: _SubmissionBoundary,
        operation_id: str | None,
    ) -> SlurmJobHandle:
        source, support, files, trees = _validate_request(request)
        if request.cancel_event is not None and request.cancel_event.is_set():
            raise SlurmError.cancelled_before_submission()
        invocation = _safe_component(
            self._invocation_id() if operation_id is None else operation_id
        )
        base = PurePosixPath(self._config.remote_workspace_root) / self._config.name / invocation
        remote_workspace = base / "workspace"
        remote_script = base / "run.sbatch"
        remote_status = base / "exit-code.txt"
        remote_log = base / "job.log"
        cache_root = (
            PurePosixPath(self._config.remote_workspace_root)
            / self._config.name
            / _CONTENT_CACHE_ROOT
        )

        staging_started = self._clock.now()
        self._transport.exec(
            f"mkdir -p {shlex.quote(base.as_posix())} {shlex.quote(cache_root.as_posix())}"
        )
        cache_hits = 0
        try:
            workspace_stage = _stage_tree(
                self._transport,
                _TreeStageRequest(
                    source=source,
                    cache_root=cache_root,
                    destination=remote_workspace,
                    trusted_parent=base,
                    staging_id=f"{invocation}.workspace",
                    excludes=(".git/",),
                ),
            )
            cache_hits += int(workspace_stage.cache_hit)
            for index, (relative, local_path) in enumerate(support):
                support_stage = _stage_tree(
                    self._transport,
                    _TreeStageRequest(
                        source=local_path,
                        cache_root=cache_root,
                        destination=remote_workspace / relative,
                        trusted_parent=remote_workspace,
                        staging_id=f"{invocation}.support{index}",
                    ),
                )
                cache_hits += int(support_stage.cache_hit)
        except _ContentStageError as exc:
            if exc.recoverable:
                raise SlurmError.content_cache_busy(str(exc)) from exc
            raise SlurmError.content_stage_failed() from exc
        staging_seconds = max(0.0, self._clock.now() - staging_started)

        with tempfile.TemporaryDirectory(prefix="vs-slurm-", dir=self._scratch_root) as temporary:
            temporary_path = Path(temporary)
            local_script = temporary_path / "run.sbatch"
            local_script.write_text(
                _job_script(
                    _JobScriptRequest(
                        workspace=remote_workspace,
                        status_path=remote_status,
                        command=request.command,
                        setup_script=request.setup_script,
                        service=request.service,
                        service_stop_grace_seconds=self._config.service_stop_grace_seconds,
                        process_exit_wait_seconds=self._config.process_exit_wait_seconds,
                        phase_timing_root=phase_timing_root,
                    )
                ),
                encoding="utf-8",
            )
            self._transport.put(local_script, remote_script)
            boundary.started = True
            job_id = self._submit(base, remote_script, remote_log, request.resources)
        artifacts = (
            *(
                SlurmArtifactTarget(
                    remote_path=path.as_posix(),
                    local_path=target,
                    kind="file",
                    collect_on_failure=item.collect_on_failure,
                )
                for (path, target), item in zip(files, request.file_artifacts, strict=True)
            ),
            *(
                SlurmArtifactTarget(remote_path=path.as_posix(), local_path=target, kind="tree")
                for path, target in trees
            ),
        )
        return SlurmJobHandle(
            job_id=job_id,
            invocation_id=invocation,
            config_identity=self._config_identity(),
            remote_workspace=remote_workspace.as_posix(),
            remote_status_path=remote_status.as_posix(),
            remote_log_path=remote_log.as_posix(),
            artifacts=artifacts,
            staging_seconds=staging_seconds,
            content_cache_hits=cache_hits,
        )

    def submit_batch(
        self, request: SlurmBatchRequest, *, operation_id: str | None = None
    ) -> SlurmBatchHandle:
        """Submit ordered stages as one Slurm job and one service lifecycle."""
        try:
            stages = _validate_batch_request(request)
            job_request = SlurmJobRequest(
                workspace=request.workspace,
                command=(
                    "bash",
                    "-c",
                    _batch_script(stages, stop_on_failure=request.stop_on_failure),
                ),
                setup_script=request.setup_script,
                service=request.service,
                support_trees=request.support_trees,
                cancel_event=request.cancel_event,
            )
        except (SlurmError, OSError, UnicodeError, ProcessTimeoutError) as error:
            raise SlurmSubmissionRejectedError(str(error)) from error
        started = self._clock.now()
        phase_root = PurePosixPath(_BATCH_RESULT_ROOT) / "phases"
        job = self._stage_and_submit(
            job_request,
            phase_timing_root=phase_root,
            operation_id=operation_id,
        )
        return SlurmBatchHandle(
            job=job,
            stages=batch_stage_handles(request),
            submission_seconds=max(0.0, self._clock.now() - started),
            stop_on_failure=request.stop_on_failure,
        )

    def completed_stages(self, handle: SlurmBatchHandle) -> int:
        """How many stages of a batch have written their result, in one remote command.

        Stages run in order, so with ``n`` results written, stage ``n`` is the one
        running. A stage that is running has not written its exit code yet.
        """
        root = PurePosixPath(handle.job.remote_workspace) / _BATCH_RESULT_ROOT
        listing = f"ls -1 {shlex.quote(root.as_posix())}/*/exit-code.txt 2>/dev/null | wc -l"
        output = self._whole_reading(listing)  # wc always counts; empty means lost, not zero
        try:
            return max(0, int(output))
        except ValueError as error:
            raise SlurmError.malformed_result() from error

    def remote_file(self, path: PurePosixPath) -> SlurmFileSource:
        """The cluster file at ``path``, read through this runner's transport as it grows."""
        return SlurmFileSource(path, self._whole_reading)

    def poll_batch(self, handle: SlurmBatchHandle) -> SlurmJobStatus:
        """Read the scheduler state of a batch handle."""
        return self.poll(handle.job)

    def wait_batch(
        self,
        handle: SlurmBatchHandle,
        *,
        pause: Callable[[float], None],
        timeout_seconds: float | None = None,
        cancel_event: Event | None = None,
    ) -> SlurmBatchWaitResult:
        """Wait on a batch without changing its one-job lifecycle semantics."""
        started = self._clock.now()
        result = self.wait(
            handle.job, pause=pause, timeout_seconds=timeout_seconds, cancel_event=cancel_event
        )
        elapsed = max(0.0, self._clock.now() - started)
        updated = handle.model_copy(update={"waited_seconds": handle.waited_seconds + elapsed})
        return SlurmBatchWaitResult(
            handle=updated,
            status=result.status,
            timed_out=result.timed_out,
        )

    def cancel_batch(self, handle: SlurmBatchHandle) -> None:
        """Cancel the single Slurm job that owns all batch stages."""
        self.cancel(handle.job)

    def collect_batch(
        self, handle: SlurmBatchHandle, *, observed: SlurmJobStatus | None = None
    ) -> SlurmBatchResult:
        """Collect ordered stage outcomes and only artifacts from passed stages.

        ``observed`` is the terminal status the caller just read; it saves a second
        scheduler query (two remote commands). A download that did not complete
        raises ``SlurmTransferInterruptedError`` (``_fetch``): no result is read
        from a partial copy.
        """
        self._validate_batch_handle(handle)
        collection_started = self._clock.now()
        job_result = self.collect_evidence(handle.job, observed=observed)
        stage_results: list[SlurmBatchStageResult] = []
        timings: dict[str, float] = {
            "staging": handle.job.staging_seconds,
            "submission": max(0.0, handle.submission_seconds - handle.job.staging_seconds),
            "scheduler_wait_observation": handle.waited_seconds,
        }
        collection_failure = job_result.collection_failure
        service_log_tail = ""
        with tempfile.TemporaryDirectory(
            prefix="vs-slurm-batch-", dir=self._scratch_root
        ) as temporary:
            temporary_path = Path(temporary)
            results_root = temporary_path / "results"
            results_root.mkdir()
            absent = self._fetch(
                PurePosixPath(handle.job.remote_workspace) / _BATCH_RESULT_ROOT,
                results_root,
                kind="tree",
            )
            collection_failure = collection_failure or absent
            for index, stage in enumerate(handle.stages):
                local_root = results_root / f"{index:04d}"
                if not local_root.exists() and job_result.exit_code != 0:
                    continue
                result = self._collect_stage(handle.job, stage, local_root)
                stage_results.append(result)
                if result.elapsed_seconds is not None:
                    timings[f"stage:{stage.name}"] = result.elapsed_seconds
            phase_root = results_root / "phases"
            for phase_name, result_name in (
                ("setup", "setup-seconds.txt"),
                ("service_startup", "service-startup-seconds.txt"),
            ):
                try:
                    timings[phase_name] = _read_nonnegative_seconds(phase_root / result_name)
                except SlurmError as exc:
                    if job_result.exit_code == 0:
                        collection_failure = collection_failure or str(exc)
            service_log_tail = _distinct_tail(phase_root / _SERVICE_LOG_TAIL)
        timings["collection"] = max(0.0, self._clock.now() - collection_started)
        return SlurmBatchResult(
            job_id=job_result.job_id,
            job_exit_code=job_result.exit_code,
            job_output=job_result.output,
            stages=tuple(stage_results),
            phase_timings_seconds=timings,
            content_cache_hits=handle.job.content_cache_hits,
            service_log_tail=service_log_tail,
            collection_failure=collection_failure,
        )

    def _collect_stage(
        self, job: SlurmJobHandle, stage: SlurmBatchStageHandle, root: Path
    ) -> SlurmBatchStageResult:
        """Keep each available stream even when other stage evidence is invalid."""
        failures: list[str] = []
        streams: dict[str, str] = {}
        for name in ("stdout", "stderr"):
            try:
                streams[name] = _read_text(root / f"{name}.txt")
            except SlurmError as exc:
                streams[name] = ""
                failures.append(f"{name}.txt: {exc}")
        exit_code = None
        skipped = False
        elapsed = None
        try:
            skipped = _read_text(root / "exit-code.txt").strip() == "SKIPPED"
            if not skipped:
                exit_code = _read_exit_code(root / "exit-code.txt")
        except SlurmError as exc:
            failures.append(f"exit-code.txt: {exc}")
        try:
            elapsed = _read_nonnegative_seconds(root / "elapsed-seconds.txt")
        except SlurmError as exc:
            failures.append(f"elapsed-seconds.txt: {exc}")
        artifacts: list[SlurmArtifactTarget] = []
        if exit_code == 0:
            for artifact in (*stage.file_artifacts, *stage.tree_artifacts):
                absent = self._collect_stage_artifact(job, artifact)
                if absent is None:
                    artifacts.append(artifact)
                else:
                    failures.append(f"{artifact.remote_path}: {absent}")
        return SlurmBatchStageResult(
            name=stage.name,
            exit_code=exit_code,
            stdout=streams["stdout"],
            stderr=streams["stderr"],
            elapsed_seconds=None if skipped else elapsed,
            skipped=skipped,
            artifacts=tuple(artifacts),
            collection_failure="; ".join(failures) or None,
        )

    def _collect_stage_artifact(
        self, job: SlurmJobHandle, artifact: SlurmArtifactTarget
    ) -> str | None:
        """Download one artifact (``_fetch``); why it is missing, None when it arrived whole.

        A local destination that cannot be made is the request's own problem, not a
        download cut short: it is recorded like a missing artifact.
        """
        local_path = artifact.local_path
        try:
            if artifact.kind == "file":
                local_path.parent.mkdir(parents=True, exist_ok=True)
            else:
                local_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return str(exc)
        return self._fetch(
            PurePosixPath(job.remote_workspace) / artifact.remote_path,
            local_path,
            kind=artifact.kind,
        )

    def _fetch(
        self, remote: PurePosixPath, local: Path, *, kind: Literal["file", "tree"]
    ) -> str | None:
        """Download ``remote`` whole; why it is absent when the cluster proved so, else None.

        A failed download says nothing about the remote files: the connection may
        have dropped before any file, mid-tree, or after the last file. So a failure
        is a missing result only when the cluster then answers, whole, that the path
        does not exist; otherwise it raises ``SlurmTransferInterruptedError``, and
        the caller reads nothing from the partial copy.
        """
        try:
            self._transport.get(remote, local, kind=kind)
        except (SlurmError, OSError, UnicodeError) as exc:
            if self._proven_absent(remote):
                return str(SlurmError.remote_path_absent(remote.as_posix()))
            raise SlurmTransferInterruptedError.of(remote.as_posix(), exc) from exc
        return None

    def _read_fetched[T](
        self, remote: PurePosixPath, local: Path, read: Callable[[Path], T], failures: list[str]
    ) -> T | None:
        """``read`` of the downloaded ``remote`` file; None, with why in ``failures``, if absent or malformed."""
        absent = self._fetch(remote, local, kind="file")
        if absent is not None:
            failures.append(absent)
            return None
        try:
            return read(local)
        except SlurmError as exc:
            failures.append(str(exc))
            return None

    def _proven_absent(self, remote: PurePosixPath) -> bool:
        """Whether the cluster answers, whole, that ``remote`` does not exist."""
        path = shlex.quote(remote.as_posix())
        probe = f"if test -e {path}; then printf '%s\\n' present; else printf '%s\\n' absent; fi"
        try:
            return self._whole_reading(probe) == "absent"
        except (SlurmError, OSError, UnicodeError, ProcessTimeoutError):
            return False

    def poll(self, handle: SlurmJobHandle) -> SlurmJobStatus:
        """Read the scheduler state for a submitted or recovered handle."""
        self._validate_handle(handle)
        return self.inspect_job(handle.job_id).status

    def wait(
        self,
        handle: SlurmJobHandle,
        *,
        pause: Callable[[float], None],
        timeout_seconds: float | None = None,
        cancel_event: Event | None = None,
    ) -> SlurmJobWaitResult:
        """Observe until terminal or bounded timeout.

        The timeout never cancels the job. An explicitly set ``cancel_event``
        requests cancellation and raises the cancellation error; this retains
        the legacy ``run()`` behavior.

        Between polls the wait pauses on ``cancel_event`` when there is one, so a
        cancel ends the pause at once, and with ``pause`` otherwise. Either runs on
        the runner clock's timeline: production passes a real sleep
        (``vs_sim.api.OsThreads().sleep``), a test one that advances its clock.
        """
        timeout = self._config.job_timeout_seconds if timeout_seconds is None else timeout_seconds
        if not math.isfinite(timeout) or timeout <= 0:
            raise SlurmError.invalid_wait_timeout()
        try:
            return self._observe(handle, timeout, cancel_event, pause)
        except SlurmError as failure:
            if cancel_event is None or not cancel_event.is_set():
                raise
            self._cancel_abandoned(handle, failure)
            raise

    def wait_until_started(
        self,
        handle: SlurmJobHandle,
        *,
        pause: Callable[[float], None],
        timeout_seconds: float,
    ) -> SlurmJobWaitResult:
        """Observe until the job runs or ends; ``timed_out``: still queued. Never cancels."""
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise SlurmError.invalid_wait_timeout()
        return self._observe(handle, timeout_seconds, None, pause, until_started=True)

    def _observe(
        self,
        handle: SlurmJobHandle,
        timeout: float,
        cancel_event: Event | None,
        pause: Callable[[float], None],
        *,
        until_started: bool = False,
    ) -> SlurmJobWaitResult:
        """Poll until terminal (or started) or *timeout*; a set *cancel_event* raises, uncancelled."""
        started = self._clock.now()
        deadline = started + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise SlurmError.cancelled(handle.job_id)
            status = self.poll(handle)
            done = status.terminal or (until_started and status != SlurmJobStatus.PENDING)
            remaining = deadline - self._clock.now()
            if done or remaining <= 0:
                waited = self._clock.now() - started
                return SlurmJobWaitResult(
                    handle=handle, status=status, timed_out=not done, waited_seconds=waited
                )
            seconds = min(self._config.poll_interval_seconds, remaining)
            # Bounded by *seconds*; the event's setter is the cancel path of the
            # caller that owns the wait, so no pause here is unbounded.
            if cancel_event is not None:
                cancel_event.wait(seconds)
            else:
                pause(seconds)

    def _await_terminal(
        self, handle: SlurmJobHandle, cancel_event: Event | None, pause: Callable[[float], None]
    ) -> SlurmJobWaitResult:
        """Observe within the configured job timeout; running past it raises."""
        outcome = self._observe(handle, self._config.job_timeout_seconds, cancel_event, pause)
        if outcome.timed_out:
            raise SlurmError.job_timed_out(handle.job_id)
        return outcome

    def _cancel_abandoned(self, handle: SlurmJobHandle, failure: BaseException) -> None:
        """Cancel a job its waiter gave up on; note a failed cancel on *failure*."""
        try:
            self.cancel(handle)
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-731105 [BLE001]; the original failure must still propagate.
            # > Any cancel failure (transport, timeout, OS) is reported on the
            # > failure being raised; a narrower catch would replace that failure
            # > with the cancel's and hide why the wait ended.
            failure.add_note(
                f"Slurm job {handle.job_id} may still be queued or running: "
                f"scancel failed: {type(error).__name__}: {error}"
            )

    def cancel(self, handle: SlurmJobHandle) -> None:
        """Request scheduler cancellation for an existing operation."""
        self._validate_handle(handle)
        self._transport.exec(f"scancel {handle.job_id}")

    def cancel_job(self, job_id: str) -> None:
        """Request cancellation by scheduler identity without a collection locator."""
        if re.fullmatch(r"[0-9]{1,32}", job_id) is None:
            raise SlurmError.invalid_job_id()
        self._transport.exec(f"scancel {job_id}")

    def collect(self, handle: SlurmJobHandle) -> SlurmJobResult:
        """Collect terminal output and declared artifacts for a handle."""
        self._validate_handle(handle)
        status = self.poll(handle)
        if status == SlurmJobStatus.CANCELLED:
            raise SlurmError.cancelled(handle.job_id)
        if status not in {SlurmJobStatus.COMPLETED, SlurmJobStatus.FAILED}:
            raise SlurmError.job_not_terminal(handle.job_id)
        with tempfile.TemporaryDirectory(
            prefix="vs-slurm-collect-", dir=self._scratch_root
        ) as temporary:
            temporary_path = Path(temporary)
            local_status = temporary_path / "exit-code.txt"
            local_log = temporary_path / "job.log"
            self._transport.get(PurePosixPath(handle.remote_status_path), local_status, kind="file")
            self._transport.get(PurePosixPath(handle.remote_log_path), local_log, kind="file")
            exit_code = _read_exit_code(local_status)
            succeeded = exit_code == 0 and status == SlurmJobStatus.COMPLETED
            for artifact in handle.artifacts:
                if not succeeded and not artifact.collect_on_failure:
                    continue
                local_path = artifact.local_path
                remote_path = PurePosixPath(handle.remote_workspace) / artifact.remote_path
                if artifact.kind == "file":
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                else:
                    local_path.mkdir(parents=True, exist_ok=True)
                if succeeded:
                    self._transport.get(remote_path, local_path, kind=artifact.kind)
                    continue
                # A failed job need not have written it; absent stays absent, and a
                # download cut short raises rather than leave a partial artifact.
                self._fetch(remote_path, local_path, kind=artifact.kind)
            return SlurmJobResult(
                job_id=handle.job_id,
                exit_code=exit_code,
                output=_read_text(local_log),
            )

    def claim_operation(self, operation_id: str, intent: str) -> RemoteOperationClaim:
        """Reserve stable identity across every local ledger on this cluster."""
        return self._operations.claim(_safe_component(operation_id), intent)

    def inspect_operation(self, operation_id: str) -> RemoteOperationEvidence:
        """Observe durable scheduler-wide identity and cancellation evidence."""
        return self._operations.inspect(_safe_component(operation_id))

    def record_operation_acceptance(self, operation_id: str, record: str) -> None:
        """Retain the acknowledged locator independently of local state roots."""
        self._operations.accepted(_safe_component(operation_id), record)

    def record_operation_rejection(self, operation_id: str, reason: str) -> None:
        """Retain a rejection known to precede scheduler acceptance."""
        self._operations.rejected(_safe_component(operation_id), reason)

    def cancel_operation(self, operation_id: str) -> None:
        """Persist scheduler-wide cancellation intent before requesting scancel."""
        self._operations.cancel(_safe_component(operation_id))

    def recover_handle(
        self, request: SlurmJobRequest | SlurmBatchRequest, *, operation_id: str, job_id: str
    ) -> SlurmJobHandle | SlurmBatchHandle:
        """Rebuild a locator from persisted intent without submitting work."""
        invocation = _safe_component(operation_id)
        base = PurePosixPath(self._config.remote_workspace_root) / self._config.name / invocation
        if isinstance(request, SlurmBatchRequest):
            _validate_batch_request(request)
            job = self.recover_handle(
                SlurmJobRequest(
                    workspace=request.workspace,
                    command=("true",),
                    setup_script=request.setup_script,
                    service=request.service,
                    support_trees=request.support_trees,
                ),
                operation_id=operation_id,
                job_id=job_id,
            )
            if not isinstance(job, SlurmJobHandle):
                raise SlurmError.invalid_handle()
            return SlurmBatchHandle(
                job=job,
                submission_seconds=0.0,
                stop_on_failure=request.stop_on_failure,
                stages=batch_stage_handles(request),
            )
        _validate_request(request)
        return SlurmJobHandle(
            job_id=job_id,
            invocation_id=invocation,
            config_identity=self._config_identity(),
            remote_workspace=(base / "workspace").as_posix(),
            remote_status_path=(base / "exit-code.txt").as_posix(),
            remote_log_path=(base / "job.log").as_posix(),
            artifacts=tuple(
                SlurmArtifactTarget(
                    remote_path=a.remote_path,
                    local_path=a.local_path,
                    kind="file",
                    collect_on_failure=a.collect_on_failure,
                )
                for a in request.file_artifacts
            )
            + tuple(
                SlurmArtifactTarget(remote_path=a.remote_path, local_path=a.local_path, kind="tree")
                for a in request.tree_artifacts
            ),
        )

    def find_operation(self, operation_id: str) -> str | None:
        """Inspect active jobs then accounting by stable scheduler name."""
        name = shlex.quote(operation_job_name(self._config, _safe_component(operation_id)))
        active = self._read_scheduler(f"squeue -h -n {name} -o %i")
        historical = self._read_scheduler(
            f"sacct -n -X --name {name} --starttime=1970-01-01 --format=JobIDRaw"
        )
        ids = {
            line.strip()
            for line in (active + "\n" + historical).splitlines()
            if re.fullmatch(r"[0-9]{1,32}", line.strip())
        }
        if len(ids) > 1:
            raise SlurmError.operation_identity_ambiguous()
        return next(iter(ids), None)

    def cleanup_operation(self, operation_id: str, *, content_identity: str) -> None:
        """Remove one settled operation and its exact content-cache staging material.

        Cleanup is deliberately narrower than a general cache eviction API.  It
        accepts the caller's validated stable operation and content identities,
        proves that the scheduler job is absent or terminal, removes only those
        names, and then confirms they are absent.  Startup lifecycle probes use
        this after every known outcome so their one-shot staging does not become
        long-lived run state.
        """
        invocation = _safe_component(operation_id)
        if re.fullmatch(r"[0-9a-f]{64}", content_identity) is None:
            raise SlurmError.content_stage_failed()
        job_id = self.find_operation(invocation)
        if job_id is not None:
            reading = self.inspect_job(job_id)
            if reading.status == SlurmJobStatus.UNKNOWN or not reading.status.terminal:
                raise SlurmError.job_not_terminal(job_id)
        cluster_root = PurePosixPath(self._config.remote_workspace_root) / self._config.name
        operation_root = cluster_root / invocation
        cache_root = cluster_root / _CONTENT_CACHE_ROOT
        cache_object = cache_root / content_identity
        temporary = cache_root / f"{content_identity}.tmp.{invocation}.workspace"
        lock = cache_root / f"{content_identity}.lock"
        stale_pattern = f"{content_identity}.lock.stale.*"
        paths = (operation_root, cache_object, temporary, lock)
        quoted = " ".join(shlex.quote(path.as_posix()) for path in paths)
        cache = shlex.quote(cache_root.as_posix())
        pattern = shlex.quote(stale_pattern)
        absent = " && ".join(f"[ ! -e {shlex.quote(path.as_posix())} ]" for path in paths)
        response = self._transport.exec(
            f"rm -rf -- {quoted} && "
            f"if [ -d {cache} ]; then "
            f"find {cache} -mindepth 1 -maxdepth 1 -name {pattern} -exec rm -rf -- {{}} +; "
            "fi && "
            f"if {absent} && "
            f'[ -z "$(find {cache} -mindepth 1 -maxdepth 1 -name {pattern} '
            "-print -quit 2>/dev/null)\" ]; then printf 'CLEAN'; else printf 'DIRTY'; fi"
        )
        if response.stdout.strip() != "CLEAN":
            raise SlurmError.operation_cleanup_unconfirmed()

    def inspect_job(self, job_id: str) -> SchedulerReading:
        """Read scheduler state with queue reason and estimated start evidence."""
        if re.fullmatch(r"[0-9]{1,32}", job_id) is None:
            raise SlurmError.invalid_job_id()
        # Accounting, not the queue, is authoritative for an ended job: slurmctld
        # purges it after MinJobAge (300 s by default) and ``squeue -j`` then fails
        # with "Invalid job id specified". A failed queue read is therefore not a
        # verdict; sacct decides, and only both failing (or sacct knowing nothing
        # of a job the queue could not be read for) leaves the state unknown. The
        # translator decides what each reading means and whether sacct must be asked.
        queue_failure: SlurmError | None = None
        try:
            queue = parse_queue_row(self._queue_state(job_id))
        except SlurmError as exc:
            queue_failure, queue = exc, NotInQueue()
        life = observe(INITIAL_LIFE, queue)
        if not needs_accounting(life):
            return _queue_reading(life, queue)
        try:
            accounting = self._read_scheduler(f"sacct -n -P -X -j {job_id} --format=State,ExitCode")
        except SlurmError:
            # Accounting only refines what the queue said (a COMPLETING job may have
            # ended). Without it the queue reading stands; with no queue reading
            # either, the job's state is unknown and the failure is the answer.
            if isinstance(life, Unknown) and life.raw is None:
                raise
            return _queue_reading(life, queue)
        row = parse_accounting_row(accounting)
        refined = observe(life, row)
        # Slurm keeps a finished or cancelled job in COMPLETING for 20 to 40 s
        # while the node tears down, but accounting already records its final
        # state, and the job script has exited. Joining the accounting row ends the
        # life; waiting for the queue to forget it would only delay the result.
        if refined != INITIAL_LIFE or queue_failure is None:
            return _queue_reading(refined, queue)
        raise queue_failure

    def read_queue(self) -> QueueSnapshot:
        """One read-only query (see ``queue_view``); raises ``SlurmError`` or ``QueueViewError``."""
        return parse_queue_snapshot(self._read_scheduler(queue_snapshot_command()))

    def _queue_state(self, job_id: str) -> str:
        """The queue's line for the job."""
        return self._read_scheduler(f"squeue -h -j {job_id} -o '%T|%r|%S'")

    def _read_scheduler(self, command: str) -> str:
        """Stdout of a read-only scheduler query; one lost read is retried, a second raises.

        ``squeue`` and ``sacct`` change nothing, so repeating one is safe. Every such
        read goes through here, so no scheduler source is the single point where one
        dropped connection turns a known job into an unknown one.
        """
        try:
            return self._whole_reading(command)
        except SlurmError:
            return self._whole_reading(command)

    def _whole_reading(self, command: str) -> str:
        """Stdout of ``command``, refused unless its end line arrived (`scheduler_read`).

        An empty answer means "no such job" only when it provably arrived
        whole; a lost or cut answer is unknown, never an absent job.
        """
        body = unframed(self._transport.exec(framed(command)).stdout)
        if body is None:
            raise SlurmError.truncated_output()
        return body.strip()

    def collect_evidence(
        self, handle: SlurmJobHandle, *, observed: SlurmJobStatus | None = None
    ) -> SlurmJobResult:
        """Preserve available evidence even when terminal status artifacts are absent.

        ``observed`` is a terminal status the caller just read; without it the scheduler
        is queried. A download that did not complete raises
        ``SlurmTransferInterruptedError`` (``_fetch``).
        """
        self._validate_handle(handle)
        status = observed if observed is not None else self.poll(handle)
        if not status.terminal:
            raise SlurmError.job_not_terminal(handle.job_id)
        failures: list[str] = []
        with tempfile.TemporaryDirectory(
            prefix="vs-slurm-evidence-", dir=self._scratch_root
        ) as temporary:
            root = Path(temporary)
            exit_code = self._read_fetched(
                PurePosixPath(handle.remote_status_path), root / "status", _read_exit_code, failures
            )
            output = (
                self._read_fetched(
                    PurePosixPath(handle.remote_log_path), root / "log", _read_text, failures
                )
                or ""
            )
            for artifact in handle.artifacts:
                if (
                    exit_code == 0 and status == SlurmJobStatus.COMPLETED
                ) or artifact.collect_on_failure:
                    absent = self._collect_stage_artifact(handle, artifact)
                    if absent is not None:
                        failures.append(absent)
        return SlurmJobResult(
            job_id=handle.job_id,
            exit_code=exit_code,
            output=output,
            collection_failure="; ".join(failures) or None,
        )

    def validate_handle(self, handle: SlurmJobHandle | SlurmBatchHandle) -> None:
        """Validate cluster configuration and operation paths without remote I/O."""
        if isinstance(handle, SlurmBatchHandle):
            self._validate_batch_handle(handle)
        else:
            self._validate_handle(handle)

    def _validate_handle(self, handle: SlurmJobHandle) -> None:
        if handle.config_identity != self._config_identity():
            raise SlurmError.handle_config_mismatch()
        base = PurePosixPath(self._config.remote_workspace_root) / self._config.name
        operation_root = base / handle.invocation_id
        if (
            handle.remote_workspace != (operation_root / "workspace").as_posix()
            or handle.remote_status_path != (operation_root / "exit-code.txt").as_posix()
            or handle.remote_log_path != (operation_root / "job.log").as_posix()
        ):
            raise SlurmError.invalid_handle()

    def _validate_batch_handle(self, handle: SlurmBatchHandle) -> None:
        self._validate_handle(handle.job)
        names: set[str] = set()
        if not handle.stages:
            raise SlurmError.invalid_batch_request()
        for stage in handle.stages:
            if _STAGE_NAME.fullmatch(stage.name) is None or stage.name in names:
                raise SlurmError.invalid_batch_request()
            names.add(stage.name)
            for artifact in (*stage.file_artifacts, *stage.tree_artifacts):
                path = _safe_relative_path(artifact.remote_path, SlurmError.invalid_artifact_path)
                if path.parts[0] == _BATCH_RESULT_ROOT:
                    raise SlurmError.invalid_artifact_path()

    def _config_identity(self) -> str:
        transport = self._config.transport.model_dump(mode="json")
        identity = {
            "name": self._config.name,
            "remote_workspace_root": self._config.remote_workspace_root,
            "transport": transport,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _submit(
        self,
        base: PurePosixPath,
        script: PurePosixPath,
        output: PurePosixPath,
        resources: SlurmJobResources,
    ) -> str:
        minimal = resources is SlurmJobResources.MINIMAL
        argv = (
            *self._config.sbatch_command,
            *(minimal_job(self._config).arguments if minimal else self._config.sbatch_arguments),
            f"--job-name={operation_job_name(self._config, base.name)}",
            f"--output={output.as_posix()}",
            f"--error={output.as_posix()}",
            # VibeSys retries a job itself; a scheduler requeue (after a node failure)
            # would rerun finished stages behind its back.
            "--no-requeue",
            script.as_posix(),
        )
        try:
            result = self._transport.exec(
                f"cd {shlex.quote(base.as_posix())} && {shlex.join(argv)}"
            )
        except SlurmTransportFailedError as error:
            raise SlurmSbatchFailedError.from_transport(error) from error
        match = _JOB_ID.search(result.stdout)
        if match is None:
            raise SlurmError.missing_job_id()
        return match.group(1)


class _Transport(Protocol):
    def exec(self, command: str) -> _TransportResponse: ...

    def sync_to(
        self,
        local: Path,
        remote: PurePosixPath,
        *,
        delete: bool,
        excludes: Sequence[str],
    ) -> None: ...

    def put(self, local: Path, remote: PurePosixPath) -> None: ...

    def get(self, remote: PurePosixPath, local: Path, *, kind: Literal["file", "tree"]) -> None: ...


class _ProcessTransport:
    # Describes a program that exited nonzero; a transport may refine it.
    _failed = staticmethod(SlurmError.transport_failed)

    def __init__(self, processes: Processes, timeout: float) -> None:
        self._processes = processes
        self._timeout = timeout

    def _launch(self, argv: Sequence[str]) -> Sequence[str]:
        """Return the argv to start for *argv*; a transport may wrap the program."""
        return argv

    def _run(
        self,
        argv: Sequence[str],
        *,
        operation: str,
        stdin: str | None = None,
        max_output_bytes: int | None = None,
    ) -> _TransportResponse:
        """Run one transport program; its output is UTF-8 (``UnicodeDecodeError`` otherwise)."""
        spec = ProcessSpec(tuple(self._launch(argv)), input=(stdin or "").encode())
        if max_output_bytes is not None:
            spec = replace(spec, max_output_bytes=max_output_bytes)
        try:
            run = self._processes.run(spec, timeout=self._timeout, output=DISCARD)
        except FileNotFoundError as exc:
            raise SlurmError.transport_unavailable(operation) from exc
        match run:
            case ProcessOutcome(returncode=0, stdout=stdout, stderr=stderr):
                return _TransportResponse(stdout=stdout.decode(), stderr=stderr.decode())
            case ProcessOutcome(returncode=returncode, stderr=stderr):
                raise self._failed(operation, returncode, stderr.decode(errors="replace"))
            case ProcessTimedOut():
                raise SlurmError.transport_timed_out(operation)
            case ProcessGroupUnreaped(detail=detail):
                error = SlurmError.transport_timed_out(operation)
                error.add_note(detail)
                raise error
            case ProcessOutputTooLarge(limit=limit):
                message = f"Slurm transport wrote more than {limit} bytes during {operation}"
                raise SlurmError(message)


class _RsyncTransport(_ProcessTransport):
    """Transfers as rsync copies; *destination* prefixes the remote side's path."""

    def __init__(
        self,
        rsync_command: tuple[str, ...],
        *,
        remote_shell: tuple[str, ...],
        destination: str,
        processes: Processes,
        timeout: float,
    ) -> None:
        super().__init__(processes, timeout)
        self._rsync_command = rsync_command
        self._remote_shell = remote_shell
        self._destination = destination

    def sync_to(
        self,
        local: Path,
        remote: PurePosixPath,
        *,
        delete: bool,
        excludes: Sequence[str],
    ) -> None:
        options = ["-a"]
        if delete:
            options.append("--delete")
        options.extend(f"--exclude={item}" for item in excludes)
        self._rsync(
            options, f"{local}/", self._remote(remote, trailing_slash=True), operation="sync_to"
        )

    def put(self, local: Path, remote: PurePosixPath) -> None:
        self._rsync(["-a"], str(local), self._remote(remote), operation="put")

    def get(self, remote: PurePosixPath, local: Path, *, kind: Literal["file", "tree"]) -> None:
        tree = kind == "tree"
        self._rsync(
            ["-a"],
            self._remote(remote, trailing_slash=tree),
            f"{local}/" if tree else str(local),
            operation="get",
        )

    def _rsync(self, options: list[str], source: str, target: str, *, operation: str) -> None:
        self._run(
            (*self._rsync_command, *options, *self._remote_shell, "--", source, target),
            operation=operation,
        )

    def _remote(self, path: PurePosixPath, *, trailing_slash: bool = False) -> str:
        suffix = "/" if trailing_slash else ""
        return f"{self._destination}{path.as_posix()}{suffix}"


class _SshTransport(_RsyncTransport):
    _failed = staticmethod(SlurmError.ssh_transport_failed)

    def __init__(
        self,
        config: SlurmSshTransport,
        *,
        processes: Processes,
        timeout: float,
    ) -> None:
        super().__init__(
            config.rsync_command,
            remote_shell=("-e", shlex.join(config.ssh_argv)),
            destination=f"{config.host}:",
            processes=processes,
            timeout=timeout,
        )
        self._config = config

    def exec(self, command: str) -> _TransportResponse:
        return self._run(
            (*self._config.ssh_argv, "--", self._config.host, command),
            operation="exec",
        )


_LOGIN_ENVIRONMENT_NAMES = frozenset(
    {"HOME", "USER", "LOGNAME", "SHELL", "PATH", "LANG", "TZ", "TMPDIR"}
)
_LOGIN_ENVIRONMENT_PREFIX = "LC_"


def login_environment(host: Mapping[str, str]) -> dict[str, str]:
    """Return the part of *host* that a login session would also have.

    The local transport runs its programs in this environment and nothing else,
    which is what the SSH transport gets from sshd: a fresh one. A host process
    can hold credentials, and inside a Slurm allocation or batch job it holds
    ``SLURM_*`` variables that would redirect the gate's own ``sbatch`` to the
    wrong job. The names kept are the identity and locale ones a login sets
    (``HOME USER LOGNAME SHELL PATH LANG LC_* TZ``) and ``TMPDIR``; every other
    name, ``SLURM_*``, ``SBATCH_*``, ``SALLOC_*`` and ``SRUN_*`` included, is dropped.
    """
    return {
        name: value
        for name, value in host.items()
        if name in _LOGIN_ENVIRONMENT_NAMES
        or (
            name.startswith(_LOGIN_ENVIRONMENT_PREFIX)
            and len(name) > len(_LOGIN_ENVIRONMENT_PREFIX)
        )
    }


class _LocalTransport(_RsyncTransport):
    def __init__(
        self,
        config: SlurmLocalTransport,
        *,
        processes: Processes,
        timeout: float,
        host_environment: Callable[[], Mapping[str, str]],
    ) -> None:
        super().__init__(
            config.rsync_command,
            remote_shell=(),
            destination="",
            processes=processes,
            timeout=timeout,
        )
        self._config = config
        self._host_environment = host_environment

    def _launch(self, argv: Sequence[str]) -> Sequence[str]:
        # ``env -i`` gives the program the login environment and nothing of this
        # process's.
        environment = login_environment(self._host_environment())
        clean = ("env", "-i", *(f"{name}={value}" for name, value in sorted(environment.items())))
        return (*clean, *argv)

    def exec(self, command: str) -> _TransportResponse:
        return self._run((*self._config.shell_command, command), operation="exec")


class _ConnectorTransport(_ProcessTransport):
    def __init__(
        self,
        config: SlurmConnectorTransport,
        *,
        processes: Processes,
        timeout: float,
    ) -> None:
        super().__init__(processes, timeout)
        self._config = config

    def exec(self, command: str) -> _TransportResponse:
        request = connector_protocol.SlurmConnectorExecRequest(
            version=1, operation="exec", command=command
        )
        return self._request(request, "exec")

    def sync_to(
        self,
        local: Path,
        remote: PurePosixPath,
        *,
        delete: bool,
        excludes: Sequence[str],
    ) -> None:
        request = connector_protocol.SlurmConnectorSyncToRequest(
            version=1,
            operation="sync_to",
            local_dir=str(local.resolve()),
            remote_dir=remote.as_posix(),
            delete=delete,
            excludes=tuple(excludes),
        )
        self._request(request, "sync_to")

    def put(self, local: Path, remote: PurePosixPath) -> None:
        request = connector_protocol.SlurmConnectorPutRequest(
            version=1,
            operation="put",
            local_path=str(local.resolve()),
            remote_path=remote.as_posix(),
        )
        self._request(request, "put")

    def get(self, remote: PurePosixPath, local: Path, *, kind: Literal["file", "tree"]) -> None:
        request = connector_protocol.SlurmConnectorGetRequest(
            version=1,
            operation="get",
            remote_path=remote.as_posix(),
            local_path=str(local.resolve()),
            kind=kind,
        )
        self._request(request, "get")

    def _request(
        self, request: connector_protocol.SlurmConnectorRequest, operation: str
    ) -> _TransportResponse:
        result = self._run(
            self._config.command,
            operation=operation,
            stdin=request.model_dump_json(),
            max_output_bytes=connector_protocol.MAX_CONNECTOR_RESPONSE_BYTES,
        )
        response = decode_json(
            result.stdout,
            connector_protocol.SlurmConnectorResponse,
            source=f"connector {operation} response",
            max_bytes=connector_protocol.MAX_CONNECTOR_RESPONSE_BYTES,
        )
        if isinstance(response, Unreadable):
            raise SlurmError.invalid_connector_response(operation)
        if response.returncode != 0:
            raise SlurmError.transport_failed(operation, response.returncode, response.stderr)
        return _TransportResponse(stdout=response.stdout, stderr=response.stderr)


def _make_transport(
    config: SlurmConfig,
    *,
    processes: Processes,
    host_environment: Callable[[], Mapping[str, str]],
) -> _Transport:
    timeout = config.transport_timeout_seconds
    match config.transport:
        case SlurmSshTransport():
            return _SshTransport(config.transport, processes=processes, timeout=timeout)
        case SlurmLocalTransport():
            return _LocalTransport(
                config.transport,
                processes=processes,
                timeout=timeout,
                host_environment=host_environment,
            )
        case SlurmConnectorTransport():
            return _ConnectorTransport(config.transport, processes=processes, timeout=timeout)


def _validate_request(
    request: SlurmJobRequest,
) -> tuple[
    Path,
    list[tuple[PurePosixPath, Path]],
    list[tuple[PurePosixPath, Path]],
    list[tuple[PurePosixPath, Path]],
]:
    if (
        isinstance(request.command, str)
        or not request.command
        or any(not isinstance(part, str) or not part for part in request.command)
    ):
        raise SlurmError.invalid_command()
    try:
        source = request.workspace.resolve(strict=True)
    except OSError as exc:
        raise SlurmError.workspace_is_not_directory() from exc
    if not source.is_dir():
        raise SlurmError.workspace_is_not_directory()
    if request.setup_script is not None:
        setup = PurePosixPath(request.setup_script)
        if not setup.is_absolute() or setup.as_posix() != request.setup_script:
            raise SlurmError.invalid_setup_script()

    support = _validate_support(request.support_trees)

    if any(
        not isinstance(item.local_path, Path)
        for item in (*request.file_artifacts, *request.tree_artifacts)
    ):
        raise SlurmError.invalid_artifact_path()
    files = [
        (_safe_relative_path(item.remote_path, SlurmError.invalid_artifact_path), item.local_path)
        for item in request.file_artifacts
    ]
    trees = [
        (_safe_relative_path(item.remote_path, SlurmError.invalid_artifact_path), item.local_path)
        for item in request.tree_artifacts
    ]
    return source, support, files, trees


def batch_stage_handles(request: SlurmBatchRequest) -> tuple[SlurmBatchStageHandle, ...]:
    """Each stage's artifact targets as the request declares them: remote paths to local ones."""
    return tuple(
        SlurmBatchStageHandle(
            name=stage.name,
            file_artifacts=tuple(
                SlurmArtifactTarget(
                    remote_path=a.remote_path,
                    local_path=a.local_path,
                    kind="file",
                    collect_on_failure=a.collect_on_failure,
                )
                for a in stage.file_artifacts
            ),
            tree_artifacts=tuple(
                SlurmArtifactTarget(remote_path=a.remote_path, local_path=a.local_path, kind="tree")
                for a in stage.tree_artifacts
            ),
        )
        for stage in _validate_batch_request(request)
    )


def _validate_support(support_trees: Mapping[str, Path] | None) -> list[tuple[PurePosixPath, Path]]:
    if support_trees is not None and not isinstance(support_trees, Mapping):
        raise SlurmError.invalid_support_path()
    support: list[tuple[PurePosixPath, Path]] = []
    support_items = (support_trees or {}).items()
    if any(
        not isinstance(relative, str) or not isinstance(local, Path)
        for relative, local in support_items
    ):
        raise SlurmError.invalid_support_path()
    for relative, local_path in sorted(support_items):
        remote = _safe_relative_path(relative, SlurmError.invalid_support_path)
        try:
            local = local_path.resolve(strict=True)
        except OSError as exc:
            raise SlurmError.invalid_support_path() from exc
        if not local.is_dir():
            raise SlurmError.invalid_support_path()
        support.append((remote, local))

    return support


def _validate_batch_request(request: SlurmBatchRequest) -> tuple[SlurmBatchStage, ...]:
    if not request.stages:
        raise SlurmError.invalid_batch_request()
    names: set[str] = set()
    for stage in request.stages:
        if _STAGE_NAME.fullmatch(stage.name) is None or stage.name in names:
            raise SlurmError.invalid_batch_request()
        names.add(stage.name)
        if (
            isinstance(stage.command, str)
            or not stage.command
            or any(not isinstance(part, str) or not part for part in stage.command)
        ):
            raise SlurmError.invalid_command()
        if stage.timeout_seconds is not None and (
            not isinstance(stage.timeout_seconds, int)
            or isinstance(stage.timeout_seconds, bool)
            or stage.timeout_seconds <= 0
        ):
            raise SlurmError.invalid_stage_timeout()
        for artifact in (*stage.file_artifacts, *stage.tree_artifacts):
            if not isinstance(artifact.local_path, Path):
                raise SlurmError.invalid_artifact_path()
            path = _safe_relative_path(artifact.remote_path, SlurmError.invalid_artifact_path)
            if path.parts[0] == _BATCH_RESULT_ROOT:
                raise SlurmError.invalid_artifact_path()
    _validate_request(
        SlurmJobRequest(
            workspace=request.workspace,
            command=("bash", "-c", "true"),
            setup_script=request.setup_script,
            service=request.service,
            support_trees=request.support_trees,
            cancel_event=request.cancel_event,
        )
    )
    return request.stages


# ``timeout`` is GNU coreutils; stock macOS lacks it (Homebrew installs it as
# ``gtimeout``) and the Fake cluster runs this script with the local shell.
# Fail loudly rather than run a stage without its time limit.
_TIMEOUT_FUNCTION = (
    "vs_timeout() { "
    'if command -v timeout >/dev/null 2>&1; then timeout "$@"; '
    'elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$@"; '
    "else echo 'vs-slurm: timeout(1) is not installed' >&2; return 127; fi; }"
)


def _batch_script(stages: Sequence[SlurmBatchStage], *, stop_on_failure: bool) -> str:
    lines = ["set +e", f"mkdir -p {shlex.quote(_BATCH_RESULT_ROOT)}", "batch_failed=0"]
    if any(stage.timeout_seconds is not None for stage in stages):
        lines.append(_TIMEOUT_FUNCTION)
    for index, stage in enumerate(stages):
        result_root = PurePosixPath(_BATCH_RESULT_ROOT) / f"{index:04d}"
        stdout_path = result_root / "stdout.txt"
        stderr_path = result_root / "stderr.txt"
        exit_path = result_root / "exit-code.txt"
        elapsed_path = result_root / "elapsed-seconds.txt"
        command = shell_join_with_port(stage.command)
        if stage.timeout_seconds is not None:
            command = f"vs_timeout --signal=TERM --kill-after=5s {stage.timeout_seconds}s {command}"
        lines.extend(
            [
                f"mkdir -p {shlex.quote(result_root.as_posix())}",
                "stage_started=$SECONDS",
                'if [ "$batch_failed" -eq 0 ] || [ '
                + ("0" if stop_on_failure else "1")
                + " -eq 1 ]; then",
                f"  {command} > {shlex.quote(stdout_path.as_posix())} 2> {shlex.quote(stderr_path.as_posix())}",
                "  stage_exit=$?",
                "  stage_elapsed=$((SECONDS - stage_started))",
                f"  printf '%s\\n' \"$stage_exit\" > {shlex.quote(exit_path.as_posix())}",
                f"  printf '%s\\n' \"$stage_elapsed\" > {shlex.quote(elapsed_path.as_posix())}",
                '  if [ "$stage_exit" -ne 0 ]; then batch_failed=1; fi',
                "else",
                "  printf '%s\\n' SKIPPED > " + shlex.quote(exit_path.as_posix()),
                "  printf '%s\\n' 0 > " + shlex.quote(elapsed_path.as_posix()),
                f"  : > {shlex.quote(stdout_path.as_posix())}",
                f"  : > {shlex.quote(stderr_path.as_posix())}",
                "fi",
            ]
        )
    # Stage failure is part of the returned evaluation result, not a scheduler
    # failure. Returning zero lets the collector retrieve the preceding stage's
    # status and output while preserving strict stop-on-first-failure ordering.
    lines.extend(["exit 0", ""])
    return "\n".join(lines)


def _safe_relative_path(value: str, error: Callable[[], SlurmError]) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise error()
    return path


def _safe_component(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
        raise SlurmConfigError.invalid_invocation_id()
    return value


def _normalized_remote_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or path.as_posix() != value
        or any(part in {".", ".."} for part in path.parts[1:])
    ):
        raise ValueError
    return path


def _job_script(request: _JobScriptRequest) -> str:
    workspace = request.workspace
    status_path = request.status_path
    command = request.command
    setup_script = request.setup_script
    service = request.service
    phase_timing_root = request.phase_timing_root
    lines = ["#!/usr/bin/env bash", "set -uo pipefail", f"cd {shlex.quote(workspace.as_posix())}"]
    # A sshd-built bash that sees these re-reads the login rc over the setup's exports (README).
    lines.append("unset SSH_CLIENT SSH2_CLIENT")
    if phase_timing_root is not None:
        lines.append(f"mkdir -p {shlex.quote((workspace / phase_timing_root).as_posix())}")
    if setup_script:
        if phase_timing_root is not None:
            lines.append("setup_started=$SECONDS")
        lines.extend(
            [
                "set +e",
                'export BUNDLE="$PWD"',
                f"source {shlex.quote(setup_script)}",
                "setup_status=$?",
                f'if [ "$setup_status" -ne 0 ]; then printf \'%s\\n\' "$setup_status" > {shlex.quote(status_path.as_posix())}; exit "$setup_status"; fi',
            ]
        )
        if phase_timing_root is not None:
            timing_path = workspace / phase_timing_root / "setup-seconds.txt"
            lines.append(
                f"printf '%s\\n' $((SECONDS - setup_started)) > {shlex.quote(timing_path.as_posix())}"
            )
    elif phase_timing_root is not None:
        timing_path = workspace / phase_timing_root / "setup-seconds.txt"
        lines.append(f"printf '%s\\n' 0 > {shlex.quote(timing_path.as_posix())}")
    lines.append("set +e")
    lines.extend(
        teardown_shell(
            stop_grace_seconds=request.service_stop_grace_seconds,
            exit_wait_seconds=request.process_exit_wait_seconds,
            status_path=status_path,
            kept_exit_code=SERVICE_NOT_READY_EXIT_CODE,
        )
    )
    if service is not None:
        lines.extend(
            _service_script(service, phase_timing_root=phase_timing_root, workspace=workspace)
        )
    elif phase_timing_root is not None:
        timing_path = workspace / phase_timing_root / "service-startup-seconds.txt"
        lines.append(f"printf '%s\\n' 0 > {shlex.quote(timing_path.as_posix())}")
    # The command gets a session of its own too, so a process it leaves behind (a server a
    # benchmark started and never stopped) is ended with the service, not left to Slurm.
    lines.extend(
        [
            *start_in_own_session(shell_join_with_port(command), pid_variable="command_pid"),
            'wait "$command_pid"',
            "job_status=$?",
        ]
    )
    if service is not None and phase_timing_root is not None:
        tail_path = workspace / phase_timing_root / _SERVICE_LOG_TAIL
        lines.append(
            f"tail -n {_SERVICE_LOG_TAIL_LINES} .vs-slurm-service.log"
            f" > {shlex.quote(tail_path.as_posix())} 2>/dev/null"
        )
    if service is not None:
        lines.append("fi")
    lines.extend(
        [
            f"printf '%s\\n' \"$job_status\" > {shlex.quote(status_path.as_posix())}",
            'exit "$job_status"',
            "",
        ]
    )
    return "\n".join(lines)


def _service_script(
    service: SlurmService,
    *,
    phase_timing_root: PurePosixPath | None = None,
    workspace: PurePosixPath | None = None,
) -> list[str]:
    command = shell_join_with_port(service.command)
    readiness_url = shlex.quote(service.readiness_url)
    lines = [
        "export PORT=$((20000 + SLURM_JOB_ID % 10000))",
        # The service's session is every process it started (a server's workers too), and
        # the teardown in ``job_teardown`` ends all of them before the script exits.
        *start_in_own_session(
            command, pid_variable="service_pid", redirect="> .vs-slurm-service.log 2>&1"
        ),
        "ready=0",
        "service_started=$SECONDS",
        f"for attempt in $(seq 1 {service.startup_timeout_seconds}); do",
        '  if ! kill -0 "$service_pid" 2>/dev/null; then break; fi',
        f"  readiness_url={readiness_url}",
        f"  readiness_url=${{readiness_url//{PORT_PLACEHOLDER}/$PORT}}",
        '  if curl -fs --max-time 2 "$readiness_url" >/dev/null; then ready=1; break; fi',
        "  sleep 1",
        "done",
    ]
    if phase_timing_root is not None and workspace is not None:
        timing_path = workspace / phase_timing_root / "service-startup-seconds.txt"
        lines.append(
            f"printf '%s\\n' $((SECONDS - service_started)) > {shlex.quote(timing_path.as_posix())}"
        )
    lines.append(
        'if [ "$ready" -ne 1 ]; then tail -40 .vs-slurm-service.log;'
        f" job_status={SERVICE_NOT_READY_EXIT_CODE}; else"
    )
    return lines


def _distinct_tail(path: Path) -> str:
    """Return the service log's last lines, keeping only the latest copy of a repeated line.

    Servers often repeat one warning hundreds of times; without this the tail
    would show only that warning and hide the server's own progress lines.
    """
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    seen: set[str] = set()
    kept: list[str] = []
    size = 0
    for raw_line in reversed(lines):
        line = (
            raw_line
            if len(raw_line) <= _SERVICE_LOG_LINE_CHARS
            else raw_line[:_SERVICE_LOG_LINE_CHARS] + " [line truncated]"
        )
        if line in seen:
            continue
        size += len(line) + 1
        if size > _SERVICE_LOG_TAIL_CHARS:
            break
        seen.add(line)
        kept.append(line)
        if len(kept) == _SERVICE_LOG_REPORTED_LINES:
            break
    return "\n".join(reversed(kept))


def _queue_reading(life: JobLife, queue: QueueRow | NotInQueue) -> SchedulerReading:
    """A reading of *life*, carrying the queue's reason and start estimate when it listed the job."""
    if isinstance(queue, NotInQueue):
        return SchedulerReading(life)
    return SchedulerReading(life, reason=queue.reason, estimated_start=queue.estimated_start)


def _read_exit_code(path: Path) -> int:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise SlurmError.malformed_result() from exc
    if not 0 <= value <= _MAX_PROCESS_EXIT_CODE:
        raise SlurmError.malformed_result()
    return value


def _read_nonnegative_seconds(path: Path) -> float:
    try:
        value = float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise SlurmError.malformed_result() from exc
    if not math.isfinite(value) or value < 0:
        raise SlurmError.malformed_result()
    return value


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SlurmError.malformed_result() from exc
