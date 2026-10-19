"""Recoverable fused evaluation execution through Slurm."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import math
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, assert_never

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vs_evaluation.api import (
    STAGE_OUTPUT_TAIL_CHARS,
    AvailabilitySnapshot,
    AvailabilityState,
    CostClass,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    ExecutorCancellationUnknownError,
    ExecutorObservation,
    ExecutorPoll,
    ExecutorRejectedError,
    ExecutorSubmissionError,
    LifecyclePublisher,
    PollPhase,
    ResourceRequirements,
    ReuseStatus,
    StageFailureKind,
    StageState,
    is_finished,
)
from vs_project.api import atomic_write_bytes
from vs_sandbox.slurm_deadlines import (
    DeadlineLimits,
    DeadlinePhase,
    WaitDeadlines,
    first_deadlines,
)
from vs_sandbox.slurm_deadlines import observe as observe_deadlines
from vs_sandbox.slurm_ledger_evaluations import (
    CLOSE_RELEASE_BOUND_S,
    LedgerEvaluations,
    Released,
)
from vs_sandbox.slurm_stage_output import CollectedStage, StageOutputFollower
from vs_sandbox.slurm_wiring import make_cluster
from vs_slurm.api import (
    INITIAL_LIFE,
    SERVICE_NOT_READY_EXIT_CODE,
    STAGE_TIMEOUT_EXIT_CODES,
    ClusterCollected,
    ClusterConflict,
    ClusterObservation,
    ClusterRejected,
    ClusterSubmitted,
    ClusterUnknown,
    Completing,
    Ended,
    EndOutcome,
    JobLife,
    LedgerLabels,
    Queued,
    Running,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStage,
    SlurmError,
    SlurmJobStatus,
    SlurmSubmissionRejectedError,
    SlurmTreeArtifact,
    SlurmWorkspaceRootProbeError,
    Unknown,
    advance_with_notice,
    recorded_job_ids,
    validate_cluster_operation_id,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from pathlib import Path

    from vs_sandbox.slurm_ledger import Attached, SlurmLedgerAttachment
    from vs_sandbox.slurm_stage_output import SlurmStageOutputOpener
    from vs_sim.api import Clock, Processes, Sleeper
    from vs_slurm.api import (
        Cluster,
        ClusterCollectOutcome,
        ClusterInspectOutcome,
        SlurmConfig,
        SlurmService,
        WorkspaceRootProof,
    )

_UNRESOLVED_JOB_ID = "0"  # the placeholder id of a submission whose acceptance is unresolved
_CLUSTER_STATE = "cluster"  # under the handle root: the cluster's operation records
_LOG = logging.getLogger(__name__)


_ENDED_BEFORE_DISPATCH = ExecutorObservation(state=EvaluationState.CANCELED)


@dataclass(frozen=True)
class _SchedulerView:
    """What one ordered scheduler phase means for the lifecycle and for a pure poll.

    This is the only place that interprets a scheduler phase. ``lifecycle`` is
    None when the phase carries no lifecycle information (collection owns the
    terminal result, or the phase is unknown). ``staged`` says whether a workload
    stage is known to be running.
    """

    lifecycle: EvaluationState | None
    phase: PollPhase
    staged: bool = False


@dataclass(frozen=True)
class _SchedulerProjection:
    """The lifecycle and poll facts derived from one scheduler observation."""

    view: _SchedulerView
    life: JobLife
    lifecycle: ExecutorObservation | None
    active_poll: ExecutorPoll | None


def _scheduler_view(life: JobLife, *, canceling: bool = False) -> _SchedulerView:
    """The one mapping from an ordered job life to what the evaluation reports.

    ``canceling`` is true once this executor has sent scancel for the job: a
    job that still computes or tears down is then CANCELING, not RUNNING.
    """
    active = EvaluationState.CANCELING if canceling else EvaluationState.RUNNING
    match life:
        case Queued():
            # An accepted job still queued in the scheduler is an active
            # evaluation. Queueing is a poll-phase fact, never a lifecycle
            # regression to local-admission QUEUED, and no stage is running yet.
            return _SchedulerView(active, PollPhase.QUEUED)
        case Running():
            return _SchedulerView(active, PollPhase.RUNNING, staged=not canceling)
        case Completing():
            # The job script has exited (or been killed); the node is tearing down.
            return _SchedulerView(active, PollPhase.RUNNING)
        case Ended(outcome=EndOutcome.CANCELLED):
            return _SchedulerView(EvaluationState.CANCELED, PollPhase.ENDED)
        case Ended():
            return _SchedulerView(None, PollPhase.ENDED)
        case Unknown():
            return _SchedulerView(None, PollPhase.UNKNOWN)
        case _:
            assert_never(life)


@dataclass(frozen=True)
class _CancelConfirmed:
    """The job ended, was never dispatched, or stayed absent after its tombstone."""


@dataclass(frozen=True)
class _CancelRequested:
    """Cancellation was sent to a known job that has not been seen ending."""

    job_id: str | None


_CancelOutcome = _CancelConfirmed | _CancelRequested

# Real clusters keep a cancelled job in COMPLETING for tens of seconds.
_DEFAULT_CANCEL_CONFIRMATION_SECONDS = 120.0
# A no-job reading already combines the active queue and accounting history.
# Three successful follow-ups, separated by the scheduler polling interval, are
# enough to distinguish stable nonacceptance from a just-published identity.
# This is intentionally independent of the much longer allocation teardown
# budget: there is no known allocation whose termination needs waiting for.
_SUBMISSION_ABSENCE_CONFIRMATIONS = 3
# How long an evaluation keeps collecting an ended batch whose collection read no
# result (a download cut short, a scheduler that did not answer), one try per poll
# interval. Its result files may be whole; past this, the evaluation ends with an
# unknown outcome, and its ledger attempt stays collectable for the next ask.
_COLLECTION_RETRY_SECONDS = 120.0
# How many times an evaluation submits its ledger request again after its job
# was withdrawn or cancelled without this run asking (an operator's removal, a
# sweep after the run's lease lapsed); past this, it fails.
_LEDGER_RESUBMITS = 3


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
    # The shared server exited or never answered its readiness probe, so no stage ran;
    # its log tail is the stage output.
    service_not_ready: bool = False
    # The compute node the allocation ran on, when the job recorded it.
    node: str | None = None


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


class _DurableEnd(StrEnum):
    """How an evaluation ended without a scheduler job."""

    CANCELLED_BEFORE_DISPATCH = "cancelled_before_dispatch"
    # Dispatch started, but its sbatch never reached the scheduler: cancellation
    # acknowledgements with no job, repeated, proved no job exists.
    CANCELLED_WITHOUT_JOB = "cancelled_without_job"
    # A ledger evaluation this run let go of (#2757): its job belongs to the
    # evaluation ledger, which keeps it for other runs or cancels it once no run
    # has leased it for the grace period. This run reports it cancelled.
    RELEASED = "released"


class _DurableSlurmEvaluation(BaseModel):
    """The saved record: the only lifecycle state that survives a restart.

    Every read path (poll, inspect, recovery, cancel) decides from this record, so a
    fact that must outlive the process is written here before it is published.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    handle: SlurmBatchHandle | None
    request: EvaluationRequest
    # The deadlines of the job's current scheduler attempt (``slurm_deadlines``):
    # ``wait_deadline_epoch_s`` bounds its queue wait (None: unbounded),
    # ``run_deadline_epoch_s`` its run from when it was first seen running.
    # ``deadline_attempt`` is None until the deadlines were first saved; a record
    # from before the split has only ``wait_deadline_epoch_s``, read as attempt 0.
    wait_deadline_epoch_s: float | None = None
    run_deadline_epoch_s: float | None = None
    deadline_attempt: int | None = None
    # Legacy records always contained an accepted handle. Missing dispatch
    # evidence is conservative: never authorize a replay from its absence.
    dispatch_started: bool = True
    submission_rejection: str | None = None
    # A provider failure is final only after cleanup proves no allocation remains.
    # Persist it before process-local publication so polling-only recovery sees the
    # same terminal result instead of reconciling an already-closed dispatch forever.
    terminal_failure: str | None = None
    # Set once the evaluation was cancelled while it waited for local admission.
    # Such a record is final: nothing may dispatch it.
    ended: _DurableEnd | None = None
    # The evaluation ledger key of the request this evaluation attached to, when
    # the operator enabled the ledger. The workspace changes once artifacts land,
    # so the key is saved, not recomputed.
    ledger_key: str | None = None

    @model_validator(mode="after")
    def _ended_before_dispatch_has_no_job(self) -> _DurableSlurmEvaluation:
        if self.ended is _DurableEnd.RELEASED and self.ledger_key is None:
            message = "only a ledger evaluation can be released"
            raise ValueError(message)
        if self.ended not in {None, _DurableEnd.RELEASED} and self.handle is not None:
            message = "an evaluation that ended without a job cannot have a job"
            raise ValueError(message)
        if self.ended is _DurableEnd.CANCELLED_BEFORE_DISPATCH and self.dispatch_started:
            message = "an evaluation ended before dispatch cannot have a dispatch"
            raise ValueError(message)
        terminal_facts = sum(
            value is not None
            for value in (self.submission_rejection, self.terminal_failure, self.ended)
        )
        if terminal_facts > 1:
            message = "an evaluation cannot have multiple durable terminal outcomes"
            raise ValueError(message)
        return self


