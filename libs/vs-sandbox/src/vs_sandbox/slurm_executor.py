"""Recoverable fused evaluation execution through Slurm."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from pydantic import BaseModel, ConfigDict, Field

from vs_evaluation.api import (
    AvailabilitySnapshot,
    AvailabilityState,
    CostClass,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    ExecutorCancellationUnconfirmedError,
    ExecutorCancellationUnknownError,
    ExecutorObservation,
    ExecutorPoll,
    ExecutorRejectedError,
    ExecutorSubmissionError,
    PollPhase,
    ResourceRequirements,
    ReuseStatus,
    StageFailureKind,
    StageState,
)
from vs_project.api import atomic_write_bytes
from vs_sandbox.slurm_wiring import make_cluster
from vs_slurm.api import (
    ClusterCollected,
    ClusterConflict,
    ClusterObservation,
    ClusterRejected,
    ClusterSubmitted,
    ClusterUnknown,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStage,
    SlurmError,
    SlurmJobStatus,
    SlurmSubmissionRejectedError,
    SlurmTreeArtifact,
    validate_cluster_operation_id,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from pathlib import Path

    from vs_slurm.api import (
        Cluster,
        ClusterCollectOutcome,
        ClusterInspectOutcome,
        SlurmConfig,
        SlurmService,
    )

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SchedulerView:
    """What one scheduler state means for the lifecycle and for a pure poll.

    This is the only place that interprets a scheduler state. ``lifecycle`` is
    None when the state carries no lifecycle information (collection owns the
    terminal result, or the state is unknown). ``staged`` says whether a workload
    stage is known to be running.
    """

    lifecycle: EvaluationState | None
    phase: PollPhase
    staged: bool = False


def _scheduler_view(status: SlurmJobStatus) -> _SchedulerView:
    match status:
        case SlurmJobStatus.PENDING:
            # An accepted job still queued in the scheduler is an active
            # evaluation. Queueing is a poll-phase fact, never a lifecycle
            # regression to local-admission QUEUED, and no stage is running yet.
            return _SchedulerView(EvaluationState.RUNNING, PollPhase.QUEUED)
        case SlurmJobStatus.RUNNING:
            return _SchedulerView(EvaluationState.RUNNING, PollPhase.RUNNING, staged=True)
        case SlurmJobStatus.CANCELLED:
            return _SchedulerView(EvaluationState.CANCELED, PollPhase.ENDED)
        case SlurmJobStatus.COMPLETED | SlurmJobStatus.FAILED:
            return _SchedulerView(None, PollPhase.ENDED)
        case SlurmJobStatus.UNKNOWN:
            return _SchedulerView(None, PollPhase.UNKNOWN)
        case _:
            assert_never(status)


def _lifecycle_observation(
    status: SlurmJobStatus, request: EvaluationRequest
) -> ExecutorObservation | None:
    """The lifecycle observation a scheduler state implies, if it implies one."""
    view = _scheduler_view(status)
    if view.lifecycle is None:
        return None
    return ExecutorObservation(
        state=view.lifecycle,
        current_stage=request.stages[0].name if view.staged else None,
    )


# Lifecycle states in the order an evaluation may move through them. A later,
# lower reading never lowers a published state: publication joins by maximum.
_LIFECYCLE_RANK = {
    EvaluationState.QUEUED: 0,
    EvaluationState.STARTING: 1,
    EvaluationState.RUNNING: 2,
}
_TERMINAL_RANK = 3


def _rank(state: EvaluationState) -> int:
    return _LIFECYCLE_RANK.get(state, _TERMINAL_RANK)


@dataclass(frozen=True)
class _CancelConfirmed:
    """The job is observed ended, or was never dispatched."""


@dataclass(frozen=True)
class _CancelRequested:
    """Cancellation was sent to a known job that has not been seen ending."""

    job_id: str | None


_CancelOutcome = _CancelConfirmed | _CancelRequested

# Real clusters keep a cancelled job in COMPLETING for tens of seconds.
_DEFAULT_CANCEL_CONFIRMATION_SECONDS = 120.0


class SlurmTargetLifecycle(StrEnum):
    """Whether a stage uses the batch's shared service."""

    SHARED_SERVICE = "shared_service"
    COMMAND_MANAGED = "command_managed"


class SlurmStagePayload(BaseModel):
    """Generic trusted command payload interpreted by the Slurm adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command: str | None = None
    timeout_seconds: int | None = Field(default=None, gt=0)
    tree_artifact_refs: tuple[str, ...] = ()
    target_lifecycle: SlurmTargetLifecycle = SlurmTargetLifecycle.SHARED_SERVICE


class SlurmExecutionMetadata(BaseModel):
    """Provider timings and immutable staging reuse for one fused batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase_timings_seconds: dict[str, float] = Field(default_factory=dict)
    content_cache_hits: int = 0
    job_exit_code: int | None = None
    collection_failure: str | None = None
    # Distinct from stage evidence: a contradiction can retain complete stages.
    aggregate_unknown: str | None = None


