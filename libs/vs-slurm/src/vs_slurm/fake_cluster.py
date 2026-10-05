"""Faithful in-memory cluster with deterministic scheduler and reply-loss scripts."""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from threading import RLock
from typing import TYPE_CHECKING, Protocol, TypeAlias, TypedDict, Unpack

from pydantic import BaseModel, ConfigDict, model_validator

from .cluster import job_handle, payload_digest
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
    ClusterResult,
    ClusterSubmitOutcome,
    ClusterSubmitted,
    ClusterTarget,
    ClusterUnknown,
    collection_problem,
    validate_operation_id,
)
from .runner import (
    SlurmArtifactTarget,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStageHandle,
    SlurmBatchStageResult,
    SlurmError,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobResult,
    SlurmJobStatus,
    SlurmPhase,
    SlurmSubmissionRejectedError,
    _safe_relative_path,
)
from .staging import _ContentStageError

if TYPE_CHECKING:
    from collections.abc import Callable

Request: TypeAlias = SlurmJobRequest | SlurmBatchRequest


class Clock(Protocol):
    """The time source a Fake cluster reads. Tests advance it; nothing sleeps."""

    def now(self) -> float:
        """Seconds on the Fake's own timeline."""
        ...


class ManualClock:
    """A clock that moves only when told to, so scheduler time is deterministic."""

    def __init__(self, start: float = 0.0) -> None:
        """Start the timeline at ``start`` seconds."""
        self._now = start
        self._lock = RLock()

    def now(self) -> float:
        """Current time on the timeline."""
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        """Move the timeline forward."""
        if seconds < 0:
            raise SlurmError.invalid_script_states()
        with self._lock:
            self._now += seconds


class SecondsRange(BaseModel):
    """A measured duration range: real clusters vary, so a range, not a point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    low: float
    high: float

    @model_validator(mode="after")
    def _ordered(self) -> SecondsRange:
        if not 0 <= self.low <= self.high:
            raise SlurmError.invalid_script_states()
        return self

    @classmethod
    def exactly(cls, seconds: float) -> SecondsRange:
        """A range that always yields ``seconds``."""
        return cls(low=seconds, high=seconds)


class SlurmTimingProfile(BaseModel):
    """Scheduler timing parameters measured on a production cluster.

    Defaults are production-like: a queue wait of 5.7 to 95 s, a job of 138 to
    168 s, and 31 to 40 s in COMPLETING after the job script exits or is cancelled.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    queue_wait_s: SecondsRange = SecondsRange(low=5.7, high=95.0)
    run_s: SecondsRange = SecondsRange(low=138.0, high=168.0)
    completing_s: SecondsRange = SecondsRange(low=30.9, high=40.0)

    @classmethod
    def instant(cls) -> SlurmTimingProfile:
        """No queue wait and no teardown: for suites that compare against a lag-free peer."""
        zero = SecondsRange.exactly(0.0)
        return cls(queue_wait_s=zero, run_s=zero, completing_s=zero)


class _ScriptOptions(TypedDict, total=False):
    pending_reason: str | None
    estimated_start: str | None
    result: ClusterResult | None
    lost_submit_reply: bool
    missing_exit_status: bool
    artifact_contents: dict[str, str]
    on_accept: Callable[[], None]
    on_dispatch: Callable[[], None]
    rejected_reason: str
    queue_wait_s: float
    run_s: float
    completing_s: float
    requeues: int
    exit_status: SlurmJobStatus


@dataclass
class _Script:
    # None: the job follows a timeline over the Fake's clock. A tuple scripts the
    # exact observation sequence, one entry per inspection, with no timing.
    states: tuple[SlurmJobStatus, ...] | None = None
    queue_wait_s: float | None = None
    run_s: float | None = None
    completing_s: float | None = None
    requeues: int = 0
    exit_status: SlurmJobStatus = SlurmJobStatus.COMPLETED
    pending_reason: str | None = None
    estimated_start: str | None = None
    result: ClusterResult | None = None
    lost_submit_reply: bool = False
    missing_exit_status: bool = False
    artifact_contents: dict[str, str] | None = None
    on_accept: Callable[[], None] | None = None
    on_dispatch: Callable[[], None] | None = None
    rejected_reason: str | None = None
    index: int = 0


