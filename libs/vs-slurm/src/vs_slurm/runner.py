"""Stage workspaces and execute commands on a remote Slurm cluster."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from .config import (
    PORT_PLACEHOLDER,
    SlurmConfig,
    SlurmConfigError,
    SlurmConnectorTransport,
    SlurmService,
    SlurmSshTransport,
)
from .staging import _ContentStageError, _stage_tree, _TreeStageRequest

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from threading import Event

_JOB_ID = re.compile(r"Submitted batch job ([0-9]+)")
_TERMINAL_STATES = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED"}
)
_ACCOUNTING_FIELD_COUNT = 2
_MAX_PROCESS_EXIT_CODE = 255
_BATCH_RESULT_ROOT = ".vibesys-slurm-results"
_CONTENT_CACHE_ROOT = ".vibesys-content-cache"
_STAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")


class SlurmProcess(Protocol):
    """Injectable process boundary for SSH, rsync, or a custom connector."""

    def __call__(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Run one transport process under a hard timeout."""
        ...


@dataclass(frozen=True)
class _TransportResponse:
    stdout: str
    stderr: str


@dataclass(frozen=True)
class SlurmFileArtifact:
    """A candidate-relative remote file copied to an explicit local path."""

    remote_path: str
    local_path: Path


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


@dataclass(frozen=True)
class SlurmJobResult:
    """Trusted scheduler identity, process status, and combined job output."""

    job_id: str
    exit_code: int
    output: str


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


class SlurmJobStatus(StrEnum):
    """Scheduler state visible through the public job lifecycle API."""

    UNKNOWN = "unknown"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_PUBLIC_TERMINAL_STATES = frozenset(
    {SlurmJobStatus.COMPLETED, SlurmJobStatus.FAILED, SlurmJobStatus.CANCELLED}
)