def evaluation_job_ids(handle_root: Path) -> frozenset[str]:
    """The scheduler job ids of the evaluations an executor over ``handle_root`` submitted.

    Reads the records the executor's own cluster keeps, so it never names another run's
    job; it only reads them, so it is safe beside a running executor.
    """
    return recorded_job_ids(handle_root / _CLUSTER_STATE)


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
        processes: Processes | None = None,
        clock: Clock,
        sleeper: Sleeper,
        cancel_confirmation_seconds: float = _DEFAULT_CANCEL_CONFIRMATION_SECONDS,
        ledger: SlurmLedgerAttachment | None = None,
        ledger_candidate: str | None = None,
        stage_output: SlurmStageOutputOpener | None = None,
        root_proof: WorkspaceRootProof | None = None,
    ) -> None:
        """Bind scheduler policy, durable intent, and the cluster interface.

        With ``ledger``, jobs are submitted through the project's evaluation ledger
        (#2757): a request the ledger already holds attaches to its job, and ``cancel``
        and ``close`` cancel a job no other run wants (``_release``); when the ledger
        policy keeps jobs on a stop, ``close`` detaches and ``cancel`` only releases
        this run's lease.
        ``ledger_candidate`` labels the ledger entries for people reading them.

        ``clock`` times the job deadline and availability readings. The deadline is
        persisted, so a production clock is :class:`vs_sim.api.SystemClock`.
        ``sleeper`` paces the polls of a running job and of a cancel's confirmation. The
        executor uses ``cluster``, or reaches the configured cluster by starting ``processes``.
        ``stage_output`` opens one stream per stage, which the stage's output reaches while
        the job runs, read at each scheduler poll (``StageOutputFollower``); without it the
        output reaches only the collected result.
        ``root_proof`` is the run's workspace-root probe: an evaluation's first submission
        waits, at the poll interval, until it proves the root, and a failed proof refuses
        the submission with the probe's error, so no job is ever sent to an unproven root.
        """
        if cluster is None:
            if processes is None:
                message = "a Slurm executor needs a cluster or the processes that reach one"
                raise ValueError(message)
            cluster = make_cluster(
                config, state_root=handle_root / _CLUSTER_STATE, processes=processes, clock=clock
            )
        self._cluster = cluster
        self._stage_output = stage_output
        self._root_proof = root_proof
        self._sleeper = sleeper
        self._poll_interval = config.poll_interval_seconds
        # Confirmation is bounded by an inspection count so that it follows the
        # same pacing as the wait loop (``sleeper``), not the wall clock.
        self._cancel_confirmations = max(
            1, math.ceil(cancel_confirmation_seconds / config.poll_interval_seconds)
        )
        self._collection_tries = 1 + math.ceil(
            _COLLECTION_RETRY_SECONDS / config.poll_interval_seconds
        )
        self._clock = clock
        self._job_timeout_seconds = config.job_timeout_seconds
        self._queue_timeout_seconds = config.queue_timeout_seconds
        self._deadline_limits = DeadlineLimits(
            queue_seconds=config.queue_timeout_seconds, run_seconds=config.job_timeout_seconds
        )
        self._workspace = workspace
        self._setup_script = setup_script
        self._service = service
        self._support_trees = dict(support_trees)
        self._handle_root = handle_root
        self._handle_root.mkdir(parents=True, exist_ok=True)
        self._supported_evidence_kinds = supported_evidence_kinds
        self._admission = admission or SharedSlurmAdmission(1)
        self._handles: dict[str, SlurmBatchHandle] = {}
        self._lifecycle = LifecyclePublisher()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._calls: set[asyncio.Future[Any]] = set()
        # Only observed termination suppresses redundant cancellation. A sent
        # scancel request does not prove that the allocation has stopped.
        self._terminated_jobs: set[str] = set()
        # One order per evaluation over every scheduler reading, whichever path made it.
        self._lives: dict[str, JobLife] = {}
        # Evaluations for which scancel was acknowledged, and the scancel-sent job ids.
        self._canceling: set[str] = set()
        self._cancel_sent: set[str] = set()
        self._confirmations_left: dict[str, int] = {}
        # Highest stage index reported per evaluation: a running job's stage never goes back.
        self._stage_floor: dict[str, int] = {}
        # A read-only poll that sees remote terminality wakes the live owner so
        # that owner, rather than the observer, settles streams and ledger state.
        self._poll_wakeups: dict[str, asyncio.Event] = {}
        self._ledger = (
            None
            if ledger is None
            else LedgerEvaluations(
                ledger,
                cluster,
                clock=clock,
                sleeper=sleeper,
                poll_interval=self._poll_interval,
                job_timeout_seconds=self._job_timeout_seconds,
                in_thread=self._in_thread,
                finish_in_thread=self._finish_in_thread,
            )
        )
        self._ledger_candidate = ledger_candidate
        # Set by ``close``: no submission is taken after it.
        self._closing = False
        # Set by ``close`` when the ledger keeps jobs on a stop: they are left to it.
        self._detaching = False
        # Evaluations this executor was asked to cancel: their jobs' ends are asked for.
        self._cancel_asked: set[str] = set()

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
            observed_at=self._clock.now(),
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
        if self._closing:
            # Refused before anything is written or sent; ambiguous, not rejected, so
            # a later run under the same handle may still submit it.
            raise ExecutorSubmissionError(_SlurmExecutionError.closing(handle_id))
        try:
            stages = self._parse_stages(request)
        except ValueError as exc:
            # Malformed stages are refused before any provider contact, so a
            # retry cannot succeed; report a rejection, not an ambiguous error.
            raise ExecutorRejectedError(str(exc)) from exc
        try:
            durable = self._read_evaluation(handle_id)
            self._validate_durable_request(handle_id, request, durable)
            if durable is not None and self._publish_durable_end(handle_id, durable) is not None:
                return
            if durable is None:
                self._write_evaluation(handle_id, None, request, dispatch_started=False)
            self._resume_use(handle_id, durable)
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
        observed = self._lifecycle.observation(handle_id)
        if observed is not None:
            task = self._tasks.get(handle_id)
            if is_finished(observed.state) or (task is not None and not task.done()):
                # A live owner alone may publish terminality after settling its
                # streams and ledger. ``poll`` owns detailed live scheduler
                # projections; this lifecycle read stays at the safe publication.
                return observed
        durable = self._read_evaluation(handle_id)
        if durable is None:
            return None
        ended = self._publish_durable_end(handle_id, durable)
        if ended is not None:
            return ended
        target = durable.handle if durable.handle is not None else handle_id
        inspected = await self._in_thread(self._cluster.inspect, target)
        if not isinstance(inspected, ClusterObservation):
            return None
        # Scheduler terminality cannot replace collected stage evidence. Normal
        # recovery owns collection; deadline inspection cannot start it or
        # discard partial results by settling prematurely.
        observation = self._lifecycle_observation(handle_id, inspected, durable.request)
        if observation is None:
            return None
        # Join with what inspect() reports, so a later inspect() or the
        # background task's own publication can never read lower than this.
        return self._publish(handle_id, observation)

    async def poll(self, handle_id: str) -> ExecutorPoll:
        """Inspect durable work once: no submission, recovery task or workspace.

        A scheduler-terminal job is collected here, so its terminal observation
        carries every stage result the cluster can still produce, failed stages
        included. Collection is a read of the cluster's terminal evidence. When
        this executor has a live owner task, its published lifecycle is the poll:
        that task alone may expose terminality after its local output and ledger
        settlement has finished.
        """
        validate_cluster_operation_id(handle_id)
        durable = self._read_evaluation(handle_id)
        if durable is None:
            return ExecutorPoll(phase=PollPhase.UNSUBMITTED)
        owned = await self._owned_poll(handle_id, durable)
        if owned is not None:
            return owned
        return await self._poll_durable(handle_id, durable)

    async def _owned_poll(
        self, handle_id: str, durable: _DurableSlurmEvaluation
    ) -> ExecutorPoll | None:
        """Return the owner's last safe poll, or defer an ownerless handle to durability.

        Task completion is the settlement fence. A live task may owe stream and ledger
        cleanup after an observer sees remote terminality, so no observer exposes that
        terminality. Once the task ends, its published terminal is authoritative and
        needs no second cluster collection.
        """
        task = self._tasks.get(handle_id)
        observed = self._lifecycle.observation(handle_id)
        if task is None or observed is None:
            return None
        if task.done():
            return self._completed_owner_poll(handle_id, observed)
        return await self._live_owner_poll(handle_id, durable, task, observed)

    async def _live_owner_poll(
        self,
        handle_id: str,
        durable: _DurableSlurmEvaluation,
        task: asyncio.Task[None],
        observed: ExecutorObservation,
    ) -> ExecutorPoll | None:
        """Inspect a live owner without allowing its observer to settle terminal evidence."""
        if is_finished(observed.state):
            return await self._settled_owner_poll(handle_id, task)
        if durable.handle is None and not durable.dispatch_started:
            return ExecutorPoll(phase=PollPhase.QUEUED, detail="awaiting local admission")
        if not await self._ledger_heartbeat(handle_id, durable):
            if self._asked(handle_id):
                return await self._settled_owner_poll(handle_id, task)
            self._wake_owner(handle_id)  # it submits the request again
            return ExecutorPoll(phase=PollPhase.QUEUED, detail="its ledger job was withdrawn")
        target = durable.handle if durable.handle is not None else handle_id
        inspected = await self._in_thread(self._cluster.inspect, target)
        current = self._lifecycle.observation(handle_id)
        if task.done():
            return self._completed_owner_poll(handle_id, current)
        return await self._live_inspection_poll(handle_id, durable.request, inspected, task)

    async def _live_inspection_poll(
        self,
        handle_id: str,
        request: EvaluationRequest,
        inspected: ClusterInspectOutcome,
        task: asyncio.Task[None],
    ) -> ExecutorPoll | None:
        """Project one live inspection, waking its owner instead of collecting an end."""
        if not isinstance(inspected, ClusterObservation):
            return ExecutorPoll(phase=PollPhase.UNKNOWN, detail=inspected.reason)
        projected = self._scheduler_projection(handle_id, inspected, request)
        if projected.active_poll is not None:
            return projected.active_poll
        if projected.view.phase is PollPhase.ENDED:
            return await self._settled_owner_poll(handle_id, task)
        return ExecutorPoll(phase=PollPhase.UNKNOWN, detail="scheduler state is unknown")

    async def _settled_owner_poll(
        self, handle_id: str, task: asyncio.Task[None]
    ) -> ExecutorPoll | None:
        """Wake and join the live owner before exposing its terminal publication.

        ``asyncio.wait`` joins without raising the owner's outcome and without cancelling
        it, so canceling a read-only poll cannot cancel the task that owns stream and
        ledger settlement, and an owner that was cancelled or failed is not the poll's
        own error: the poll reports what the owner published.  If the owner ends without
        a terminal publication, returning ``None`` lets the durable observer reconcile it.
        """
        self._wake_owner(handle_id)
        await asyncio.wait({task})
        return self._completed_owner_poll(handle_id, self._lifecycle.observation(handle_id))

    def _completed_owner_poll(
        self, handle_id: str, observed: ExecutorObservation | None
    ) -> ExecutorPoll | None:
        """Return a settled owner's terminal, or defer its unresolved durability."""
        if observed is None or not is_finished(observed.state):
            return None
        return ExecutorPoll(
            phase=PollPhase.ENDED,
            attempt=self._lives.get(handle_id, INITIAL_LIFE).attempt,
            terminal=observed,
        )

    def _wake_owner(self, handle_id: str) -> None:
        """Wake a live owner's scheduler pause after an observer sees terminality."""
        self._poll_wakeups.setdefault(handle_id, asyncio.Event()).set()

    async def _poll_durable(self, handle_id: str, durable: _DurableSlurmEvaluation) -> ExecutorPoll:
        """Read one handle with no live process-local owner from durable and cluster evidence."""
        if durable.submission_rejection is not None:
            return ExecutorPoll(
                phase=PollPhase.ENDED,
                terminal=ExecutorObservation(
                    state=EvaluationState.FAILED, failure=durable.submission_rejection
                ),
            )
        if durable.terminal_failure is not None:
            return ExecutorPoll(
                phase=PollPhase.ENDED,
                terminal=ExecutorObservation(
                    state=EvaluationState.FAILED, failure=durable.terminal_failure
                ),
            )
        if durable.ended is not None:
            return ExecutorPoll(phase=PollPhase.ENDED, terminal=_ENDED_BEFORE_DISPATCH)
        if durable.handle is None and not durable.dispatch_started:
            return ExecutorPoll(phase=PollPhase.QUEUED, detail="awaiting local admission")
        return await self._poll_cluster(handle_id, durable)

    async def _poll_cluster(self, handle_id: str, durable: _DurableSlurmEvaluation) -> ExecutorPoll:
        if not await self._ledger_heartbeat(handle_id, durable):
            # With no live owner, this observer owes no local stream settlement.
            # Save the withdrawal before exposing it so restart sees the same end.
            self._end_cancelled(handle_id)
            return ExecutorPoll(phase=PollPhase.ENDED, terminal=_ENDED_BEFORE_DISPATCH)
        target = durable.handle if durable.handle is not None else handle_id
        inspected = await self._in_thread(self._cluster.inspect, target)
        if not isinstance(inspected, ClusterObservation):
            return ExecutorPoll(phase=PollPhase.UNKNOWN, detail=inspected.reason)
        projected = self._scheduler_projection(handle_id, inspected, durable.request)
        if projected.active_poll is not None:
            return projected.active_poll
        match projected.view.phase:
            case PollPhase.ENDED if projected.view.lifecycle is EvaluationState.CANCELED:
                return ExecutorPoll(
                    phase=PollPhase.ENDED,
                    attempt=projected.life.attempt,
                    terminal=ExecutorObservation(state=EvaluationState.CANCELED),
                )
            case _:
                return await self._poll_terminal(
                    handle_id,
                    durable,
                    durable.handle or inspected.handle,
                    inspected,
                    attempt=projected.life.attempt,
                )

    async def _poll_terminal(
        self,
        handle_id: str,
        durable: _DurableSlurmEvaluation,
        handle: object,
        inspected: ClusterObservation,
        *,
        attempt: int,
    ) -> ExecutorPoll:
        if not isinstance(handle, SlurmBatchHandle):
            return ExecutorPoll(phase=PollPhase.UNKNOWN, detail="missing batch identity")
        # The poll just read this job's terminal state; collecting reuses that reading.
        collected = await self._in_thread(
            functools.partial(self._cluster.collect, handle, observed=inspected)
        )
        await self._ledger_settle(handle_id, durable, inspected.status, collected)
        if isinstance(collected, ClusterUnknown) and collected.result is None:
            # Nothing was read (a download cut short): the next poll collects again.
            return ExecutorPoll(phase=PollPhase.UNKNOWN, detail=collected.reason)
        try:
            terminal = self._collected_observation(handle_id, durable.request, handle, collected)
        except SlurmError as error:
            return ExecutorPoll(phase=PollPhase.UNKNOWN, detail=str(error))
        return ExecutorPoll(phase=PollPhase.ENDED, attempt=attempt, terminal=terminal)

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        """Recover durable intent and inspect before resuming unfinished work."""
        validate_cluster_operation_id(handle_id)
        observed = self._lifecycle.observation(handle_id)
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
        ended = self._publish_durable_end(handle_id, durable)
        if ended is not None:
            return ended
        if durable.handle is not None:
            self._handles[handle_id] = durable.handle
        self._resume_use(handle_id, durable)
        published = self._publish(
            handle_id,
            ExecutorObservation(
                state=EvaluationState.RUNNING,
                current_stage=self._reported_stage(handle_id, durable.request),
            ),
        )
        stages = self._parse_stages(durable.request)
        self._tasks[handle_id] = asyncio.create_task(
            self._admit_and_execute(handle_id, durable.request, stages, reconcile=True),
            name=f"vibesys-slurm-{handle_id[-12:]}",
        )
        return published

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        """Wait boundedly for a sticky lifecycle notification."""
        await self._lifecycle.wait_for_change(handle_id, timeout_s)

    async def cancel(self, handle_id: str) -> None:
        """Cancel an accepted batch, including after process restart."""
        validate_cluster_operation_id(handle_id)
        observed = self._lifecycle.observation(handle_id)
        if observed is not None and is_finished(observed.state):
            return
        durable = self._read_evaluation(handle_id)
        if durable is not None and self._publish_durable_end(handle_id, durable) is not None:
            return
        self._cancel_asked.add(handle_id)
        outcome: _CancelOutcome = _CancelConfirmed()
        task = self._tasks.get(handle_id)
        if task is not None and not task.done():
            durable = self._read_evaluation(handle_id)
            handle_known = handle_id in self._handles or (
                durable is not None and durable.dispatch_started
            )
            unsubmitted = not handle_known and durable is not None and not durable.dispatch_started
            first = handle_id in self._handles
            if first:
                outcome = await self._cancel_running(handle_id, resend=True)
            # Drain acceptance before canceling by operation identity. A
            # conflicting payload can prove this executor owns no allocation.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            observation = self._lifecycle.observation(handle_id)
            if observation is not None and observation.state is EvaluationState.FAILED:
                return
            if not unsubmitted:
                outcome = await self._cancel_running(handle_id, resend=not first)
        elif self._read_evaluation(handle_id) is not None:
            outcome = await self._cancel_running(handle_id, resend=True)
        elif handle_id not in self._handles:
            raise ExecutorCancellationUnknownError(handle_id)
        if isinstance(outcome, _CancelConfirmed):
            self._end_cancelled(handle_id)
        # Otherwise the evaluation is CANCELING: the job is known and scancel was
        # acknowledged, so a later inspect or cancel reconciles it to CANCELED.

    async def close(self) -> None:
        """Refuse new submissions, then cancel and drain every execution this executor owns.

        With the evaluation ledger, a cancel withdraws a request no other run wants
        (``_release``). When the ledger keeps jobs on a stop, closing detaches
        instead: every job keeps running, its record keeps it resumable, and its
        lease lapses. A close starts no ledger relinquish after
        ``CLOSE_RELEASE_BOUND_S`` (a controller that does not answer) and detaches
        those jobs the same way; work already under way still finishes, so this
        bounds when releases start, not how long the close takes.
        """
        self._closing = True
        if self._ledger is not None and self._ledger.ledger.policy.keep_jobs_on_stop:
            self._detaching = True
            await self._drain()
            return
        active = tuple(handle_id for handle_id, task in self._tasks.items() if not task.done())
        # A cancel whose job is still tearing down returns with the evaluation
        # CANCELING; the durable record keeps the job for a later reconcile.
        if self._ledger is not None:
            self._ledger.give_up_releasing_after(CLOSE_RELEASE_BOUND_S)
        results = await asyncio.gather(
            *(self.cancel(handle_id) for handle_id in active), return_exceptions=True
        )
        # A cancel of an evaluation that already ended durably returns without touching
        # its task, so a task still waiting for admission would outlive the executor.
        await self._drain()
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _drain(self) -> None:
        """Cancel every background task and wait for the cluster calls they left running."""
        leftover = tuple(task for task in self._tasks.values() if not task.done())
        for task in leftover:
            task.cancel()
        await asyncio.gather(*leftover, return_exceptions=True)
        # A cluster call whose caller was cancelled still runs in its thread. Wait
        # only for calls still running: a call that has ended stays in ``_calls``
        # until its done callback runs on a later loop turn, and gathering it
        # returns without yielding (Python 3.14), so waiting on it would spin the
        # loop and starve that very callback.
        while pending := tuple(call for call in self._calls if not call.done()):
            await asyncio.gather(*pending, return_exceptions=True)

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
                published = self._lifecycle.observation(handle_id)
                if published is None or published.state is EvaluationState.QUEUED:
                    self._publish(
                        handle_id,
                        ExecutorObservation(
                            state=EvaluationState.STARTING,
                            current_stage=request.stages[0].name,
                        ),
                    )
                await self._accept_and_execute(handle_id, request, stages, reconcile=reconcile)
        except asyncio.CancelledError:
            if self._detaching:
                raise  # the saved record resumes it; the job stays with the ledger
            durable = self._read_evaluation(handle_id)
            if durable is not None and not durable.dispatch_started:
                self._end_before_dispatch(handle_id, durable.request)
            elif await self._cancel_running_best_effort(handle_id):
                self._end_cancelled(handle_id)
            raise
        except (SlurmSubmissionRejectedError, ExecutorRejectedError) as exc:
            # Definite nonacceptance disproves ownership of any allocation
            # associated with this identity. Retain that proof across restart.
            self._record_rejection(handle_id, request, str(exc))
            await self._let_go_of_ended(handle_id, None)
            self._publish_rejection(handle_id, str(exc))
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-930042 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
            if not await self._cancel_running_best_effort(handle_id):
                return
            failure = f"{type(exc).__name__}: {exc}"
            self._record_terminal_failure(handle_id, failure)
            self._publish(
                handle_id, ExecutorObservation(state=EvaluationState.FAILED, failure=failure)
            )

    async def _accept_and_execute(
        self,
        handle_id: str,
        request: EvaluationRequest,
        stages: tuple[SlurmStagePayload, ...],
        *,
        reconcile: bool,
    ) -> None:
        """Submit or attach, then wait for and collect the job; again if the ledger took it."""
        await self._await_root_proof(self._read_evaluation(handle_id))
        resubmits = 0
        while True:
            try:
                await self._accept_cancellation_safe(
                    handle_id, request, stages, reconcile=reconcile
                )
                await self._execute(handle_id, request)
            except _LedgerJobGoneError:
                if resubmits >= _LEDGER_RESUBMITS:
                    raise
                resubmits += 1
                _LOG.warning("evaluation %s submits its ledger request again", handle_id)
                # Forget the job, keeping the request's use: the attach takes a new attempt.
                for held in (self._handles, self._lives, self._stage_floor):
                    held.pop(handle_id, None)
                self._write_evaluation(handle_id, None, request, dispatch_started=True)
                continue
            return

    async def _await_root_proof(self, durable: _DurableSlurmEvaluation | None) -> None:
        """Hold a first submission until the run's workspace root is proven; refuse it if not.

        An evaluation whose dispatch already started may have a job on the cluster, so it
        goes on to reconcile that job: refusing it would disown a live allocation.
        """
        proof = self._root_proof
        if proof is None or (durable is not None and durable.dispatch_started):
            return
        try:
            while not proof.check():
                await self._sleeper.sleep(self._poll_interval)
        except SlurmWorkspaceRootProbeError as exc:
            # Nothing was sent: a definite refusal, recorded and published as one.
            raise SlurmSubmissionRejectedError(str(exc)) from exc

    async def _let_go_of_ended(self, handle_id: str, known: str | None) -> None:
        """Stop using the ledger request of an evaluation that ended, before saying it ended.

        A cancel or a failure lets go through ``_cancel_running``; a job that
        ended, or a submission the cluster refused, lets go here.
        """
        if self._ledger is not None and not self._detaching:
            await self._ledger.let_go_of_ended(handle_id, known)

    def _asked(self, handle_id: str) -> bool:
        """Whether this run asked for the evaluation's job to end: a cancel, or the stop."""
        return self._closing or handle_id in self._cancel_asked

    def _resume_use(self, handle_id: str, durable: _DurableSlurmEvaluation | None) -> None:
        """Count a resumed evaluation attached to a ledger request as a user of it."""
        if (
            self._ledger is not None
            and durable is not None
            and durable.handle is not None
            and durable.ledger_key is not None
        ):
            self._ledger.resumed(handle_id, durable.ledger_key)

    async def _accept_cancellation_safe(
        self,
        handle_id: str,
        request: EvaluationRequest,
        stages: tuple[SlurmStagePayload, ...],
        *,
        reconcile: bool,
    ) -> None:
        if self._ledger is not None:
            # Each ledger step is shielded on its own, and the wait between steps is
            # not: a stop never waits for another process's claim to resolve.
            if not self._accepted_before(handle_id, self._read_evaluation(handle_id)):
                await self._attach(self._ledger, handle_id, request, stages)
            return
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
        if self._accepted_before(handle_id, durable):
            return
        await self._dispatch(handle_id, request, stages, durable, reconcile=reconcile)

    def _accepted_before(self, handle_id: str, durable: _DurableSlurmEvaluation | None) -> bool:
        """Whether the evaluation already has its job; raise if it was definitely rejected."""
        if durable is not None and durable.submission_rejection is not None:
            raise ExecutorRejectedError(durable.submission_rejection)
        existing = self._handles.get(handle_id) or (durable.handle if durable is not None else None)
        if existing is None:
            return False
        self._handles[handle_id] = existing
        return True

    async def _dispatch(
        self,
        handle_id: str,
        request: EvaluationRequest,
        stages: tuple[SlurmStagePayload, ...],
        durable: _DurableSlurmEvaluation | None,
        *,
        reconcile: bool,
    ) -> None:
        """Submit under the evaluation's own operation id, or find its job after a restart."""
        if reconcile and durable is not None and durable.dispatch_started:
            observed = await self._in_thread(self._cluster.inspect, handle_id)
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
        self._write_evaluation(handle_id, None, request, dispatch_started=True)
        submitted = await self._in_thread(
            self._cluster.submit, self._batch_request(request, stages), operation_id=handle_id
        )
        if isinstance(submitted, ClusterRejected):
            raise SlurmSubmissionRejectedError(submitted.reason)
        if isinstance(submitted, ClusterConflict):
            raise ExecutorRejectedError(str(_SlurmExecutionError.request_conflict(handle_id)))
        if isinstance(submitted, ClusterUnknown):
            observed = await self._in_thread(self._cluster.inspect, handle_id)
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

    def _batch_request(
        self, request: EvaluationRequest, stages: tuple[SlurmStagePayload, ...]
    ) -> SlurmBatchRequest:
        lifecycle = stages[0].target_lifecycle
        return SlurmBatchRequest(
            workspace=self._workspace,
            stages=tuple(
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
            ),
            stop_on_failure=request.stop_on_failure,
            setup_script=self._setup_script,
            service=None if lifecycle is SlurmTargetLifecycle.COMMAND_MANAGED else self._service,
            support_trees=self._support_trees,
        )

    async def _attach(
        self,
        ledger: LedgerEvaluations,
        handle_id: str,
        request: EvaluationRequest,
        stages: tuple[SlurmStagePayload, ...],
    ) -> None:
        """Attach to the ledger's job for this request (``LedgerEvaluations.attach``)."""
        self._write_evaluation(handle_id, None, request, dispatch_started=True)
        batch = self._batch_request(request, stages)
        labels = LedgerLabels(candidate=self._ledger_candidate, workstream=request.owner_scope)
        attached = await ledger.attach(
            handle_id, batch, labels, functools.partial(self._save_attached, handle_id, request)
        )
        if attached is None:
            raise _SlurmExecutionError.deadline_exceeded(self._job_timeout_seconds)
        self._save_attached(handle_id, request, attached)

    def _save_attached(
        self, handle_id: str, request: EvaluationRequest, attached: Attached
    ) -> None:
        self._handles[handle_id] = attached.handle
        self._save_evaluation(
            handle_id,
            _DurableSlurmEvaluation(
                handle=attached.handle, request=request, ledger_key=attached.key
            ),
        )

    async def _ledger_heartbeat(self, handle_id: str, durable: _DurableSlurmEvaluation) -> bool:
        """Keep a ledger evaluation leased; return False once it was withdrawn."""
        if self._ledger is None or durable.ledger_key is None or durable.handle is None:
            return True
        return await self._ledger.heartbeat(
            handle_id, durable.ledger_key, durable.handle.job.invocation_id
        )

    async def _ledger_settle(
        self,
        handle_id: str,
        durable: _DurableSlurmEvaluation | None,
        status: SlurmJobStatus,
        collected: ClusterCollectOutcome | None,
    ) -> None:
        """Record a ledger job's end (``LedgerEvaluations.settle``)."""
        if (
            self._ledger is None
            or durable is None
            or durable.ledger_key is None
            or durable.handle is None
        ):
            return
        await self._ledger.settle(
            handle_id, durable.ledger_key, durable.handle.job.invocation_id, status, collected
        )

    async def _execute(self, handle_id: str, request: EvaluationRequest) -> None:
        self._publish(
            handle_id,
            ExecutorObservation(
                state=EvaluationState.RUNNING,
                current_stage=self._reported_stage(handle_id, request),
            ),
        )
        durable = self._read_evaluation(handle_id)
        handle = self._handles.get(handle_id) or (durable.handle if durable is not None else None)
        if handle is None:
            raise _SlurmExecutionError.not_accepted()
        follower = self._follower(handle_id, handle, request)
        try:
            status = await self._wait_for_batch(handle_id, handle, request, durable, follower)
            if status is SlurmJobStatus.CANCELLED:
                # A cancelled job ends CANCELED however its allocation exit code reads,
                # as poll() reports it. Collecting would let a recorded "0:0" exit
                # contradict the scheduler and turn a requested stop into a failure.
                follower.abort()
                await self._ledger_settle(handle_id, durable, status, None)
                if durable is not None and durable.ledger_key is not None:
                    self._unless_asked(handle_id)
                self._end_cancelled(handle_id)
                return
            collected = await self._collect_ended(handle_id, handle)
        except BaseException:
            follower.abort()
            raise
        follower.settle(_collected_stages(request, collected))
        await self._ledger_settle(handle_id, durable, status, collected)
        await self._let_go_of_ended(handle_id, durable.ledger_key if durable is not None else None)
        self._publish(handle_id, self._collected_observation(handle_id, request, handle, collected))

    async def _collect_ended(
        self, handle_id: str, handle: SlurmBatchHandle
    ) -> ClusterCollectOutcome:
        """Collect the ended batch, trying again each poll interval while a try reads no result.

        A try that read no result (``ClusterUnknown`` without one: a download cut
        short, a scheduler that did not answer) says nothing about the result
        files, which may be whole. Tries are bounded by ``_COLLECTION_RETRY_SECONDS``;
        when none read a result, ``SlurmOutcomeUnknownError`` ends the evaluation
        with no stage scored, and its ledger attempt is not settled, so the next
        ask collects the same job again. Each try is shielded: a cancel waits for
        the try in flight to finish.
        """
        tries = 0
        while True:
            tries += 1
            collection = asyncio.create_task(self._in_thread(self._cluster.collect, handle))
            try:
                collected = await asyncio.shield(collection)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await collection
                raise
            if not isinstance(collected, ClusterUnknown) or collected.result is not None:
                return collected
            if tries >= self._collection_tries:
                raise SlurmOutcomeUnknownError.observation(handle_id, collected.reason)
            await self._sleeper.sleep(self._poll_interval)

    def _follower(
        self, handle_id: str, handle: SlurmBatchHandle, request: EvaluationRequest
    ) -> StageOutputFollower:
        return StageOutputFollower(
            self._stage_output,
            handle_id,
            tuple(step.name for step in request.stages),
            functools.partial(self._cluster.stage_output, handle),
        )

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
            service_not_ready=batch.job_exit_code == SERVICE_NOT_READY_EXIT_CODE,
            node=batch.node,
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
            service_log_tail = None
            if item.exit_code is None:
                error = SlurmOutcomeUnknownError.missing_exit_code(step.name)
                unknown = f"{type(error).__name__}: {error}"
                failure = unknown if failure is None else f"{unknown}; {failure}"
            elif failure is None and raw.exit_code != 0:
                failure = _stage_failure(step, raw, item.elapsed_seconds)
                service_log_tail = (
                    batch.service_log_tail[-STAGE_OUTPUT_TAIL_CHARS:]
                    if batch.service_log_tail.strip()
                    else None
                )
            results.append(
                EvaluationStepResult(
                    name=step.name,
                    state=StageState.FAILED if failure is not None else StageState.SUCCEEDED,
                    result=raw.model_dump(mode="json"),
                    failure=failure,
                    service_log_tail=service_log_tail,
                    failure_kind=(
                        None
                        if failure is None
                        else StageFailureKind.EXECUTION
                        if raw.exit_code not in (None, 0)
                        else StageFailureKind.COLLECTION
                    ),
                    duration_s=item.elapsed_seconds,
                    host=batch.node,
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
        follower: StageOutputFollower,
    ) -> SlurmJobStatus:
        """Persist and enforce the queue and run deadlines; return the terminal status.

        Queue wait and run time are bounded separately (``slurm_deadlines``): a job
        that starts late still gets its whole run budget. Each poll that finds a
        stage running also copies its new output (``follower``).
        """
        record = durable or _DurableSlurmEvaluation(handle=handle, request=request)
        deadlines = _deadlines_of(record)
        if deadlines is None:
            deadlines = first_deadlines(
                now=self._clock.now(),
                queued_seconds=handle.waited_seconds,
                limits=self._deadline_limits,
            )
            record = self._save_deadlines(handle_id, record, deadlines)
        while True:
            self._check_deadline(deadlines)
            if durable is not None and not await self._ledger_heartbeat(handle_id, durable):
                self._unless_asked(handle_id)
                return SlurmJobStatus.CANCELLED
            observed = await self._inspect_cancellation_safe(handle_id, handle)
            if isinstance(observed, ClusterObservation):
                projected = self._scheduler_projection(handle_id, observed, request)
                if observed.status.terminal:
                    return observed.status
                folded = observe_deadlines(
                    deadlines,
                    attempt=observed.attempt,
                    running=observed.status is SlurmJobStatus.RUNNING,
                    now=self._clock.now(),
                    limits=self._deadline_limits,
                )
                if folded != deadlines:
                    deadlines = folded
                    record = self._save_deadlines(handle_id, record, deadlines)
                active = projected.lifecycle
                if active is not None:
                    self._publish(handle_id, active)
                    await self._follow(follower, request, active)
            self._check_deadline(deadlines)
            until = deadlines.active_epoch_s
            pause = self._poll_interval
            if until is not None:
                pause = min(pause, until - self._clock.now())
            await self._poll_pause(handle_id, pause)

    def _unless_asked(self, handle_id: str) -> None:
        """Raise ``_LedgerJobGoneError`` unless this run asked for its ledger job to end."""
        if not self._asked(handle_id):
            raise _LedgerJobGoneError(handle_id)

    async def _poll_pause(self, handle_id: str, seconds: float) -> None:
        """Pause at the configured pace unless a read-only poll asks the owner to settle."""
        wakeup = self._poll_wakeups.setdefault(handle_id, asyncio.Event())
        if wakeup.is_set():
            wakeup.clear()
            return
        sleeping = asyncio.create_task(self._sleeper.sleep(seconds))
        waking = asyncio.create_task(wakeup.wait())
        try:
            await asyncio.wait({sleeping, waking}, return_when=asyncio.FIRST_COMPLETED)
            # A wake asks only to cancel a still-pending pause.  If the sleeper
            # completed independently, its failure remains part of the owner's
            # execution outcome even when the wake completed in the same turn.
            if sleeping.done():
                await sleeping
        finally:
            for waiting in (sleeping, waking):
                if not waiting.done():
                    waiting.cancel()
            await asyncio.gather(sleeping, waking, return_exceptions=True)
            wakeup.clear()

    def _check_deadline(self, deadlines: WaitDeadlines) -> None:
        until = deadlines.active_epoch_s
        if until is None or until - self._clock.now() > 0:
            return
        if deadlines.phase is DeadlinePhase.RUN:
            raise _SlurmExecutionError.run_deadline_exceeded(self._job_timeout_seconds)
        raise _SlurmExecutionError.queue_deadline_exceeded(self._queue_timeout_seconds or 0)

    def _save_deadlines(
        self, handle_id: str, record: _DurableSlurmEvaluation, deadlines: WaitDeadlines
    ) -> _DurableSlurmEvaluation:
        """Persist the deadlines before acting on them, so a restart enforces the same ones.

        The rest of the record, its ledger key included, is kept as it was.
        """
        saved = record.model_copy(
            update={
                "wait_deadline_epoch_s": deadlines.queue_epoch_s,
                "run_deadline_epoch_s": deadlines.run_epoch_s,
                "deadline_attempt": deadlines.attempt,
            }
        )
        self._save_evaluation(handle_id, saved)
        return saved

    async def _follow(
        self,
        follower: StageOutputFollower,
        request: EvaluationRequest,
        active: ExecutorObservation,
    ) -> None:
        """Copy the running stage's new output; the read finishes before a cancel lands."""
        if active.state is not EvaluationState.RUNNING or active.current_stage is None:
            return
        names = [step.name for step in request.stages]
        running = names.index(active.current_stage)
        follower.open_through(running)
        await self._finish_in_thread(follower.follow, running)

    def _scheduler_view(
        self, handle_id: str, observed: ClusterObservation
    ) -> tuple[_SchedulerView, JobLife]:
        """Join one reading into the job's held life, then interpret it. Every read path comes here."""
        step = advance_with_notice(self._lives.get(handle_id, INITIAL_LIFE), observed.life)
        self._lives[handle_id] = step.life
        if step.notice is not None:
            _LOG.warning(
                "evaluation %s: scheduler reading %s was not applied (%s)",
                handle_id,
                observed.life,
                step.notice.value,
            )
        if step.requeued:
            _LOG.info(
                "evaluation %s: scheduler requeued the job (attempt %d)",
                handle_id,
                step.life.attempt,
            )
        view = _scheduler_view(step.life, canceling=handle_id in self._canceling)
        return view, step.life

    def _lifecycle_observation(
        self, handle_id: str, observed: ClusterObservation, request: EvaluationRequest
    ) -> ExecutorObservation | None:
        """The lifecycle observation a scheduler reading implies, if it implies one."""
        return self._scheduler_projection(handle_id, observed, request).lifecycle

    def _scheduler_projection(
        self, handle_id: str, observed: ClusterObservation, request: EvaluationRequest
    ) -> _SchedulerProjection:
        """Derive and retain every public fact carried by one scheduler reading."""
        view, life = self._scheduler_view(handle_id, observed)
        current_stage = self._running_stage(handle_id, request, observed) if view.staged else None
        lifecycle = (
            None
            if view.lifecycle is None
            else ExecutorObservation(state=view.lifecycle, current_stage=current_stage)
        )
        active_poll: ExecutorPoll | None = None
        if view.phase is PollPhase.QUEUED:
            active_poll = ExecutorPoll(
                phase=PollPhase.QUEUED,
                attempt=life.attempt,
                pending_reason=observed.pending_reason,
                estimated_start=observed.estimated_start,
            )
        elif view.phase is PollPhase.RUNNING:
            active_poll = ExecutorPoll(
                phase=PollPhase.RUNNING,
                attempt=life.attempt,
                current_stage=current_stage,
            )
        return _SchedulerProjection(view, life, lifecycle, active_poll)

    def _reported_stage(self, handle_id: str, request: EvaluationRequest) -> str:
        """The stage already reported for a running evaluation, or the first."""
        return request.stages[self._stage_floor.get(handle_id, 0)].name

    def _running_stage(
        self, handle_id: str, request: EvaluationRequest, observed: ClusterObservation
    ) -> str:
        """The stage a computing job is in, never earlier than one already reported.

        Stages run in order, so the cluster's count of finished stages names the
        running one. A reading without the count (not observed) says nothing new:
        it keeps the stage already reported, or the first stage if none was.
        """
        last = len(request.stages) - 1
        floor = self._stage_floor.get(handle_id, 0)
        reported = observed.completed_stages
        index = floor if reported is None else max(floor, min(reported, last))
        self._stage_floor[handle_id] = index
        return request.stages[index].name

    async def _in_thread[**P, R](
        self, function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        """Run a blocking call on a worker thread that ``close`` waits for.

        ``asyncio.to_thread`` abandons its worker when the awaiting task is cancelled,
        and the call keeps running in it. Every such call is held in ``_calls`` until
        its thread returns, so ``close`` can wait for it, and a cancelled caller still
        stops waiting at once.
        """
        work = asyncio.ensure_future(asyncio.to_thread(function, *args, **kwargs))
        self._calls.add(work)
        work.add_done_callback(self._call_ended)
        return await asyncio.shield(work)

    def _call_ended(self, work: asyncio.Future[Any]) -> None:
        self._calls.discard(work)
        if not work.cancelled():
            work.exception()  # a cancelled caller never reads the outcome; mark it retrieved

    async def _finish_in_thread[**P, R](
        self, function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        """Run a blocking cluster call that must finish even if its awaiter is cancelled.

        ``asyncio.to_thread`` abandons its worker when the awaiting task is cancelled,
        so a cancel or inspect could still be mid-flight (an scancel not yet sent) after
        the executor reported itself closed. This waits for the call to end, then
        re-raises the cancellation.
        """
        work = asyncio.ensure_future(self._in_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            while not work.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(work)
            raise

    async def _inspect_cancellation_safe(
        self, handle_id: str, handle: SlurmBatchHandle
    ) -> ClusterInspectOutcome:
        inspection = asyncio.create_task(self._in_thread(self._cluster.inspect, handle))
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

    async def _cancel_running(self, handle_id: str, *, resend: bool = False) -> _CancelOutcome:
        """Cancel the evaluation's job, or with the ledger, let go of it."""
        if self._ledger is not None:
            released = await self._release(self._ledger, handle_id)
            if released is not None:
                return released
        return await self._cancel_scheduler_job(handle_id, resend=resend)

    async def _release(self, ledger: LedgerEvaluations, handle_id: str) -> _CancelOutcome | None:
        """Let go of a ledger job; None when the job is still to be cancelled here."""
        durable = self._read_evaluation(handle_id)
        attached = durable is not None and durable.handle is not None
        known = durable.ledger_key if durable is not None and attached else None
        released = await ledger.release(
            handle_id, known, attached=attached, detaching=self._detaching
        )
        match released:
            case Released.DETACHED:
                return _CancelRequested(None)
            case Released.CANCEL_HERE:
                return None
            case Released.DONE:
                return _CancelConfirmed()
            case _:
                assert_never(released)

    async def _cancel_scheduler_job(
        self, handle_id: str, *, resend: bool = False
    ) -> _CancelOutcome:
        """Send scancel once, report CANCELING, then confirm termination within a bound.

        A job that still reads active right after scancel is "cancel requested",
        not an unknown identity: Slurm keeps a cancelled job in COMPLETING while
        it tears down. The evaluation is CANCELING from the acknowledged scancel
        until a terminal reading. Confirmation inspects at the wait loop's pacing
        and shares one bounded budget of inspections across every call for the
        evaluation. A retry inspects once and sends no second scancel, unless
        ``resend`` is set and the budget is spent: a scheduler that acknowledged
        scancel but still shows the job active deserves one more request.
        """
        durable = self._read_evaluation(handle_id)
        if durable is not None and durable.submission_rejection is not None:
            return _CancelConfirmed()
        handle = self._handles.get(handle_id) or (durable.handle if durable is not None else None)
        target = handle if handle is not None else handle_id
        if handle is not None and handle.job.job_id in self._terminated_jobs:
            return _CancelConfirmed()
        job_id = handle.job.job_id if handle is not None else None
        exhausted = resend and self._confirmations_left.get(handle_id) == 0
        if job_id is None or job_id not in self._cancel_sent or exhausted:
            if exhausted:
                self._confirmations_left[handle_id] = self._cancel_confirmations
            cancelled = await self._finish_in_thread(self._cluster.cancel, target)
            if isinstance(cancelled, ClusterUnknown):
                job_id = job_id or cancelled.job_id
                if job_id in {None, _UNRESOLVED_JOB_ID}:
                    raise ExecutorCancellationUnknownError(handle_id)
                # A transient scheduler error does not make a known job unknown. The
                # cluster persisted the cancel intent before it failed, and every
                # inspection of a cancelled record re-sends scancel, so the bounded
                # confirmation below is also the retry. `_cancel_sent` stays unset,
                # so a later call sends scancel again.
            else:
                if (
                    handle is None
                    and durable is not None
                    and not durable.dispatch_started
                    and cancelled.job_id is None
                ):
                    # The durable preparation proves this executor never dispatched.
                    # The cluster retained cancellation intent and has no accepted job
                    # identity, so there is no allocation termination to confirm.
                    self._end_before_dispatch(handle_id, durable.request)
                    return _CancelConfirmed()
                job_id = job_id or cancelled.job_id
                if job_id is not None:
                    self._cancel_sent.add(job_id)
        if job_id is not None:
            self._canceling.add(handle_id)
            self._publish(handle_id, ExecutorObservation(state=EvaluationState.CANCELING))
        return await self._confirm_termination(handle_id, target, job_id)

    async def _confirm_termination(
        self, handle_id: str, target: SlurmBatchHandle | str, job_id: str | None
    ) -> _CancelOutcome:
        if job_id is None:
            return await self._confirm_unaccepted(handle_id, target)
        left = self._confirmations_left.setdefault(handle_id, self._cancel_confirmations)
        for _ in range(max(left, 1)):
            observed = await self._finish_in_thread(self._cluster.inspect, target)
            if isinstance(observed, ClusterObservation) and observed.status.terminal:
                self._terminated_jobs.add(observed.job_id)
                return _CancelConfirmed()
            left = max(0, left - 1)
            self._confirmations_left[handle_id] = left
            if left > 0:
                await self._sleeper.sleep(self._poll_interval)
        return _CancelRequested(job_id)

    async def _confirm_unaccepted(
        self, handle_id: str, target: SlurmBatchHandle | str
    ) -> _CancelOutcome:
        """Confirm that a tombstoned ambiguous dispatch never acquired a job identity.

        Only another cancellation acknowledgement with no job counts as absence:
        unlike a plain inspection, it proves the cluster both retained the tombstone
        and completed a scheduler lookup. A transport-unknown answer remains owned.
        """
        for attempt in range(_SUBMISSION_ABSENCE_CONFIRMATIONS):
            cancelled = await self._finish_in_thread(self._cluster.cancel, target)
            if isinstance(cancelled, ClusterUnknown):
                return _CancelRequested(None)
            job_id = cancelled.job_id
            if job_id is not None:
                self._cancel_sent.add(job_id)
                self._canceling.add(handle_id)
                self._publish(handle_id, ExecutorObservation(state=EvaluationState.CANCELING))
                return await self._confirm_termination(handle_id, target, job_id)
            if attempt + 1 < _SUBMISSION_ABSENCE_CONFIRMATIONS:
                await self._sleeper.sleep(self._poll_interval)
        return _CancelConfirmed()

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
            case _CancelRequested(job_id=_):
                _LOG.warning("cancellation of evaluation %s is not yet confirmed", handle_id)
                return False
            case _:
                assert_never(outcome)

    def _publish(self, handle_id: str, observation: ExecutorObservation) -> ExecutorObservation:
        return self._lifecycle.publish(handle_id, observation)

    def _end_cancelled(self, handle_id: str) -> None:
        """Publish CANCELED for a confirmed cancel, saving the end when no job holds it.

        A poll reads only the saved record and the cluster. When the record names a
        job, the cluster reports that job's end. When it names none (the dispatch's
        sbatch never reached the scheduler, and repeated cancel acknowledgements with
        no job proved so), the cluster knows nothing, and only the saved end keeps a
        poll from reading UNKNOWN forever.
        """
        durable = self._read_evaluation(handle_id)
        if (
            durable is not None
            and durable.dispatch_started
            and durable.submission_rejection is None
            and durable.terminal_failure is None
            and durable.ended is None
        ):
            if durable.handle is None:
                self._end_without_job(handle_id, durable, _DurableEnd.CANCELLED_WITHOUT_JOB)
                return
            if durable.ledger_key is not None:
                # A poll must not read the ledger's still-running job as this run's.
                self._end_without_job(handle_id, durable, _DurableEnd.RELEASED)
                return
        self._publish(handle_id, ExecutorObservation(state=EvaluationState.CANCELED))

    def _end_before_dispatch(self, handle_id: str, request: EvaluationRequest) -> None:
        """End an evaluation that was never dispatched."""
        self._end_without_job(
            handle_id,
            _DurableSlurmEvaluation(handle=None, request=request, dispatch_started=False),
            _DurableEnd.CANCELLED_BEFORE_DISPATCH,
        )

    def _end_without_job(
        self, handle_id: str, durable: _DurableSlurmEvaluation, end: _DurableEnd
    ) -> None:
        """End an evaluation that has no scheduler job: save the end, then publish it.

        The record is written first, so a poll, a restart, or a second cancel can only
        ever see the end once it is durable, and nothing can dispatch the evaluation.
        """
        record = durable.model_copy(update={"ended": end})
        record = _DurableSlurmEvaluation.model_validate(record.model_dump())
        atomic_write_bytes(
            self._handle_root / f"{handle_id}.json", (record.model_dump_json() + "\n").encode()
        )
        self._publish(handle_id, _ENDED_BEFORE_DISPATCH)

    def _publish_durable_end(
        self, handle_id: str, durable: _DurableSlurmEvaluation
    ) -> ExecutorObservation | None:
        """Publish the end the saved record states, or None when the record holds none."""
        if durable.submission_rejection is not None:
            return self._publish_rejection(handle_id, durable.submission_rejection)
        if durable.terminal_failure is not None:
            return self._publish(
                handle_id,
                ExecutorObservation(state=EvaluationState.FAILED, failure=durable.terminal_failure),
            )
        if durable.ended is not None:
            return self._publish(handle_id, _ENDED_BEFORE_DISPATCH)
        return None

    def _record_rejection(self, handle_id: str, request: EvaluationRequest, reason: str) -> None:
        record = _DurableSlurmEvaluation(handle=None, request=request, submission_rejection=reason)
        path = self._handle_root / f"{handle_id}.json"
        atomic_write_bytes(path, (record.model_dump_json() + "\n").encode("utf-8"))

    def _publish_rejection(self, handle_id: str, reason: str) -> ExecutorObservation:
        return self._publish(
            handle_id, ExecutorObservation(state=EvaluationState.FAILED, failure=reason)
        )

    def _record_terminal_failure(self, handle_id: str, failure: str) -> None:
        durable = self._read_evaluation(handle_id)
        if durable is None:
            raise _SlurmExecutionError.missing_durable_evaluation(handle_id)
        record = durable.model_copy(update={"terminal_failure": failure})
        path = self._handle_root / f"{handle_id}.json"
        atomic_write_bytes(path, (record.model_dump_json() + "\n").encode("utf-8"))

    def _write_evaluation(
        self,
        handle_id: str,
        handle: SlurmBatchHandle | None,
        request: EvaluationRequest,
        *,
        dispatch_started: bool = True,
    ) -> None:
        self._save_evaluation(
            handle_id,
            _DurableSlurmEvaluation(
                handle=handle,
                request=request,
                dispatch_started=dispatch_started,
            ),
        )

    def _save_evaluation(self, handle_id: str, record: _DurableSlurmEvaluation) -> None:
        path = self._handle_root / f"{handle_id}.json"
        atomic_write_bytes(path, (record.model_dump_json() + "\n").encode("utf-8"))

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


class _LedgerJobGoneError(RuntimeError):
    """The ledger withdrew, or the cluster cancelled, the job without this run asking."""

    def __init__(self, handle_id: str) -> None:
        super().__init__(f"evaluation {handle_id}'s ledger job ended without its run asking")


class _SlurmStagePayloadError(ValueError):
    @classmethod
    def missing_command(cls, name: str) -> _SlurmStagePayloadError:
        return cls(f"Slurm stage {name!r} requires a nonempty configured command")


def _deadlines_of(record: _DurableSlurmEvaluation) -> WaitDeadlines | None:
    """The saved deadlines, or None when the record has never saved any."""
    if record.deadline_attempt is None and record.wait_deadline_epoch_s is None:
        return None
    return WaitDeadlines(
        attempt=record.deadline_attempt or 0,
        queue_epoch_s=record.wait_deadline_epoch_s,
        run_epoch_s=record.run_deadline_epoch_s,
    )


def _stage_command(name: str, command: str | None) -> tuple[str, ...]:
    if command is None or not command.strip():
        raise _SlurmStagePayloadError.missing_command(name)
    return ("bash", "-c", command)


def _collected_stages(
    request: EvaluationRequest, collected: ClusterCollectOutcome
) -> dict[int, CollectedStage]:
    """Each collected stage's whole output, by its index in the request."""
    if not isinstance(collected, (ClusterCollected, ClusterUnknown)) or not isinstance(
        collected.result, SlurmBatchResult
    ):
        return {}
    by_name = {item.name: item for item in collected.result.stages}
    return {
        index: CollectedStage(
            stdout=item.stdout.encode(),
            stderr=item.stderr.encode(),
            ran=not item.skipped and item.exit_code is not None,
        )
        for index, step in enumerate(request.stages)
        if (item := by_name.get(step.name)) is not None
    }


def _collected_batch(collected: ClusterCollectOutcome, job_id: str) -> SlurmBatchResult:
    if isinstance(collected, (ClusterCollected, ClusterUnknown)) and isinstance(
        collected.result, SlurmBatchResult
    ):
        return collected.result
    raise SlurmError.job_not_terminal(job_id)


# GNU timeout's exit status when it stopped the command (the job script wraps
# each stage in ``timeout --kill-after``; 137 is the follow-up SIGKILL).


def _stage_failure(
    step: EvaluationStep,
    raw: SlurmCommandResult,
    elapsed_seconds: float | None,
) -> str:
    """Return the failure text for a nonzero stage, never empty.

    A stage that is killed by its timeout often prints nothing, and an empty
    failure would make the whole observation invalid and hide the cause. The
    shared server's log tail travels beside it in ``EvaluationStepResult.service_log_tail``,
    never inside this text: this is the stage's own verdict, and a reader that
    cuts the text must keep its end.
    """
    if raw.output.strip():
        return raw.output
    elapsed = "" if elapsed_seconds is None else f" after {elapsed_seconds:.0f} s"
    if raw.exit_code in STAGE_TIMEOUT_EXIT_CODES:
        return f"stage {step.name!r} hit its time limit{elapsed} and printed no output"
    return f"stage {step.name!r} exited with code {raw.exit_code}{elapsed} and printed no output"


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
    def missing_durable_evaluation(cls, handle_id: str) -> _SlurmExecutionError:
        return cls(f"Slurm evaluation {handle_id!r} lost its durable lifecycle record")

    @classmethod
    def run_deadline_exceeded(cls, timeout_seconds: int) -> _SlurmExecutionError:
        return cls(f"Slurm evaluation ran past its {timeout_seconds}-second run deadline")

    @classmethod
    def queue_deadline_exceeded(cls, timeout_seconds: int) -> _SlurmExecutionError:
        return cls(
            f"Slurm evaluation waited in the queue past its {timeout_seconds}-second queue deadline"
        )

    @classmethod
    def deadline_exceeded(cls, timeout_seconds: int) -> _SlurmExecutionError:
        return cls(f"Slurm evaluation exceeded its {timeout_seconds}-second deadline")

    @classmethod
    def closing(cls, handle_id: str) -> _SlurmExecutionError:
        return cls(f"Slurm evaluation {handle_id!r} arrived while its executor was closing")