@dataclass(frozen=True)
class _Attempt:
    pending_from: float
    running_from: float
    ends_at: float


@dataclass(frozen=True)
class _Timeline:
    """One job's schedule on the Fake's clock, fixed when the job is accepted."""

    attempts: tuple[_Attempt, ...]
    completing_s: float
    terminal: SlurmJobStatus

    @property
    def exits_at(self) -> float:
        return self.attempts[-1].ends_at

    def natural(self, now: float) -> tuple[SlurmJobStatus, SlurmPhase, int]:
        """The scheduler state at ``now`` if nobody cancels."""
        last = len(self.attempts) - 1
        for index, attempt in enumerate(self.attempts):
            if now < attempt.running_from:
                return SlurmJobStatus.PENDING, SlurmPhase.PENDING, index
            if now < attempt.ends_at:
                return SlurmJobStatus.RUNNING, SlurmPhase.RUNNING, index
            if index == last:
                if now < attempt.ends_at + self.completing_s:
                    return SlurmJobStatus.RUNNING, SlurmPhase.COMPLETING, index
                return self.terminal, SlurmPhase.ENDED, index
        raise AssertionError  # unreachable: the loop returns on the last attempt

    def state(
        self, now: float, cancelled_at: float | None
    ) -> tuple[SlurmJobStatus, SlurmPhase, int]:
        """The scheduler state at ``now``, with a cancellation at ``cancelled_at``."""
        if cancelled_at is None or cancelled_at >= self.exits_at:
            return self.natural(now)
        _, phase_then, attempt = self.natural(cancelled_at)
        if now < cancelled_at:
            return self.natural(now)
        if phase_then is SlurmPhase.PENDING:
            return SlurmJobStatus.CANCELLED, SlurmPhase.ENDED, attempt
        if now < cancelled_at + self.completing_s:
            return SlurmJobStatus.RUNNING, SlurmPhase.COMPLETING, attempt
        return SlurmJobStatus.CANCELLED, SlurmPhase.ENDED, attempt


_STATES_PHASE = {
    SlurmJobStatus.PENDING: SlurmPhase.PENDING,
    SlurmJobStatus.RUNNING: SlurmPhase.RUNNING,
    SlurmJobStatus.COMPLETED: SlurmPhase.ENDED,
    SlurmJobStatus.FAILED: SlurmPhase.ENDED,
    SlurmJobStatus.CANCELLED: SlurmPhase.ENDED,
    SlurmJobStatus.UNKNOWN: SlurmPhase.UNKNOWN,
}


@dataclass
class _Job:
    operation_id: str
    digest: str
    handle: ClusterHandle
    result: ClusterResult
    cancelled: bool = False
    cancelled_at: float | None = None
    timeline: _Timeline | None = None
    acceptance_observed: bool = False
    accepted_published: bool = False