class SlurmArtifactTarget(BaseModel):
    """Artifact mapping retained in a recoverable operation handle."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    remote_path: str
    local_path: Path
    kind: Literal["file", "tree"]


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
        if re.fullmatch(r"[0-9]+", value) is None:
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
    """Status, separate output streams, timing, and collected artifacts."""

    name: str
    exit_code: int | None
    stdout: str
    stderr: str
    elapsed_seconds: float | None
    skipped: bool
    artifacts: tuple[SlurmArtifactTarget, ...] = ()


@dataclass(frozen=True)
class SlurmBatchResult:
    """One allocation result with ordered stage outcomes and phase timings."""

    job_id: str
    job_exit_code: int
    job_output: str
    stages: tuple[SlurmBatchStageResult, ...]
    phase_timings_seconds: Mapping[str, float]
    content_cache_hits: int


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

    @property
    def terminal(self) -> bool:
        """Whether the scheduler reported a terminal job state."""
        return self.status in {
            SlurmJobStatus.COMPLETED,
            SlurmJobStatus.FAILED,
            SlurmJobStatus.CANCELLED,
        }


class SlurmError(RuntimeError):
    """Actionable failure from validation, transport, Slurm, or artifact collection."""

    @classmethod
    def invalid_command(cls) -> SlurmError:
        """Describe an empty or malformed command vector."""
        return cls("Slurm job command must be a non-empty argv")

    @classmethod
    def invalid_batch_request(cls) -> SlurmError:
        """Describe an empty batch or an unsafe or repeated stage name."""
        return cls("Slurm batch must contain stages with unique safe names")

    @classmethod
    def invalid_stage_timeout(cls) -> SlurmError:
        """Describe a stage timeout outside the supported integer range."""
        return cls("Slurm batch stage timeout must be a positive integer number of seconds")

    @classmethod
    def content_stage_failed(cls) -> SlurmError:
        """Describe a local tree that could not be safely content-addressed."""
        return cls("Slurm input changed or became invalid during content-addressed staging")

    @classmethod
    def content_cache_busy(cls, detail: str) -> SlurmError:
        """Describe a bounded wait on another cache publisher that can be retried."""
        return cls(f"Slurm content staging is temporarily unavailable: {detail}")

    @classmethod
    def workspace_is_not_directory(cls) -> SlurmError:
        """Describe a missing or non-directory workspace."""
        return cls("Slurm job workspace must be an existing directory")

    @classmethod
    def missing_job_id(cls) -> SlurmError:
        """Describe submission output without a job identifier."""
        return cls("sbatch did not return a Slurm job id")

    @classmethod
    def job_timed_out(cls, job_id: str) -> SlurmError:
        """Describe a job that exceeded its configured deadline."""
        return cls(f"Slurm job {job_id} exceeded the configured timeout")

    @classmethod
    def transport_unavailable(cls, operation: str) -> SlurmError:
        """Describe a missing transport executable."""
        return cls(f"Slurm transport is unavailable during {operation}")

    @classmethod
    def transport_timed_out(cls, operation: str) -> SlurmError:
        """Describe a transport request that exceeded its deadline."""
        return cls(f"Slurm transport timed out during {operation}")

    @classmethod
    def transport_failed(cls, operation: str, returncode: int) -> SlurmError:
        """Describe an unsuccessful transport operation without exposing output."""
        return cls(f"Slurm transport {operation} failed with exit code {returncode}")

    @classmethod
    def invalid_connector_response(cls, operation: str) -> SlurmError:
        """Describe a response that violates the connector protocol."""
        return cls(f"Slurm connector returned an invalid response during {operation}")

    @classmethod
    def invalid_artifact_path(cls) -> SlurmError:
        """Describe an artifact outside the staged workspace."""
        return cls("Slurm artifact paths must be safe candidate-relative paths")

    @classmethod
    def invalid_support_path(cls) -> SlurmError:
        """Describe an invalid local or remote support tree."""
        return cls("Slurm support trees must map safe relative paths to directories")

    @classmethod
    def invalid_setup_script(cls) -> SlurmError:
        """Describe an unsafe remote setup script path."""
        return cls("Slurm setup script must be an absolute normalized POSIX path")

    @classmethod
    def malformed_result(cls) -> SlurmError:
        """Describe missing or malformed job result files."""
        return cls("Slurm job returned a missing or malformed status artifact")

    @classmethod
    def cancelled(cls, job_id: str) -> SlurmError:
        """Describe a submitted job cancelled by its caller."""
        return cls(f"Slurm job {job_id} was cancelled")

    @classmethod
    def cancelled_before_submission(cls) -> SlurmError:
        """Describe a request cancelled before side effects began."""
        return cls("Slurm job was cancelled before submission")

    @classmethod
    def job_not_terminal(cls, job_id: str) -> SlurmError:
        """Describe a collection attempt before scheduler completion."""
        return cls(f"Slurm job {job_id} is not terminal")

    @classmethod
    def invalid_wait_timeout(cls) -> SlurmError:
        """Describe a non-positive bounded observation interval."""
        return cls("timeout_seconds must be greater than zero")

    @classmethod
    def handle_config_mismatch(cls) -> SlurmError:
        """Describe a handle used against a different configured cluster."""
        return cls("Slurm job handle does not belong to this cluster configuration")

    @classmethod
    def invalid_handle(cls) -> SlurmError:
        """Describe a handle whose remote paths do not match its identity."""
        return cls("Slurm job handle contains paths outside its operation workspace")


class SlurmJobRunner:
    """Run a command in Slurm without knowing cluster credentials or transport."""

    def __init__(
        self,
        config: SlurmConfig,
        *,
        process: SlurmProcess | None = None,
        clock: Callable[[], float] = time.monotonic,
        pause: Callable[[float], None] = time.sleep,
        invocation_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        """Create a runner with injectable process, clock, and identity effects."""
        self._config = config
        self._process = process or _run_process
        self._transport = _make_transport(
            config,
            process=self._process,
        )
        self._clock = clock
        self._pause = pause
        self._invocation_id = invocation_id

    def run(self, request: SlurmJobRequest) -> SlurmJobResult:
        """Stage, submit, wait, and collect as a blocking compatibility API."""
        if request.cancel_event is not None and request.cancel_event.is_set():
            raise SlurmError.cancelled_before_submission()
        handle = self.submit(request)
        while True:
            if request.cancel_event is not None and request.cancel_event.is_set():
                self.cancel(handle)
                raise SlurmError.cancelled(handle.job_id)
            outcome = self.wait(handle, cancel_event=request.cancel_event)
            if outcome.timed_out:
                with suppress(SlurmError):
                    self.cancel(handle)
                raise SlurmError.job_timed_out(handle.job_id)
            if outcome.terminal:
                break
        if outcome.status == SlurmJobStatus.CANCELLED:
            raise SlurmError.cancelled(handle.job_id)
        return self.collect(handle)

    def submit(self, request: SlurmJobRequest) -> SlurmJobHandle:
        """Stage a workspace and submit once, returning a recoverable handle."""
        return self._stage_and_submit(request)

    def _stage_and_submit(
        self,
        request: SlurmJobRequest,
        *,
        phase_timing_root: PurePosixPath | None = None,
    ) -> SlurmJobHandle:
        source, support, files, trees = _validate_request(request)
        if request.cancel_event is not None and request.cancel_event.is_set():
            raise SlurmError.cancelled_before_submission()
        invocation = _safe_component(self._invocation_id())
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

        staging_started = self._clock()
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
        staging_seconds = max(0.0, self._clock() - staging_started)

        with tempfile.TemporaryDirectory(prefix="vs-slurm-") as temporary:
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
                        phase_timing_root=phase_timing_root,
                    )
                ),
                encoding="utf-8",
            )
            self._transport.put(local_script, remote_script)
            job_id = self._submit(base, remote_script, remote_log)
        artifacts = tuple(
            SlurmArtifactTarget(remote_path=path.as_posix(), local_path=target, kind=kind)
            for kind, entries in (("file", files), ("tree", trees))
            for path, target in entries
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

    def submit_batch(self, request: SlurmBatchRequest) -> SlurmBatchHandle:
        """Submit ordered stages as one Slurm job and one service lifecycle."""
        stages = _validate_batch_request(request)
        started = self._clock()
        phase_root = PurePosixPath(_BATCH_RESULT_ROOT) / "phases"
        job = self._stage_and_submit(
            SlurmJobRequest(
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
            ),
            phase_timing_root=phase_root,
        )
        return SlurmBatchHandle(
            job=job,
            stages=tuple(
                SlurmBatchStageHandle(
                    name=stage.name,
                    file_artifacts=tuple(
                        SlurmArtifactTarget(
                            remote_path=item.remote_path,
                            local_path=item.local_path,
                            kind="file",
                        )
                        for item in stage.file_artifacts
                    ),
                    tree_artifacts=tuple(
                        SlurmArtifactTarget(
                            remote_path=item.remote_path,
                            local_path=item.local_path,
                            kind="tree",
                        )
                        for item in stage.tree_artifacts
                    ),
                )
                for stage in stages
            ),
            submission_seconds=max(0.0, self._clock() - started),
            stop_on_failure=request.stop_on_failure,
        )

    def poll_batch(self, handle: SlurmBatchHandle) -> SlurmJobStatus:
        """Read the scheduler state of a batch handle."""
        return self.poll(handle.job)

    def wait_batch(
        self,
        handle: SlurmBatchHandle,
        *,
        timeout_seconds: float | None = None,
        cancel_event: Event | None = None,
    ) -> SlurmBatchWaitResult:
        """Wait on a batch without changing its one-job lifecycle semantics."""
        started = self._clock()
        result = self.wait(handle.job, timeout_seconds=timeout_seconds, cancel_event=cancel_event)
        elapsed = max(0.0, self._clock() - started)
        updated = handle.model_copy(update={"waited_seconds": handle.waited_seconds + elapsed})
        return SlurmBatchWaitResult(
            handle=updated,
            status=result.status,
            timed_out=result.timed_out,
        )

    def cancel_batch(self, handle: SlurmBatchHandle) -> None:
        """Cancel the single Slurm job that owns all batch stages."""
        self.cancel(handle.job)

    def collect_batch(self, handle: SlurmBatchHandle) -> SlurmBatchResult:
        """Collect ordered stage outcomes and only artifacts from passed stages."""
        self._validate_batch_handle(handle)
        collection_started = self._clock()
        job_result = self.collect(handle.job)
        stage_results: list[SlurmBatchStageResult] = []
        timings: dict[str, float] = {
            "staging": handle.job.staging_seconds,
            "submission": max(0.0, handle.submission_seconds - handle.job.staging_seconds),
            "scheduler_wait_observation": handle.waited_seconds,
        }
        if job_result.exit_code != 0:
            timings["collection"] = max(0.0, self._clock() - collection_started)
            return SlurmBatchResult(
                job_id=job_result.job_id,
                job_exit_code=job_result.exit_code,
                job_output=job_result.output,
                stages=(),
                phase_timings_seconds=timings,
                content_cache_hits=handle.job.content_cache_hits,
            )

        with tempfile.TemporaryDirectory(prefix="vs-slurm-batch-") as temporary:
            temporary_path = Path(temporary)
            results_root = temporary_path / "results"
            results_root.mkdir()
            self._transport.get(
                PurePosixPath(handle.job.remote_workspace) / _BATCH_RESULT_ROOT,
                results_root,
                kind="tree",
            )
            for index, stage in enumerate(handle.stages):
                local_root = results_root / f"{index:04d}"
                exit_value = _read_text(local_root / "exit-code.txt").strip()
                skipped = exit_value == "SKIPPED"
                exit_code = None if skipped else _read_exit_code(local_root / "exit-code.txt")
                elapsed = _read_nonnegative_seconds(local_root / "elapsed-seconds.txt")
                stage_artifacts: list[SlurmArtifactTarget] = []
                if exit_code == 0:
                    for artifact in (*stage.file_artifacts, *stage.tree_artifacts):
                        remote_path = (
                            PurePosixPath(handle.job.remote_workspace) / artifact.remote_path
                        )
                        local_path = artifact.local_path
                        if artifact.kind == "file":
                            local_path.parent.mkdir(parents=True, exist_ok=True)
                        else:
                            local_path.mkdir(parents=True, exist_ok=True)
                        self._transport.get(remote_path, local_path, kind=artifact.kind)
                        stage_artifacts.append(artifact)
                stage_results.append(
                    SlurmBatchStageResult(
                        name=stage.name,
                        exit_code=exit_code,
                        stdout=_read_text(local_root / "stdout.txt"),
                        stderr=_read_text(local_root / "stderr.txt"),
                        elapsed_seconds=None if skipped else elapsed,
                        skipped=skipped,
                        artifacts=tuple(stage_artifacts),
                    )
                )
                if not skipped:
                    timings[f"stage:{stage.name}"] = elapsed
            phase_root = results_root / "phases"
            for phase_name, result_name in (
                ("setup", "setup-seconds.txt"),
                ("service_startup", "service-startup-seconds.txt"),
            ):
                timings[phase_name] = _read_nonnegative_seconds(phase_root / result_name)
        timings["collection"] = max(0.0, self._clock() - collection_started)
        return SlurmBatchResult(
            job_id=job_result.job_id,
            job_exit_code=job_result.exit_code,
            job_output=job_result.output,
            stages=tuple(stage_results),
            phase_timings_seconds=timings,
            content_cache_hits=handle.job.content_cache_hits,
        )

    def poll(self, handle: SlurmJobHandle) -> SlurmJobStatus:
        """Read the scheduler state for a submitted or recovered handle."""
        self._validate_handle(handle)
        active = self._transport.exec(f"squeue -h -j {handle.job_id} -o %T").stdout.strip()
        if active:
            state = active.splitlines()[0].strip().split()[0].upper()
            return _public_status(state)
        accounting = self._transport.exec(
            f"sacct -n -X -j {handle.job_id} --format=State,ExitCode"
        ).stdout.strip()
        parsed = _accounting_state(accounting)
        if parsed is None:
            return SlurmJobStatus.UNKNOWN
        state, exit_code = parsed
        if state == "COMPLETED":
            return SlurmJobStatus.COMPLETED if exit_code.startswith("0:") else SlurmJobStatus.FAILED
        return _public_status(state)

    def wait(
        self,
        handle: SlurmJobHandle,
        *,
        timeout_seconds: float | None = None,
        cancel_event: Event | None = None,
    ) -> SlurmJobWaitResult:
        """Observe until terminal or bounded timeout.

        The timeout never cancels the job. An explicitly set ``cancel_event``
        requests cancellation and raises the cancellation error; this retains
        the legacy ``run()`` behavior.
        """
        timeout = self._config.job_timeout_seconds if timeout_seconds is None else timeout_seconds
        if not math.isfinite(timeout) or timeout <= 0:
            raise SlurmError.invalid_wait_timeout()
        deadline = self._clock() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                with suppress(SlurmError):
                    self.cancel(handle)
                raise SlurmError.cancelled(handle.job_id)
            status = self.poll(handle)
            if status in _PUBLIC_TERMINAL_STATES:
                return SlurmJobWaitResult(handle=handle, status=status, timed_out=False)
            remaining = deadline - self._clock()
            if remaining <= 0:
                return SlurmJobWaitResult(handle=handle, status=status, timed_out=True)
            self._pause(min(self._config.poll_interval_seconds, remaining))

    def cancel(self, handle: SlurmJobHandle) -> None:
        """Request scheduler cancellation for an existing operation."""
        self._validate_handle(handle)
        self._transport.exec(f"scancel {handle.job_id}")

    def collect(self, handle: SlurmJobHandle) -> SlurmJobResult:
        """Collect terminal output and declared artifacts for a handle."""
        self._validate_handle(handle)
        status = self.poll(handle)
        if status == SlurmJobStatus.CANCELLED:
            raise SlurmError.cancelled(handle.job_id)
        if status not in {SlurmJobStatus.COMPLETED, SlurmJobStatus.FAILED}:
            raise SlurmError.job_not_terminal(handle.job_id)
        with tempfile.TemporaryDirectory(prefix="vs-slurm-collect-") as temporary:
            temporary_path = Path(temporary)
            local_status = temporary_path / "exit-code.txt"
            local_log = temporary_path / "job.log"
            self._transport.get(PurePosixPath(handle.remote_status_path), local_status, kind="file")
            self._transport.get(PurePosixPath(handle.remote_log_path), local_log, kind="file")
            exit_code = _read_exit_code(local_status)
            if exit_code == 0 and status == SlurmJobStatus.COMPLETED:
                for artifact in handle.artifacts:
                    local_path = artifact.local_path
                    remote_path = PurePosixPath(handle.remote_workspace) / artifact.remote_path
                    if artifact.kind == "file":
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                    else:
                        local_path.mkdir(parents=True, exist_ok=True)
                    self._transport.get(remote_path, local_path, kind=artifact.kind)
            return SlurmJobResult(
                job_id=handle.job_id,
                exit_code=exit_code,
                output=_read_text(local_log),
            )

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

    def _submit(self, base: PurePosixPath, script: PurePosixPath, output: PurePosixPath) -> str:
        argv = (
            *self._config.sbatch_command,
            *self._config.sbatch_arguments,
            f"--output={output.as_posix()}",
            f"--error={output.as_posix()}",
            script.as_posix(),
        )
        result = self._transport.exec(f"cd {shlex.quote(base.as_posix())} && {shlex.join(argv)}")
        match = _JOB_ID.search(result.stdout)
        if match is None:
            raise SlurmError.missing_job_id()
        return match.group(1)


class _ConnectorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int
    returncode: int
    stdout: str
    stderr: str


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
    def __init__(self, process: SlurmProcess, timeout: float) -> None:
        self._process = process
        self._timeout = timeout

    def _run(
        self,
        argv: Sequence[str],
        *,
        operation: str,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._process(argv, stdin=stdin, timeout=self._timeout)
        except FileNotFoundError as exc:
            raise SlurmError.transport_unavailable(operation) from exc
        except subprocess.TimeoutExpired as exc:
            raise SlurmError.transport_timed_out(operation) from exc
        if result.returncode != 0:
            raise SlurmError.transport_failed(operation, result.returncode)
        return result


class _SshTransport(_ProcessTransport):
    def __init__(
        self,
        config: SlurmSshTransport,
        *,
        process: SlurmProcess,
        timeout: float,
    ) -> None:
        super().__init__(process, timeout)
        self._config = config

    def exec(self, command: str) -> _TransportResponse:
        result = self._run(
            (*self._config.ssh_command, "--", self._config.host, command),
            operation="exec",
        )
        return _TransportResponse(stdout=result.stdout, stderr=result.stderr)

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
        self._run(
            (
                *self._config.rsync_command,
                *options,
                "-e",
                shlex.join(self._config.ssh_command),
                "--",
                f"{local}/",
                self._remote(remote, trailing_slash=True),
            ),
            operation="sync_to",
        )

    def put(self, local: Path, remote: PurePosixPath) -> None:
        self._run(
            (
                *self._config.rsync_command,
                "-a",
                "-e",
                shlex.join(self._config.ssh_command),
                "--",
                str(local),
                self._remote(remote),
            ),
            operation="put",
        )

    def get(self, remote: PurePosixPath, local: Path, *, kind: Literal["file", "tree"]) -> None:
        tree = kind == "tree"
        self._run(
            (
                *self._config.rsync_command,
                "-a",
                "-e",
                shlex.join(self._config.ssh_command),
                "--",
                self._remote(remote, trailing_slash=tree),
                f"{local}/" if tree else str(local),
            ),
            operation="get",
        )

    def _remote(self, path: PurePosixPath, *, trailing_slash: bool = False) -> str:
        suffix = "/" if trailing_slash else ""
        return f"{self._config.host}:{path.as_posix()}{suffix}"


class _ConnectorTransport(_ProcessTransport):
    def __init__(
        self,
        config: SlurmConnectorTransport,
        *,
        process: SlurmProcess,
        timeout: float,
    ) -> None:
        super().__init__(process, timeout)
        self._config = config

    def exec(self, command: str) -> _TransportResponse:
        return self._request({"version": 1, "operation": "exec", "command": command}, "exec")

    def sync_to(
        self,
        local: Path,
        remote: PurePosixPath,
        *,
        delete: bool,
        excludes: Sequence[str],
    ) -> None:
        self._request(
            {
                "version": 1,
                "operation": "sync_to",
                "local_dir": str(local),
                "remote_dir": remote.as_posix(),
                "delete": delete,
                "excludes": list(excludes),
            },
            "sync_to",
        )

    def put(self, local: Path, remote: PurePosixPath) -> None:
        self._request(
            {
                "version": 1,
                "operation": "put",
                "local_path": str(local),
                "remote_path": remote.as_posix(),
            },
            "put",
        )

    def get(self, remote: PurePosixPath, local: Path, *, kind: Literal["file", "tree"]) -> None:
        self._request(
            {
                "version": 1,
                "operation": "get",
                "remote_path": remote.as_posix(),
                "local_path": str(local),
                "kind": kind,
            },
            "get",
        )

    def _request(self, request: dict[str, object], operation: str) -> _TransportResponse:
        result = self._run(
            self._config.command,
            operation=operation,
            stdin=json.dumps(request, separators=(",", ":")),
        )
        try:
            response = _ConnectorResponse.model_validate_json(result.stdout, strict=True)
        except ValidationError as exc:
            raise SlurmError.invalid_connector_response(operation) from exc
        if response.version != 1:
            raise SlurmError.invalid_connector_response(operation)
        if response.returncode != 0:
            raise SlurmError.transport_failed(operation, response.returncode)
        return _TransportResponse(stdout=response.stdout, stderr=response.stderr)


def _make_transport(config: SlurmConfig, *, process: SlurmProcess) -> _Transport:
    transport = config.transport
    if isinstance(transport, SlurmSshTransport):
        return _SshTransport(
            transport,
            process=process,
            timeout=config.transport_timeout_seconds,
        )
    return _ConnectorTransport(
        transport,
        process=process,
        timeout=config.transport_timeout_seconds,
    )


def _run_process(
    argv: Sequence[str], *, stdin: str | None, timeout: float
) -> subprocess.CompletedProcess[str]:
    # lint-waiver: LW-020071 [S603]; configured transport argv is the library's
    # > process boundary. Shell parsing would weaken argument separation, while
    # > wrapping subprocess again would expose the same argv and timeout.
    return subprocess.run(  # noqa: S603
        argv,
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
    )


def _validate_request(
    request: SlurmJobRequest,
) -> tuple[
    Path,
    list[tuple[PurePosixPath, Path]],
    list[tuple[PurePosixPath, Path]],
    list[tuple[PurePosixPath, Path]],
]:
    if not request.command or any(not part for part in request.command):
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

    support: list[tuple[PurePosixPath, Path]] = []
    for relative, local_path in sorted((request.support_trees or {}).items()):
        remote = _safe_relative_path(relative, SlurmError.invalid_support_path)
        try:
            local = local_path.resolve(strict=True)
        except OSError as exc:
            raise SlurmError.invalid_support_path() from exc
        if not local.is_dir():
            raise SlurmError.invalid_support_path()
        support.append((remote, local))

    files = [
        (_safe_relative_path(item.remote_path, SlurmError.invalid_artifact_path), item.local_path)
        for item in request.file_artifacts
    ]
    trees = [
        (_safe_relative_path(item.remote_path, SlurmError.invalid_artifact_path), item.local_path)
        for item in request.tree_artifacts
    ]
    return source, support, files, trees


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


def _batch_script(stages: Sequence[SlurmBatchStage], *, stop_on_failure: bool) -> str:
    lines = ["set +e", f"mkdir -p {shlex.quote(_BATCH_RESULT_ROOT)}", "batch_failed=0"]
    for index, stage in enumerate(stages):
        result_root = PurePosixPath(_BATCH_RESULT_ROOT) / f"{index:04d}"
        stdout_path = result_root / "stdout.txt"
        stderr_path = result_root / "stderr.txt"
        exit_path = result_root / "exit-code.txt"
        elapsed_path = result_root / "elapsed-seconds.txt"
        command = _shell_join_dynamic(stage.command)
        if stage.timeout_seconds is not None:
            command = f"timeout --signal=TERM --kill-after=5s {stage.timeout_seconds}s {command}"
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
    if service is not None:
        lines.extend(
            _service_script(service, phase_timing_root=phase_timing_root, workspace=workspace)
        )
    elif phase_timing_root is not None:
        timing_path = workspace / phase_timing_root / "service-startup-seconds.txt"
        lines.append(f"printf '%s\\n' 0 > {shlex.quote(timing_path.as_posix())}")
    lines.extend([_shell_join_dynamic(command), "job_status=$?"])
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
    command = _shell_join_dynamic(service.command)
    readiness_url = shlex.quote(service.readiness_url)
    lines = [
        "export PORT=$((20000 + SLURM_JOB_ID % 10000))",
        'service_pid=""',
        'stop_service() { if [ -n "$service_pid" ]; then kill "$service_pid" 2>/dev/null || true; wait "$service_pid" 2>/dev/null || true; fi; }',
        "trap stop_service EXIT",
        f"{command} > .vs-slurm-service.log 2>&1 &",
        "service_pid=$!",
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
    lines.append('if [ "$ready" -ne 1 ]; then tail -40 .vs-slurm-service.log; job_status=70; else')
    return lines


def _shell_join_dynamic(arguments: Sequence[str]) -> str:
    parts: list[str] = []
    for argument in arguments:
        if argument == PORT_PLACEHOLDER:
            parts.append('"${PORT}"')
        elif PORT_PLACEHOLDER in argument:
            before, after = argument.split(PORT_PLACEHOLDER, maxsplit=1)
            parts.append(f'"{before}${{PORT}}{after}"')
        else:
            parts.append(shlex.quote(argument))
    return " ".join(parts)


def _accounting_state(output: str) -> tuple[str, str] | None:
    for line in output.splitlines():
        fields = line.strip().split()
        if len(fields) >= _ACCOUNTING_FIELD_COUNT:
            return fields[0].split("+")[0], fields[1]
    return None


def _public_status(state: str) -> SlurmJobStatus:
    if state in {"PENDING", "CONFIGURING", "REQUEUED", "RESV_DEL_HOLD"}:
        return SlurmJobStatus.PENDING
    if state in {"RUNNING", "COMPLETING", "SUSPENDED", "STAGE_OUT"}:
        return SlurmJobStatus.RUNNING
    if state == "COMPLETED":
        return SlurmJobStatus.COMPLETED
    if state in {"CANCELLED", "PREEMPTED"}:
        return SlurmJobStatus.CANCELLED
    if state in _TERMINAL_STATES:
        return SlurmJobStatus.FAILED
    return SlurmJobStatus.UNKNOWN


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
    except OSError as exc:
        raise SlurmError.malformed_result() from exc