class SlurmCommandResult(BaseModel):
    """Portable result stored in an evaluation stage result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    executed: bool = True
    execution_metadata: SlurmExecutionMetadata | None = None
    collection_failure: str | None = None


class SlurmOutcomeUnknownError(RuntimeError):
    """Collected stage evidence cannot establish the command's outcome."""

    @classmethod
    def observation(cls, operation_id: str, reason: str) -> SlurmOutcomeUnknownError:
        """Preserve the stable identity and the ambiguous cluster observation."""
        return cls(f"Slurm operation {operation_id!r} has an unknown outcome: {reason}")

    @classmethod
    def missing_exit_code(cls, stage: str) -> SlurmOutcomeUnknownError:
        """Name the stage whose terminal evidence lacks an exit code."""
        return cls(f"Slurm stage {stage!r} has an unknown outcome: missing exit code")


class SharedSlurmAdmission:
    """Share bounded cluster capacity across workspace-scoped executors."""

    def __init__(self, capacity: int) -> None:
        """Create a fair admission pool with fixed capacity."""
        if capacity <= 0:
            raise ValueError("evaluation admission capacity must be greater than zero")  # noqa: TRY003  # lint-waiver: LW-930039 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        self.capacity = capacity
        self._active: set[str] = set()
        self._queued: list[str] = []
        self._condition = asyncio.Condition()

    async def counts(self) -> tuple[int, int]:
        """Return active and queued lease counts atomically."""
        async with self._condition:
            return len(self._active), len(self._queued)

    @asynccontextmanager
    async def lease(self, handle_id: str) -> AsyncIterator[None]:
        """Acquire one fair, cancellation-safe capacity lease."""
        acquired = False
        async with self._condition:
            if handle_id in self._active or handle_id in self._queued:
                raise ValueError(  # noqa: TRY003  # lint-waiver: LW-930040 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
                    f"evaluation handle {handle_id!r} already has an admission lease"
                )
            self._queued.append(handle_id)
            try:
                await self._condition.wait_for(
                    lambda: self._queued[0] == handle_id and len(self._active) < self.capacity
                )
                self._queued.pop(0)
                self._active.add(handle_id)
                acquired = True
            except BaseException:
                if handle_id in self._queued:
                    self._queued.remove(handle_id)
                    self._condition.notify_all()
                raise
        try:
            yield
        finally:
            if acquired:
                async with self._condition:
                    self._active.remove(handle_id)
                    self._condition.notify_all()


class _DurableSlurmEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    handle: SlurmBatchHandle | None
    request: EvaluationRequest
    wait_deadline_epoch_s: float | None = None
    # Legacy records always contained an accepted handle. Missing dispatch
    # evidence is conservative: never authorize a replay from its absence.
    dispatch_started: bool = True
    submission_rejection: str | None = None