class FakeCluster:
    """Same input validation and ambiguity rules as SlurmCluster, without I/O."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        timing: SlurmTimingProfile | None = None,
        seed: int = 0,
    ) -> None:
        """Create the implementation with its owned operation storage.

        Job state is a function of ``clock``: it advances only when the test
        advances it. ``timing`` bounds queue wait, run time and COMPLETING time;
        each job's values are drawn deterministically from ``seed`` and its
        operation identity.
        """
        self.clock: Clock = clock if clock is not None else ManualClock()
        self._timing = timing if timing is not None else SlurmTimingProfile()
        self._seed = seed
        self._lock = RLock()
        self._scripts: dict[str, _Script] = {}
        self._jobs: dict[str, _Job] = {}
        self._cancelled: set[str] = set()
        self._pending: dict[str, str] = {}
        self._rejections: dict[str, tuple[str, str]] = {}
        self._expired_names: set[str] = set()

    def reopen(self) -> FakeCluster:
        """Reconstruct an implementation over the same external scheduler state."""
        reopened = FakeCluster(clock=self.clock, timing=self._timing, seed=self._seed)
        reopened._lock = self._lock
        reopened._scripts = self._scripts
        reopened._jobs = self._jobs
        reopened._cancelled = self._cancelled
        reopened._pending = self._pending
        reopened._rejections = self._rejections
        reopened._expired_names = self._expired_names
        return reopened

    def forget_name_history(self, operation_id: str) -> None:
        """Expire scheduler name lookup while retained acceptance stays recoverable."""
        validate_operation_id(operation_id)
        self._expired_names.add(operation_id)

    def on_accept(self, operation_id: str, callback: Callable[[], None]) -> None:
        """Run a deterministic test barrier after external acceptance."""
        validate_operation_id(operation_id)
        self._scripts.setdefault(operation_id, _Script()).on_accept = callback

    def script(
        self,
        operation_id: str,
        *,
        states: tuple[SlurmJobStatus, ...] | None = None,
        pending_polls: int = 0,
        **options: Unpack[_ScriptOptions],
    ) -> None:
        """Script a job, either on the Fake's clock or as an exact observation sequence.

        Without ``states`` the job follows a timeline on ``clock``: PENDING for
        its queue wait, RUNNING for its run time, then COMPLETING (public status
        RUNNING) before the terminal ``exit_status``. A cancel of a running job
        also passes through COMPLETING. ``queue_wait_s``, ``run_s`` and
        ``completing_s`` override the profile's drawn values, ``requeues`` is how
        many times the scheduler requeues the job (each starts a new attempt),
        and ``run_s=math.inf`` never exits by itself.

        With ``states`` each inspection returns the next entry, with no timing:
        the scripted sequence is the observation. ``pending_polls`` prepends that
        many PENDING entries.
        """
        validate_operation_id(operation_id)
        if pending_polls < 0 or options.get("requeues", 0) < 0:
            raise SlurmError.invalid_script_states()
        if any(options.get(name, 0.0) < 0 for name in ("queue_wait_s", "run_s", "completing_s")):
            raise SlurmError.invalid_script_states()
        if states is not None and (
            not states or any(not isinstance(state, SlurmJobStatus) for state in states)
        ):
            raise SlurmError.invalid_script_states()
        if states is None and pending_polls:
            raise SlurmError.invalid_script_states()
        for path, content in options.get("artifact_contents", {}).items():
            _safe_relative_path(path, SlurmError.invalid_artifact_path)
            if not isinstance(content, str):
                raise SlurmError.invalid_artifact_path()
        if states is not None:
            states = (SlurmJobStatus.PENDING,) * pending_polls + states
        self._scripts[operation_id] = _Script(states=states, **options)

    def submit(self, request: Request, *, operation_id: str) -> ClusterSubmitOutcome:
        """Validate and submit once under a caller-supplied stable identity."""
        try:
            validate_operation_id(operation_id)
            digest = payload_digest(request)
        except (SlurmError, OSError, ValueError, _ContentStageError) as exc:
            return ClusterRejected(operation_id=operation_id, reason=str(exc))
        with self._lock:
            started = self._reserve(request, operation_id, digest)
        if not isinstance(started, _Script):
            return started
        try:
            if started.on_dispatch is not None:
                started.on_dispatch()
        except SlurmSubmissionRejectedError as exc:
            return self._reject(operation_id, digest, str(exc))
        except (SlurmError, OSError) as exc:
            return ClusterUnknown(operation_id=operation_id, reason=str(exc))
        if started.rejected_reason is not None:
            return self._reject(operation_id, digest, started.rejected_reason)
        with self._lock:
            job = self._accept(request, operation_id, digest, started)
            self._pending.pop(operation_id, None)
        return self._reply(job, started)

    def _reserve(
        self, request: Request, operation_id: str, digest: str
    ) -> _Script | ClusterSubmitOutcome:
        existing = self._jobs.get(operation_id)
        if existing is not None:
            return self._existing(existing, digest)
        rejected = self._rejections.get(operation_id)
        if rejected is not None:
            return (
                ClusterConflict(
                    operation_id=operation_id, reason="operation_id already names another payload"
                )
                if rejected[0] != digest
                else ClusterRejected(operation_id=operation_id, reason=rejected[1])
            )
        pending = self._pending.get(operation_id)
        if pending is not None:
            return (
                ClusterConflict(
                    operation_id=operation_id, reason="operation_id already names another payload"
                )
                if pending != digest
                else ClusterUnknown(
                    operation_id=operation_id, reason="submission acceptance unresolved"
                )
            )
        if operation_id in self._cancelled or (
            request.cancel_event is not None and request.cancel_event.is_set()
        ):
            if request.cancel_event is not None and request.cancel_event.is_set():
                self._rejections[operation_id] = (digest, "operation cancelled before submission")
            self._cancelled.add(operation_id)
            return ClusterRejected(
                operation_id=operation_id, reason="operation cancelled before submission"
            )
        script = self._scripts.setdefault(operation_id, _Script())
        self._pending[operation_id] = digest
        return script

    def _reject(self, operation_id: str, digest: str, reason: str) -> ClusterRejected:
        with self._lock:
            self._rejections[operation_id] = (digest, reason)
            self._pending.pop(operation_id, None)
        return ClusterRejected(operation_id=operation_id, reason=reason)

    def _accept(self, request: Request, operation_id: str, digest: str, script: _Script) -> _Job:
        handle = self._handle(request, operation_id, str(len(self._jobs) + 1))
        result = script.result or self._unknown_result(request, handle)
        result = replace(result, job_id=job_handle(handle).job_id)
        if script.missing_exit_status:
            result = (
                replace(
                    result,
                    job_exit_code=None,
                    collection_failure=result.collection_failure
                    or "missing allocation exit status",
                )
                if isinstance(result, SlurmBatchResult)
                else replace(
                    result,
                    exit_code=None,
                    collection_failure=result.collection_failure
                    or "missing allocation exit status",
                )
            )
        job = _Job(operation_id, digest, handle, result, acceptance_observed=False)
        if script.states is None:
            job.timeline = self._draw_timeline(operation_id, script)
        self._jobs[operation_id] = job
        return job

    def _draw_timeline(self, operation_id: str, script: _Script) -> _Timeline:
        """Fix a job's schedule at acceptance from the profile and the script."""
        rng = random.Random(f"{self._seed}/{operation_id}")  # noqa: S311  # lint-waiver: LW-731201 [S311]; a seeded draw of Fake scheduler timing, not a security value.

        def draw(override: float | None, bounds: SecondsRange) -> float:
            if override is not None:
                return override
            return rng.uniform(bounds.low, bounds.high)

        at = self.clock.now()
        attempts = []
        for index in range(script.requeues + 1):
            running_from = at + draw(script.queue_wait_s, self._timing.queue_wait_s)
            run = draw(script.run_s, self._timing.run_s)
            # A requeued attempt is interrupted partway through its run.
            ends_at = running_from + (run if index == script.requeues else run / 2)
            attempts.append(_Attempt(at, running_from, ends_at))
            at = ends_at
        completing = draw(script.completing_s, self._timing.completing_s)
        return _Timeline(tuple(attempts), completing, script.exit_status)

    def _reply(self, job: _Job, script: _Script) -> ClusterSubmitOutcome:
        operation_id = job.operation_id
        handle = job.handle
        if script.on_accept is not None:
            script.on_accept()
        with self._lock:
            if not script.lost_submit_reply:
                job.acceptance_observed = True
                job.accepted_published = True
        with self._lock:
            if operation_id in self._cancelled:
                self._cancel(operation_id, by_job_id=False)
        if script.lost_submit_reply:
            script.lost_submit_reply = False
            return ClusterUnknown(operation_id=operation_id, reason="lost submit reply")
        return ClusterSubmitted(operation_id=operation_id, handle=handle)

    def _existing(self, job: _Job, digest: str) -> ClusterSubmitOutcome:
        if job.digest != digest:
            return ClusterConflict(
                operation_id=job.operation_id, reason="operation_id already names another payload"
            )
        if not job.acceptance_observed or job.operation_id in self._cancelled:
            observed = self.inspect(job.operation_id)
            if isinstance(observed, ClusterUnknown):
                return observed
        return ClusterSubmitted(operation_id=job.operation_id, handle=job.handle)

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        """Observe scheduler evidence without submitting work."""
        try:
            with self._lock:
                return self._inspect(target, by_job_id=by_job_id)
        except SlurmError as error:
            return ClusterUnknown(
                operation_id=target if isinstance(target, str) and not by_job_id else None,
                reason=str(error),
            )

    def _inspect(self, target: ClusterTarget, *, by_job_id: bool) -> ClusterInspectOutcome:
        job = self._find(target, by_job_id=by_job_id)
        if job is None:
            return ClusterUnknown(
                operation_id=target if isinstance(target, str) and not by_job_id else None,
                reason="operation not found",
            )
        if job.operation_id in self._expired_names and not job.accepted_published and not by_job_id:
            return ClusterUnknown(
                operation_id=job.operation_id, reason="submission acceptance unresolved"
            )
        job.acceptance_observed = True
        job.accepted_published = True
        script = self._scripts[job.operation_id]
        status, phase, attempt = self._observe(job, script)
        identity = job_handle(job.handle).job_id
        if status == SlurmJobStatus.UNKNOWN:
            return ClusterUnknown(
                operation_id=job.operation_id, job_id=identity, reason="scheduler state unknown"
            )
        return ClusterObservation(
            operation_id=job.operation_id,
            job_id=identity,
            status=status,
            phase=phase,
            attempt=attempt,
            pending_reason=script.pending_reason if status == SlurmJobStatus.PENDING else None,
            estimated_start=script.estimated_start if status == SlurmJobStatus.PENDING else None,
            handle=job.handle,
        )

    def _observe(self, job: _Job, script: _Script) -> tuple[SlurmJobStatus, SlurmPhase, int]:
        """The job's state now: from the clock, or the next scripted observation."""
        if job.timeline is not None:
            return job.timeline.state(self.clock.now(), job.cancelled_at)
        states = script.states if script.states is not None else (SlurmJobStatus.PENDING,)
        status = (
            SlurmJobStatus.CANCELLED
            if job.cancelled
            else states[min(script.index, len(states) - 1)]
        )
        script.index += 1
        if job.operation_id in self._cancelled and status in {
            SlurmJobStatus.PENDING,
            SlurmJobStatus.RUNNING,
        }:
            # Cancellation reaches the scheduler after this observation. Its
            # next state may already be terminal, which scancel must preserve.
            current = states[min(script.index, len(states) - 1)]
            if current in {SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING}:
                job.cancelled = True
        return status, _STATES_PHASE[status], 0

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        """Record cancellation intent, leaving confirmation to inspect."""
        try:
            with self._lock:
                return self._cancel(target, by_job_id=by_job_id)
        except SlurmError as error:
            return ClusterUnknown(
                operation_id=target if isinstance(target, str) and not by_job_id else None,
                reason=str(error),
            )

    def _cancel(self, target: ClusterTarget, *, by_job_id: bool) -> ClusterCancelOutcome:
        job = self._find(target, by_job_id=by_job_id)
        if job is None:
            if isinstance(target, str) and not by_job_id:
                validate_operation_id(target)
                self._cancelled.add(target)
                return ClusterCancelRequested(operation_id=target)
            return ClusterUnknown(operation_id=None, reason="operation not found")
        self._cancelled.add(job.operation_id)
        if job.cancelled_at is None:
            job.cancelled_at = self.clock.now()
        if job.operation_id in self._expired_names and not job.accepted_published and not by_job_id:
            return ClusterUnknown(
                operation_id=job.operation_id, reason="submission acceptance unresolved"
            )
        observed = self._inspect(target, by_job_id=by_job_id)
        if isinstance(observed, ClusterUnknown):
            return observed
        return ClusterCancelRequested(
            operation_id=job.operation_id, job_id=job_handle(job.handle).job_id
        )

    def collect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCollectOutcome:
        """Collect terminal evidence, preserving partial results as Unknown."""
        observation = self.inspect(target, by_job_id=by_job_id)
        if isinstance(observation, ClusterUnknown):
            return observation
        job = self._find(target, by_job_id=by_job_id)
        if job is None:
            return ClusterUnknown(
                operation_id=observation.operation_id, reason="operation not found"
            )
        if observation.status in {SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING}:
            return ClusterUnknown(
                operation_id=job.operation_id,
                job_id=observation.job_id,
                reason="job is not terminal",
            )
        self._collect_artifacts(job, status=observation.status)
        result = job.result
        code = result.job_exit_code if isinstance(result, SlurmBatchResult) else result.exit_code
        if code == 0 and observation.status in {SlurmJobStatus.FAILED, SlurmJobStatus.CANCELLED}:
            return ClusterUnknown(
                operation_id=job.operation_id,
                job_id=observation.job_id,
                reason="scheduler terminal state contradicts zero allocation exit status",
                result=result,
            )
        missing_stage = isinstance(result, SlurmBatchResult) and (
            not isinstance(job.handle, SlurmBatchHandle)
            or len(result.stages) != len(job.handle.stages)
            or any(
                (stage.exit_code is None and not stage.skipped)
                or stage.collection_failure is not None
                for stage in result.stages
            )
        )
        problem = collection_problem(result)
        if problem is not None or missing_stage:
            return ClusterUnknown(
                operation_id=job.operation_id,
                job_id=observation.job_id,
                reason=problem or "missing stage evidence",
                result=result,
            )
        return ClusterCollected(operation_id=job.operation_id, result=result)

    def _collect_artifacts(self, job: _Job, *, status: SlurmJobStatus) -> None:
        contents = self._scripts[job.operation_id].artifact_contents or {}
        result = job.result
        if isinstance(result, SlurmBatchResult) and isinstance(job.handle, SlurmBatchHandle):
            stages = []
            handles = {stage.name: stage for stage in job.handle.stages}
            for stage in result.stages:
                declared = handles.get(stage.name)
                if type(stage.exit_code) is not int or stage.exit_code != 0 or declared is None:
                    stages.append(stage)
                    continue
                artifacts, failures = self._materialize(
                    (*declared.file_artifacts, *declared.tree_artifacts), contents
                )
                stages.append(
                    replace(
                        stage,
                        artifacts=artifacts,
                        collection_failure=stage.collection_failure or failures,
                    )
                )
            job.result = replace(result, stages=tuple(stages))
        elif isinstance(result, SlurmJobResult):
            targets = tuple(
                a
                for a in job_handle(job.handle).artifacts
                if (
                    type(result.exit_code) is int
                    and result.exit_code == 0
                    and status == SlurmJobStatus.COMPLETED
                )
                or a.collect_on_failure
            )
            _, failures = self._materialize(targets, contents)
            job.result = replace(result, collection_failure=result.collection_failure or failures)

    @staticmethod
    def _materialize(
        targets: tuple[SlurmArtifactTarget, ...], contents: dict[str, str]
    ) -> tuple[tuple[SlurmArtifactTarget, ...], str | None]:
        recovered = []
        failures = []
        for target in targets:
            try:
                if target.kind == "file" and target.remote_path in contents:
                    target.local_path.parent.mkdir(parents=True, exist_ok=True)
                    target.local_path.write_text(contents[target.remote_path], encoding="utf-8")
                    recovered.append(target)
                elif target.kind == "tree":
                    entries = {
                        name.removeprefix(target.remote_path + "/"): content
                        for name, content in contents.items()
                        if name.startswith(target.remote_path + "/")
                    }
                    if entries:
                        for name, content in entries.items():
                            local = target.local_path / name
                            local.parent.mkdir(parents=True, exist_ok=True)
                            local.write_text(content, encoding="utf-8")
                        recovered.append(target)
                    else:
                        failures.append(target.remote_path + ": artifact missing")
                else:
                    failures.append(target.remote_path + ": artifact missing")
            except OSError as error:
                failures.append(f"{target.remote_path}: {error}")
        return tuple(recovered), "; ".join(failures) or None

    def _find(self, target: ClusterTarget, *, by_job_id: bool) -> _Job | None:
        if not isinstance(target, str):
            incoming = job_handle(target)
            job = self._jobs.get(incoming.invocation_id)
            if job is None:
                return None
            expected = job_handle(job.handle)
            if (
                incoming.config_identity,
                incoming.job_id,
                incoming.remote_workspace,
                incoming.remote_status_path,
                incoming.remote_log_path,
            ) != (
                expected.config_identity,
                expected.job_id,
                expected.remote_workspace,
                expected.remote_status_path,
                expected.remote_log_path,
            ):
                return None
            return job
        if not by_job_id:
            validate_operation_id(target)
            return self._jobs.get(target)
        return next(
            (job for job in self._jobs.values() if job_handle(job.handle).job_id == target), None
        )

    @staticmethod
    def _handle(request: Request, operation_id: str, job_id: str) -> ClusterHandle:
        base = "/fake/cluster/" + operation_id
        job = SlurmJobHandle(
            job_id=job_id,
            invocation_id=operation_id,
            config_identity="0" * 64,
            remote_workspace=base + "/workspace",
            remote_status_path=base + "/exit-code.txt",
            remote_log_path=base + "/job.log",
            artifacts=()
            if isinstance(request, SlurmBatchRequest)
            else tuple(
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
        if not isinstance(request, SlurmBatchRequest):
            return job
        return SlurmBatchHandle(
            job=job,
            submission_seconds=0.0,
            stop_on_failure=request.stop_on_failure,
            stages=tuple(
                SlurmBatchStageHandle(
                    name=stage.name,
                    file_artifacts=tuple(
                        SlurmArtifactTarget(
                            remote_path=a.remote_path, local_path=a.local_path, kind="file"
                        )
                        for a in stage.file_artifacts
                    ),
                    tree_artifacts=tuple(
                        SlurmArtifactTarget(
                            remote_path=a.remote_path, local_path=a.local_path, kind="tree"
                        )
                        for a in stage.tree_artifacts
                    ),
                )
                for stage in request.stages
            ),
        )

    @staticmethod
    def _unknown_result(request: Request, handle: ClusterHandle) -> ClusterResult:
        identity = job_handle(handle).job_id
        if isinstance(request, SlurmJobRequest):
            return SlurmJobResult(
                job_id=identity,
                exit_code=None,
                output="",
                collection_failure="missing exit status",
            )
        return SlurmBatchResult(
            job_id=identity,
            job_exit_code=None,
            job_output="",
            stages=tuple(
                SlurmBatchStageResult(
                    name=stage.name,
                    exit_code=None,
                    stdout="",
                    stderr="",
                    elapsed_seconds=None,
                    skipped=False,
                )
                for stage in request.stages
            ),
            phase_timings_seconds={},
            content_cache_hits=0,
            collection_failure="missing exit status",
        )