class SlurmEvaluationExecutor:
    """Run ordered stages in one recoverable Slurm allocation."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-930041 [PLR0913]; these arguments are independent injected ports or policy facts; grouping them in a DTO would add a shallow mutable carrier and obscure ownership.
        self,
        config: SlurmConfig,
        *,
        workspace: Path,
        setup_script: str | None,
        service: SlurmService | None,
        support_trees: Mapping[str, Path],
        handle_root: Path,
        supported_evidence_kinds: tuple[str, ...] = ("accuracy", "benchmark"),
        admission: SharedSlurmAdmission | None = None,
        cluster: Cluster | None = None,
        pause: Callable[[float], None] | None = None,
        deadline_clock: Callable[[], float] = time.time,
        cancel_confirmation_seconds: float = _DEFAULT_CANCEL_CONFIRMATION_SECONDS,
    ) -> None:
        """Bind scheduler policy, durable intent, and the cluster interface."""
        self._cluster = (
            cluster
            if cluster is not None
            else make_cluster(config, state_root=handle_root / "cluster")
        )
        self._pause = pause
        self._poll_interval = config.poll_interval_seconds
        # Confirmation is bounded by an inspection count so that it follows the
        # same pacing as the wait loop (and an injected pause), not the wall clock.
        self._cancel_confirmations = max(
            1, math.ceil(cancel_confirmation_seconds / config.poll_interval_seconds)
        )
        self._deadline_clock = deadline_clock
        self._job_timeout_seconds = config.job_timeout_seconds
        self._workspace = workspace
        self._setup_script = setup_script
        self._service = service
        self._support_trees = dict(support_trees)
        self._handle_root = handle_root
        self._handle_root.mkdir(parents=True, exist_ok=True)
        self._supported_evidence_kinds = supported_evidence_kinds
        self._admission = admission or SharedSlurmAdmission(1)
        self._handles: dict[str, SlurmBatchHandle] = {}
        self._observations: dict[str, ExecutorObservation] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Only observed termination suppresses redundant cancellation. A sent
        # scancel request does not prove that the allocation has stopped.
        self._terminated_jobs: set[str] = set()
        self._changes: dict[str, asyncio.Event] = {}

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        """Report normalized process-local capacity without probing credentials."""
        del requirements
        active, queued = await self._admission.counts()
        return AvailabilitySnapshot(
            state=(
                AvailabilityState.BUSY
                if active >= self._admission.capacity
                else AvailabilityState.DELAYED
            ),
            capacity=self._admission.capacity,
            in_flight=active,
            queue_depth=queued,
            reuse_status=ReuseStatus.UNKNOWN,
            cost_class=CostClass.UNKNOWN,
            observed_at=time.monotonic(),
            fresh_for_s=1.0,
            supported_evidence_kinds=self._supported_evidence_kinds,
            supported_capabilities=("ordered_stages", "stop_on_failure", "shared_service"),
        )

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        """Idempotently submit or resume a fused batch under a stable handle."""
        try:
            validate_cluster_operation_id(handle_id)
        except SlurmError as exc:
            raise ExecutorRejectedError(str(exc)) from exc
        if handle_id in self._tasks:
            self._validate_durable_request(handle_id, request, self._read_evaluation(handle_id))
            return
        try:
            stages = self._parse_stages(request)
        except ValueError as exc:
            # Malformed stages are refused before any provider contact, so a
            # retry cannot succeed; report a rejection, not an ambiguous error.
            raise ExecutorRejectedError(str(exc)) from exc
        try:
            durable = self._read_evaluation(handle_id)
            self._validate_durable_request(handle_id, request, durable)
            if durable is not None and durable.submission_rejection is not None:
                self._publish_rejection(handle_id, durable.submission_rejection)
                return
            if durable is None:
                self._write_evaluation(handle_id, None, request, dispatch_started=False)
            self._publish(
                handle_id,
                ExecutorObservation(
                    state=EvaluationState.QUEUED, current_stage=request.stages[0].name
                ),
            )
            self._tasks[handle_id] = asyncio.create_task(
                self._admit_and_execute(handle_id, request, stages, reconcile=durable is not None),
                name=f"vibesys-slurm-{handle_id[-12:]}",
            )
        except Exception as exc:
            raise ExecutorSubmissionError(exc) from exc

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        """Poll known durable work once without resuming collection or cancelling it."""
        validate_cluster_operation_id(handle_id)
        observed = self._observations.get(handle_id)
        if observed is not None and observed.state in {
            EvaluationState.SUCCEEDED,
            EvaluationState.FAILED,
            EvaluationState.CANCELED,
        }:
            return observed
        durable = self._read_evaluation(handle_id)
        if durable is None:
            return None
        if durable.submission_rejection is not None:
            return ExecutorObservation(
                state=EvaluationState.FAILED, failure=durable.submission_rejection
            )
        target = durable.handle if durable.handle is not None else handle_id
        inspected = await asyncio.to_thread(self._cluster.inspect, target)
        status = (
            inspected.status
            if isinstance(inspected, ClusterObservation)
            else SlurmJobStatus.UNKNOWN
        )
        # Scheduler terminality cannot replace collected stage evidence. Normal
        # recovery owns collection; deadline inspection cannot start it or
        # discard partial results by settling prematurely.
        return _lifecycle_observation(status, durable.request)

    async def poll(self, handle_id: str) -> ExecutorPoll:
        """Inspect durable work once: no submission, recovery task or workspace.

        A scheduler-terminal job is collected here, so its terminal observation
        carries every stage result the cluster can still produce, failed stages
        included. Collection is a read of the cluster's terminal evidence.
        """
        validate_cluster_operation_id(handle_id)
        durable = self._read_evaluation(handle_id)
        if durable is None:
            return ExecutorPoll(phase=PollPhase.UNSUBMITTED)
        if durable.submission_rejection is not None:
            return ExecutorPoll(
                phase=PollPhase.ENDED,
                terminal=ExecutorObservation(
                    state=EvaluationState.FAILED, failure=durable.submission_rejection
                ),
            )
        if durable.handle is None and not durable.dispatch_started:
            return ExecutorPoll(phase=PollPhase.QUEUED, detail="awaiting local admission")
        return await self._poll_cluster(handle_id, durable)

    async def _poll_cluster(self, handle_id: str, durable: _DurableSlurmEvaluation) -> ExecutorPoll:
        target = durable.handle if durable.handle is not None else handle_id
        inspected = await asyncio.to_thread(self._cluster.inspect, target)
        if not isinstance(inspected, ClusterObservation):
            return ExecutorPoll(phase=PollPhase.UNKNOWN, detail=inspected.reason)
        view = _scheduler_view(inspected.status)
        match view.phase:
            case PollPhase.QUEUED:
                return ExecutorPoll(
                    phase=PollPhase.QUEUED,
                    pending_reason=inspected.pending_reason,
                    estimated_start=inspected.estimated_start,
                )
            case PollPhase.RUNNING:
                return ExecutorPoll(
                    phase=PollPhase.RUNNING, current_stage=durable.request.stages[0].name
                )
            case PollPhase.ENDED if view.lifecycle is EvaluationState.CANCELED:
                return ExecutorPoll(
                    phase=PollPhase.ENDED,
                    terminal=ExecutorObservation(state=EvaluationState.CANCELED),
                )
            case _:
                return await self._poll_terminal(
                    handle_id, durable, durable.handle or inspected.handle
                )

    async def _poll_terminal(
        self, handle_id: str, durable: _DurableSlurmEvaluation, handle: object
    ) -> ExecutorPoll:
        if not isinstance(handle, SlurmBatchHandle):
            return ExecutorPoll(phase=PollPhase.UNKNOWN, detail="missing batch identity")
        collected = await asyncio.to_thread(self._cluster.collect, handle)
        try:
            terminal = self._collected_observation(handle_id, durable.request, handle, collected)
        except SlurmError as error:
            return ExecutorPoll(phase=PollPhase.UNKNOWN, detail=str(error))
        return ExecutorPoll(phase=PollPhase.ENDED, terminal=terminal)

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        """Recover durable intent and inspect before resuming unfinished work."""
        validate_cluster_operation_id(handle_id)
        observed = self._observations.get(handle_id)
        task = self._tasks.get(handle_id)
        if observed is not None and (
            observed.state
            in {EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED}
            or (task is not None and not task.done())
        ):
            return observed
        durable = self._read_evaluation(handle_id)
        if durable is None:
            return None
        if durable.submission_rejection is not None:
            self._publish_rejection(handle_id, durable.submission_rejection)
            return self._observations[handle_id]
        if durable.handle is not None:
            self._handles[handle_id] = durable.handle
        self._publish(
            handle_id,
            ExecutorObservation(
                state=EvaluationState.RUNNING,
                current_stage=durable.request.stages[0].name,
            ),
        )
        stages = self._parse_stages(durable.request)
        self._tasks[handle_id] = asyncio.create_task(
            self._admit_and_execute(handle_id, durable.request, stages, reconcile=True),
            name=f"vibesys-slurm-{handle_id[-12:]}",
        )
        return self._observations[handle_id]

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        """Wait boundedly for a sticky lifecycle notification."""
        event = self._changes.setdefault(handle_id, asyncio.Event())
        if event.is_set():
            event.clear()
            return
        try:
            async with asyncio.timeout(timeout_s):
                await event.wait()
        except TimeoutError:
            return
        finally:
            event.clear()

    async def cancel(self, handle_id: str) -> None:
        """Cancel an accepted batch, including after process restart."""
        validate_cluster_operation_id(handle_id)
        observed = self._observations.get(handle_id)
        if observed is not None and observed.state in {
            EvaluationState.SUCCEEDED,
            EvaluationState.FAILED,
            EvaluationState.CANCELED,
        }:
            return
        durable = self._read_evaluation(handle_id)
        if durable is not None and durable.submission_rejection is not None:
            self._publish_rejection(handle_id, durable.submission_rejection)
            return
        task = self._tasks.get(handle_id)
        if task is not None and not task.done():
            durable = self._read_evaluation(handle_id)
            handle_known = handle_id in self._handles or (
                durable is not None and durable.dispatch_started
            )
            unsubmitted = not handle_known and durable is not None and not durable.dispatch_started
            if handle_id in self._handles:
                self._require_confirmed(handle_id, await self._cancel_running(handle_id))
            # Drain acceptance before canceling by operation identity. A
            # conflicting payload can prove this executor owns no allocation.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            observation = self._observations.get(handle_id)
            if observation is not None and observation.state is EvaluationState.FAILED:
                return
            if not unsubmitted:
                self._require_confirmed(handle_id, await self._cancel_running(handle_id))
        elif self._read_evaluation(handle_id) is not None:
            self._require_confirmed(handle_id, await self._cancel_running(handle_id))
        elif handle_id not in self._handles:
            raise ExecutorCancellationUnknownError(handle_id)
        self._publish(handle_id, ExecutorObservation(state=EvaluationState.CANCELED))

    async def close(self) -> None:
        """Cancel and drain every background execution owned by this executor."""
        active = tuple(handle_id for handle_id, task in self._tasks.items() if not task.done())
        await asyncio.gather(*(self.cancel(handle_id) for handle_id in active))

    async def _admit_and_execute(
        self,
        handle_id: str,
        request: EvaluationRequest,
        stages: tuple[SlurmStagePayload, ...],
        *,
        reconcile: bool = False,
    ) -> None:
        try:
            async with self._admission.lease(handle_id):
                # Recovery through inspect() already published RUNNING; a later
                # STARTING would regress the lifecycle the coordinator stored.
                published = self._observations.get(handle_id)
                if published is None or published.state is EvaluationState.QUEUED:
                    self._publish(
                        handle_id,
                        ExecutorObservation(
                            state=EvaluationState.STARTING,
                            current_stage=request.stages[0].name,
                        ),
                    )
                await self._accept_cancellation_safe(
                    handle_id, request, stages, reconcile=reconcile
                )
                await self._execute(handle_id, request)
        except asyncio.CancelledError:
            durable = self._read_evaluation(handle_id)
            if (
                durable is not None and not durable.dispatch_started
            ) or await self._cancel_running_best_effort(handle_id):
                self._publish(handle_id, ExecutorObservation(state=EvaluationState.CANCELED))
            raise
        except (SlurmSubmissionRejectedError, ExecutorRejectedError) as exc:
            # Definite nonacceptance disproves ownership of any allocation
            # associated with this identity. Retain that proof across restart.
            self._record_rejection(handle_id, request, str(exc))
            self._publish_rejection(handle_id, str(exc))
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-930042 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
            if not await self._cancel_running_best_effort(handle_id):
                return
            self._publish(
                handle_id,
                ExecutorObservation(
                    state=EvaluationState.FAILED,
                    failure=f"{type(exc).__name__}: {exc}",
                ),
            )

    async def _accept_cancellation_safe(
        self,
        handle_id: str,
        request: EvaluationRequest,
        stages: tuple[SlurmStagePayload, ...],
        *,
        reconcile: bool,
    ) -> None:
        acceptance = asyncio.create_task(
            self._accept(handle_id, request, stages, reconcile=reconcile)
        )
        try:
            await asyncio.shield(acceptance)
        except asyncio.CancelledError:
            try:
                await acceptance
            except (SlurmSubmissionRejectedError, ExecutorRejectedError):
                # Cancellation racing staging cannot erase proof that no job
                # was submitted. Let the lifecycle boundary publish failure.
                raise
            except Exception:  # noqa: BLE001  # lint-waiver: LW-930077 [BLE001]; enumerating runner exceptions would let an extension bypass cleanup; suppressing Exception would also erase definite rejection, so this boundary preserves that proof and drains other failures before reconciliation.
                _LOG.exception("Slurm submission failed while cancellation was pending")
            with contextlib.suppress(Exception):
                await self._cancel_running(handle_id)
            raise

    async def _accept(
        self,
        handle_id: str,
        request: EvaluationRequest,
        stages: tuple[SlurmStagePayload, ...],
        *,
        reconcile: bool,
    ) -> None:
        durable = self._read_evaluation(handle_id)
        if durable is not None and durable.submission_rejection is not None:
            raise ExecutorRejectedError(durable.submission_rejection)
        existing = self._handles.get(handle_id) or (durable.handle if durable is not None else None)
        if existing is not None:
            self._handles[handle_id] = existing
            return
        if reconcile and durable is not None and durable.dispatch_started:
            observed = await asyncio.to_thread(self._cluster.inspect, handle_id)
            if isinstance(observed, ClusterObservation) and isinstance(
                observed.handle, SlurmBatchHandle
            ):
                self._handles[handle_id] = observed.handle
                self._write_evaluation(handle_id, observed.handle, request)
                return
            reason = (
                observed.reason
                if isinstance(observed, ClusterUnknown)
                else "missing batch identity"
            )
            raise SlurmOutcomeUnknownError.observation(handle_id, reason)
        lifecycle = stages[0].target_lifecycle
        batch_stages = tuple(
            SlurmBatchStage(
                name=step.name,
                command=_stage_command(step.name, stage.command),
                timeout_seconds=stage.timeout_seconds,
                tree_artifacts=tuple(
                    SlurmTreeArtifact(remote_path=ref, local_path=self._workspace / ref)
                    for ref in stage.tree_artifact_refs
                ),
            )
            for step, stage in zip(request.stages, stages, strict=True)
        )
        self._write_evaluation(handle_id, None, request, dispatch_started=True)
        submitted = await asyncio.to_thread(
            self._cluster.submit,
            SlurmBatchRequest(
                workspace=self._workspace,
                stages=batch_stages,
                stop_on_failure=request.stop_on_failure,
                setup_script=self._setup_script,
                service=(
                    None if lifecycle is SlurmTargetLifecycle.COMMAND_MANAGED else self._service
                ),
                support_trees=self._support_trees,
            ),
            operation_id=handle_id,
        )
        if isinstance(submitted, ClusterRejected):
            raise SlurmSubmissionRejectedError(submitted.reason)
        if isinstance(submitted, ClusterConflict):
            raise ExecutorRejectedError(str(_SlurmExecutionError.request_conflict(handle_id)))
        if isinstance(submitted, ClusterUnknown):
            observed = await asyncio.to_thread(self._cluster.inspect, handle_id)
            if not isinstance(observed, ClusterObservation) or not isinstance(
                observed.handle, SlurmBatchHandle
            ):
                raise SlurmOutcomeUnknownError.observation(handle_id, submitted.reason)
            handle = observed.handle
        elif isinstance(submitted, ClusterSubmitted) and isinstance(
            submitted.handle, SlurmBatchHandle
        ):
            handle = submitted.handle
        else:
            raise SlurmOutcomeUnknownError.observation(handle_id, "missing batch identity")
        self._handles[handle_id] = handle
        self._write_evaluation(handle_id, handle, request)

    async def _execute(self, handle_id: str, request: EvaluationRequest) -> None:
        self._publish(
            handle_id,
            ExecutorObservation(
                state=EvaluationState.RUNNING,
                current_stage=request.stages[0].name,
            ),
        )
        durable = self._read_evaluation(handle_id)
        handle = self._handles.get(handle_id) or (durable.handle if durable is not None else None)
        if handle is None:
            raise _SlurmExecutionError.not_accepted()
        handle = await self._wait_for_batch(handle_id, handle, request, durable)
        collection = asyncio.create_task(asyncio.to_thread(self._cluster.collect, handle))
        try:
            collected = await asyncio.shield(collection)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await collection
            raise
        self._publish(handle_id, self._collected_observation(handle_id, request, handle, collected))

    @staticmethod
    def _collected_observation(
        handle_id: str,
        request: EvaluationRequest,
        handle: SlurmBatchHandle,
        collected: ClusterCollectOutcome,
    ) -> ExecutorObservation:
        """Terminal observation with every collectable stage result, kept even when failed."""
        batch = _collected_batch(collected, handle.job.job_id)
        metadata = SlurmExecutionMetadata(
            phase_timings_seconds=dict(batch.phase_timings_seconds),
            content_cache_hits=batch.content_cache_hits,
            job_exit_code=batch.job_exit_code,
            collection_failure=batch.collection_failure,
            aggregate_unknown=collected.reason if isinstance(collected, ClusterUnknown) else None,
        )
        by_name = {item.name: item for item in batch.stages}
        results: list[EvaluationStepResult] = []
        for index, step in enumerate(request.stages):
            item = by_name.get(step.name)
            if item is None:
                output = (
                    batch.job_output or f"Slurm batch returned no evidence for stage {step.name!r}"
                )
                raw = SlurmCommandResult(
                    output=output,
                    exit_code=None,
                    executed=False,
                    execution_metadata=metadata if index == 0 else None,
                    collection_failure=output,
                )
                results.append(
                    EvaluationStepResult(
                        name=step.name,
                        state=StageState.FAILED,
                        result=raw.model_dump(mode="json"),
                        failure=output,
                        failure_kind=StageFailureKind.COLLECTION,
                    )
                )
                continue
            if item.skipped and item.collection_failure is None:
                results.append(EvaluationStepResult(name=step.name, state=StageState.SKIPPED))
                continue
            output = item.stdout + item.stderr
            raw = SlurmCommandResult(
                output=output,
                exit_code=item.exit_code,
                stdout=item.stdout,
                stderr=item.stderr,
                executed=not item.skipped,
                execution_metadata=metadata if index == 0 else None,
                collection_failure=item.collection_failure,
            )
            failure = item.collection_failure
            if item.exit_code is None:
                error = SlurmOutcomeUnknownError.missing_exit_code(step.name)
                unknown = f"{type(error).__name__}: {error}"
                failure = unknown if failure is None else f"{unknown}; {failure}"
            elif failure is None and raw.exit_code != 0:
                failure = _stage_failure(step, raw, item.elapsed_seconds, batch.service_log_tail)
            results.append(
                EvaluationStepResult(
                    name=step.name,
                    state=StageState.FAILED if failure is not None else StageState.SUCCEEDED,
                    result=raw.model_dump(mode="json"),
                    failure=failure,
                    failure_kind=(
                        None
                        if failure is None
                        else StageFailureKind.EXECUTION
                        if raw.exit_code not in (None, 0)
                        else StageFailureKind.COLLECTION
                    ),
                    duration_s=item.elapsed_seconds,
                )
            )
        failed = next((item for item in results if item.state is StageState.FAILED), None)
        failure = failed.failure if failed is not None else batch.collection_failure
        if failure is None and isinstance(collected, ClusterUnknown):
            unknown = SlurmOutcomeUnknownError.observation(handle_id, collected.reason)
            failure = f"{type(unknown).__name__}: {unknown}"
        if failure is None and batch.job_exit_code != 0:
            error = (
                SlurmOutcomeUnknownError.observation(handle_id, "missing allocation exit status")
                if batch.job_exit_code is None
                else _SlurmExecutionError.batch_failed(batch.job_id, batch.job_exit_code)
            )
            failure = f"{type(error).__name__}: {error}"
        return ExecutorObservation(
            state=EvaluationState.FAILED if failure is not None else EvaluationState.SUCCEEDED,
            stage_results=tuple(results),
            failure=failure,
        )

    async def _wait_for_batch(
        self,
        handle_id: str,
        handle: SlurmBatchHandle,
        request: EvaluationRequest,
        durable: _DurableSlurmEvaluation | None,
    ) -> SlurmBatchHandle:
        """Persist and enforce the scheduler observation deadline."""
        wait_deadline_epoch_s = durable.wait_deadline_epoch_s if durable is not None else None
        if wait_deadline_epoch_s is None:
            remaining = self._job_timeout_seconds - handle.waited_seconds
            wait_deadline_epoch_s = self._deadline_clock() + remaining
            self._write_evaluation(
                handle_id,
                handle,
                request,
                wait_deadline_epoch_s=wait_deadline_epoch_s,
            )
        else:
            remaining = wait_deadline_epoch_s - self._deadline_clock()
        if remaining <= 0:
            raise _SlurmExecutionError.deadline_exceeded(self._job_timeout_seconds)
        while True:
            observed = await self._inspect_cancellation_safe(handle_id, handle)
            if isinstance(observed, ClusterObservation):
                if observed.status in {
                    SlurmJobStatus.COMPLETED,
                    SlurmJobStatus.FAILED,
                    SlurmJobStatus.CANCELLED,
                }:
                    return handle
                active = _lifecycle_observation(observed.status, request)
                if active is not None:
                    self._publish(handle_id, active)
            remaining = wait_deadline_epoch_s - self._deadline_clock()
            if remaining <= 0:
                raise _SlurmExecutionError.deadline_exceeded(self._job_timeout_seconds)
            await self._pace(min(self._poll_interval, remaining))

    async def _pace(self, interval: float) -> None:
        if self._pause is None:
            await asyncio.sleep(interval)
        else:
            await asyncio.to_thread(self._pause, interval)

    async def _inspect_cancellation_safe(
        self, handle_id: str, handle: SlurmBatchHandle
    ) -> ClusterInspectOutcome:
        inspection = asyncio.create_task(asyncio.to_thread(self._cluster.inspect, handle))
        try:
            return await asyncio.shield(inspection)
        except asyncio.CancelledError:
            await self._cancel_running_best_effort(handle_id)
            with contextlib.suppress(Exception):
                await inspection
            raise

    @staticmethod
    def _parse_stages(request: EvaluationRequest) -> tuple[SlurmStagePayload, ...]:
        # EvaluationStep.payload has already crossed a JSON boundary, so tuples
        # and string enums have their JSON representation here.
        stages = tuple(
            SlurmStagePayload.model_validate(stage.payload, strict=False)
            for stage in request.stages
        )
        for step, stage in zip(request.stages, stages, strict=True):
            _stage_command(step.name, stage.command)
        if len({stage.target_lifecycle for stage in stages}) != 1:
            raise ValueError(  # noqa: TRY003  # lint-waiver: LW-930044 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
                "all fused evaluation stages must use one target lifecycle policy"
            )
        return stages

    @staticmethod
    def _require_confirmed(handle_id: str, outcome: _CancelOutcome) -> None:
        match outcome:
            case _CancelConfirmed():
                return
            case _CancelRequested(job_id=None):
                raise ExecutorCancellationUnknownError(handle_id)
            case _CancelRequested(job_id=str() as job_id):
                raise ExecutorCancellationUnconfirmedError(handle_id, job_id)
            case _:
                assert_never(outcome)

    async def _cancel_running(self, handle_id: str) -> _CancelOutcome:
        """Send scancel, then confirm termination within a bounded wait.

        A job that still reads active right after scancel is "cancel requested",
        not an unknown identity: Slurm keeps a cancelled job in COMPLETING while
        it tears down. Confirmation inspects at the wait loop's pacing for a
        bounded number of inspections.
        """
        durable = self._read_evaluation(handle_id)
        if durable is not None and durable.submission_rejection is not None:
            return _CancelConfirmed()
        handle = self._handles.get(handle_id) or (durable.handle if durable is not None else None)
        target = handle if handle is not None else handle_id
        if handle is not None and handle.job.job_id in self._terminated_jobs:
            return _CancelConfirmed()
        job_id = handle.job.job_id if handle is not None else None
        # Every request sends scancel: it is idempotent, and a retry after an
        # unconfirmed outcome must reach a scheduler that lost the first one.
        cancelled = await _finish_in_thread(self._cluster.cancel, target)
        if isinstance(cancelled, ClusterUnknown):
            raise ExecutorCancellationUnknownError(handle_id)
        if (
            handle is None
            and durable is not None
            and not durable.dispatch_started
            and cancelled.job_id is None
        ):
            # The durable preparation proves this executor never dispatched.
            # The cluster retained cancellation intent and has no accepted job
            # identity, so there is no allocation termination to confirm.
            return _CancelConfirmed()
        job_id = job_id or cancelled.job_id
        for attempt in range(self._cancel_confirmations):
            observed = await _finish_in_thread(self._cluster.inspect, target)
            if isinstance(observed, ClusterObservation) and observed.status in {
                SlurmJobStatus.COMPLETED,
                SlurmJobStatus.FAILED,
                SlurmJobStatus.CANCELLED,
            }:
                self._terminated_jobs.add(observed.job_id)
                return _CancelConfirmed()
            if attempt + 1 < self._cancel_confirmations:
                await self._pace(self._poll_interval)
        return _CancelRequested(job_id)

    async def _cancel_running_best_effort(self, handle_id: str) -> bool:
        """Report confirmed cleanup, retaining nonterminal ownership on failure."""
        try:
            outcome = await self._cancel_running(handle_id)
        except (ExecutorCancellationUnknownError, SlurmError, OSError, subprocess.SubprocessError):
            _LOG.exception("could not cancel the Slurm job of evaluation %s", handle_id)
            return False
        match outcome:
            case _CancelConfirmed():
                return True
            case _CancelRequested():
                _LOG.warning("cancellation of evaluation %s is not yet confirmed", handle_id)
                return False
            case _:
                assert_never(outcome)

    def _publish(self, handle_id: str, observation: ExecutorObservation) -> None:
        held = self._observations.get(handle_id)
        if held is not None and _rank(observation.state) < _rank(held.state):
            # Join by maximum: a later, lower reading is not news.
            return
        self._observations[handle_id] = observation
        self._changes.setdefault(handle_id, asyncio.Event()).set()

    def _record_rejection(self, handle_id: str, request: EvaluationRequest, reason: str) -> None:
        record = _DurableSlurmEvaluation(handle=None, request=request, submission_rejection=reason)
        path = self._handle_root / f"{handle_id}.json"
        atomic_write_bytes(path, (record.model_dump_json() + "\n").encode("utf-8"))

    def _publish_rejection(self, handle_id: str, reason: str) -> None:
        self._publish(handle_id, ExecutorObservation(state=EvaluationState.FAILED, failure=reason))

    def _write_evaluation(
        self,
        handle_id: str,
        handle: SlurmBatchHandle | None,
        request: EvaluationRequest,
        *,
        wait_deadline_epoch_s: float | None = None,
        dispatch_started: bool = True,
    ) -> None:
        path = self._handle_root / f"{handle_id}.json"
        document = (
            _DurableSlurmEvaluation(
                handle=handle,
                request=request,
                wait_deadline_epoch_s=wait_deadline_epoch_s,
                dispatch_started=dispatch_started,
            ).model_dump_json()
            + "\n"
        )
        atomic_write_bytes(path, document.encode("utf-8"))

    def _read_evaluation(self, handle_id: str) -> _DurableSlurmEvaluation | None:
        path = self._handle_root / f"{handle_id}.json"
        if not path.is_file():
            return None
        return _DurableSlurmEvaluation.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def _validate_durable_request(
        handle_id: str,
        request: EvaluationRequest,
        durable: _DurableSlurmEvaluation | None,
    ) -> None:
        if durable is not None and durable.request != request:
            raise _SlurmExecutionError.request_conflict(handle_id)


class _SlurmStagePayloadError(ValueError):
    @classmethod
    def missing_command(cls, name: str) -> _SlurmStagePayloadError:
        return cls(f"Slurm stage {name!r} requires a nonempty configured command")


async def _finish_in_thread[**P, R](
    function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
) -> R:
    """Run a blocking cluster call that must finish even if its awaiter is cancelled.

    ``asyncio.to_thread`` abandons its worker when the awaiting task is cancelled,
    so a cancel or inspect could still be mid-flight (an scancel not yet sent) after
    the executor reported itself closed. This waits for the call to end, then
    re-raises the cancellation.
    """
    work = asyncio.ensure_future(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(work)
        raise


def _stage_command(name: str, command: str | None) -> tuple[str, ...]:
    if command is None or not command.strip():
        raise _SlurmStagePayloadError.missing_command(name)
    return ("bash", "-c", command)


def _collected_batch(collected: ClusterCollectOutcome, job_id: str) -> SlurmBatchResult:
    if isinstance(collected, (ClusterCollected, ClusterUnknown)) and isinstance(
        collected.result, SlurmBatchResult
    ):
        return collected.result
    raise SlurmError.job_not_terminal(job_id)


# GNU timeout's exit status when it stopped the command (the job script wraps
# each stage in ``timeout --kill-after``; 137 is the follow-up SIGKILL).
_TIMEOUT_EXIT_CODES = frozenset({124, 137})


def _stage_failure(
    step: EvaluationStep,
    raw: SlurmCommandResult,
    elapsed_seconds: float | None,
    service_log_tail: str,
) -> str:
    """Return the failure text for a nonzero stage, never empty.

    A stage that is killed by its timeout often prints nothing, and an empty
    failure would make the whole observation invalid and hide the cause. The
    shared server's log tail is appended because a slow or hung server is the
    usual cause of a failed client stage, and the agent cannot read that log.
    """
    if raw.output.strip():
        failure = raw.output
    else:
        elapsed = "" if elapsed_seconds is None else f" after {elapsed_seconds:.0f} s"
        if raw.exit_code in _TIMEOUT_EXIT_CODES:
            failure = f"stage {step.name!r} hit its time limit{elapsed} and printed no output"
        else:
            failure = (
                f"stage {step.name!r} exited with code {raw.exit_code}{elapsed}"
                " and printed no output"
            )
    if not service_log_tail.strip():
        return failure
    return f"{failure.rstrip()}\n--- server log tail (repeated lines collapsed) ---\n{service_log_tail}"


class _SlurmExecutionError(RuntimeError):
    @classmethod
    def batch_failed(cls, job_id: str, exit_code: int) -> _SlurmExecutionError:
        return cls(f"Slurm batch {job_id!r} exited with code {exit_code}")

    @classmethod
    def request_conflict(cls, handle_id: str) -> _SlurmExecutionError:
        return cls(f"Slurm evaluation handle {handle_id!r} has a different request")

    @classmethod
    def not_accepted(cls) -> _SlurmExecutionError:
        return cls("Slurm evaluation was not durably accepted before execution")

    @classmethod
    def deadline_exceeded(cls, timeout_seconds: int) -> _SlurmExecutionError:
        return cls(f"Slurm evaluation exceeded its {timeout_seconds}-second deadline")
