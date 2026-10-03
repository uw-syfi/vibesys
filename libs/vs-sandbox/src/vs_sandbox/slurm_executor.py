"""Recoverable fused evaluation execution through Slurm."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from vs_evaluation.api import (
    AvailabilitySnapshot,
    AvailabilityState,
    CostClass,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    ExecutorObservation,
    ExecutorSubmissionError,
    ResourceRequirements,
    ReuseStatus,
    StageState,
)
from vs_slurm.api import (
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmJobRunner,
    SlurmTreeArtifact,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from pathlib import Path

    from vs_slurm.api import SlurmConfig, SlurmService


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


class SlurmCommandResult(BaseModel):
    """Portable result stored in an evaluation stage result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    executed: bool = True
    execution_metadata: SlurmExecutionMetadata | None = None


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

    handle: SlurmBatchHandle
    request: EvaluationRequest
    wait_deadline_epoch_s: float | None = None


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
        runner: SlurmJobRunner | None = None,
        deadline_clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind scheduler policy, durable state, and an injectable runner."""
        self._runner = runner if runner is not None else SlurmJobRunner(config)
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
        if handle_id in self._tasks:
            return
        try:
            stages = self._parse_stages(request)
            durable = self._read_evaluation(handle_id)
            self._validate_durable_request(handle_id, request, durable)
            self._publish(
                handle_id,
                ExecutorObservation(
                    state=EvaluationState.QUEUED, current_stage=request.stages[0].name
                ),
            )
            self._tasks[handle_id] = asyncio.create_task(
                self._admit_and_execute(handle_id, request, stages),
                name=f"vibesys-slurm-{handle_id[-12:]}",
            )
        except Exception as exc:
            raise ExecutorSubmissionError(exc) from exc

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        """Recover a durable provider handle and resume collection when needed."""
        observed = self._observations.get(handle_id)
        if observed is not None:
            return observed
        durable = self._read_evaluation(handle_id)
        if durable is None:
            return None
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
            self._resume_admitted(handle_id, durable.request, stages),
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
        task = self._tasks.get(handle_id)
        if task is not None and not task.done():
            await self._cancel_running(handle_id)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        elif self._read_evaluation(handle_id) is not None:
            await self._cancel_running(handle_id)
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
    ) -> None:
        try:
            async with self._admission.lease(handle_id):
                self._publish(
                    handle_id,
                    ExecutorObservation(
                        state=EvaluationState.STARTING,
                        current_stage=request.stages[0].name,
                    ),
                )
                await self._accept_cancellation_safe(handle_id, request, stages)
                await self._execute(handle_id, request)
        except asyncio.CancelledError:
            self._publish(handle_id, ExecutorObservation(state=EvaluationState.CANCELED))
            raise
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-930042 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
            with contextlib.suppress(Exception):
                await self._cancel_running(handle_id)
            self._publish(
                handle_id,
                ExecutorObservation(
                    state=EvaluationState.FAILED,
                    failure=f"{type(exc).__name__}: {exc}",
                ),
            )

    async def _resume_admitted(
        self,
        handle_id: str,
        request: EvaluationRequest,
        stages: tuple[SlurmStagePayload, ...],
    ) -> None:
        del stages
        try:
            async with self._admission.lease(handle_id):
                await self._execute(handle_id, request)
        except asyncio.CancelledError:
            self._publish(handle_id, ExecutorObservation(state=EvaluationState.CANCELED))
            raise
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-930043 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
            with contextlib.suppress(Exception):
                await self._cancel_running(handle_id)
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
    ) -> None:
        acceptance = asyncio.create_task(self._accept(handle_id, request, stages))
        try:
            await asyncio.shield(acceptance)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await acceptance
            with contextlib.suppress(Exception):
                await self._cancel_running(handle_id)
            raise

    async def _accept(
        self,
        handle_id: str,
        request: EvaluationRequest,
        stages: tuple[SlurmStagePayload, ...],
    ) -> None:
        durable = self._read_evaluation(handle_id)
        existing = self._handles.get(handle_id) or (durable.handle if durable is not None else None)
        if existing is not None:
            self._handles[handle_id] = existing
            return
        lifecycle = stages[0].target_lifecycle
        batch_stages = tuple(
            SlurmBatchStage(
                name=step.name,
                command=("bash", "-c", stage.command or "true"),
                timeout_seconds=stage.timeout_seconds,
                tree_artifacts=tuple(
                    SlurmTreeArtifact(remote_path=ref, local_path=self._workspace / ref)
                    for ref in stage.tree_artifact_refs
                ),
            )
            for step, stage in zip(request.stages, stages, strict=True)
        )
        handle = await asyncio.to_thread(
            self._runner.submit_batch,
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
        )
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
        collection = asyncio.create_task(asyncio.to_thread(self._runner.collect_batch, handle))
        try:
            batch = await asyncio.shield(collection)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await collection
            raise
        metadata = SlurmExecutionMetadata(
            phase_timings_seconds=dict(batch.phase_timings_seconds),
            content_cache_hits=batch.content_cache_hits,
        )
        by_name = {item.name: item for item in batch.stages}
        results: list[EvaluationStepResult] = []
        for index, step in enumerate(request.stages):
            item = by_name.get(step.name)
            if item is None or item.skipped:
                if index == 0:
                    output = batch.job_output or "Slurm batch returned no stage results"
                    raw = SlurmCommandResult(
                        output=output,
                        exit_code=batch.job_exit_code or 1,
                        execution_metadata=metadata,
                    )
                    results.append(
                        EvaluationStepResult(
                            name=step.name,
                            state=StageState.FAILED,
                            result=raw.model_dump(mode="json"),
                            failure=output,
                        )
                    )
                else:
                    results.append(EvaluationStepResult(name=step.name, state=StageState.SKIPPED))
                continue
            output = item.stdout + item.stderr
            raw = SlurmCommandResult(
                output=output,
                exit_code=item.exit_code or 0,
                stdout=item.stdout,
                stderr=item.stderr,
                execution_metadata=metadata if index == 0 else None,
            )
            failed = raw.exit_code != 0
            results.append(
                EvaluationStepResult(
                    name=step.name,
                    state=StageState.FAILED if failed else StageState.SUCCEEDED,
                    result=raw.model_dump(mode="json"),
                    failure=_stage_failure(step, raw, item.elapsed_seconds) if failed else None,
                    duration_s=item.elapsed_seconds,
                )
            )
        failed = next((item for item in results if item.state is StageState.FAILED), None)
        self._publish(
            handle_id,
            ExecutorObservation(
                state=EvaluationState.FAILED if failed is not None else EvaluationState.SUCCEEDED,
                stage_results=tuple(results),
                failure=failed.failure if failed is not None else None,
            ),
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
        wait_call = asyncio.create_task(
            asyncio.to_thread(
                self._runner.wait_batch,
                handle,
                timeout_seconds=remaining,
            )
        )
        try:
            waited = await asyncio.shield(wait_call)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await wait_call
            raise
        handle = waited.handle
        self._handles[handle_id] = handle
        self._write_evaluation(
            handle_id,
            handle,
            request,
            wait_deadline_epoch_s=wait_deadline_epoch_s,
        )
        if waited.timed_out:
            raise _SlurmExecutionError.deadline_exceeded(self._job_timeout_seconds)
        return handle

    @staticmethod
    def _parse_stages(request: EvaluationRequest) -> tuple[SlurmStagePayload, ...]:
        # EvaluationStep.payload has already crossed a JSON boundary, so tuples
        # and string enums have their JSON representation here.
        stages = tuple(
            SlurmStagePayload.model_validate(stage.payload, strict=False)
            for stage in request.stages
        )
        if len({stage.target_lifecycle for stage in stages}) != 1:
            raise ValueError(  # noqa: TRY003  # lint-waiver: LW-930044 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
                "all fused evaluation stages must use one target lifecycle policy"
            )
        return stages

    async def _cancel_running(self, handle_id: str) -> None:
        durable = self._read_evaluation(handle_id)
        handle = self._handles.get(handle_id) or (durable.handle if durable is not None else None)
        if handle is not None:
            await asyncio.to_thread(self._runner.cancel_batch, handle)

    def _publish(self, handle_id: str, observation: ExecutorObservation) -> None:
        self._observations[handle_id] = observation
        self._changes.setdefault(handle_id, asyncio.Event()).set()

    def _write_evaluation(
        self,
        handle_id: str,
        handle: SlurmBatchHandle,
        request: EvaluationRequest,
        *,
        wait_deadline_epoch_s: float | None = None,
    ) -> None:
        path = self._handle_root / f"{handle_id}.json"
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        document = (
            _DurableSlurmEvaluation(
                handle=handle,
                request=request,
                wait_deadline_epoch_s=wait_deadline_epoch_s,
            ).model_dump_json()
            + "\n"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            output = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = -1
            with output:
                output.write(document)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
            directory = os.open(self._handle_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

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


# GNU timeout's exit status when it stopped the command (the job script wraps
# each stage in ``timeout --kill-after``; 137 is the follow-up SIGKILL).
_TIMEOUT_EXIT_CODES = frozenset({124, 137})


def _stage_failure(
    step: EvaluationStep, raw: SlurmCommandResult, elapsed_seconds: float | None
) -> str:
    """Return the failure text for a nonzero stage, never empty.

    A stage that is killed by its timeout often prints nothing, and an empty
    failure would make the whole observation invalid and hide the cause.
    """
    if raw.output.strip():
        return raw.output
    elapsed = "" if elapsed_seconds is None else f" after {elapsed_seconds:.0f} s"
    if raw.exit_code in _TIMEOUT_EXIT_CODES:
        return f"stage {step.name!r} hit its time limit{elapsed} and printed no output"
    return f"stage {step.name!r} exited with code {raw.exit_code}{elapsed} and printed no output"


class _SlurmExecutionError(RuntimeError):
    @classmethod
    def request_conflict(cls, handle_id: str) -> _SlurmExecutionError:
        return cls(f"Slurm evaluation handle {handle_id!r} has a different request")

    @classmethod
    def not_accepted(cls) -> _SlurmExecutionError:
        return cls("Slurm evaluation was not durably accepted before execution")

    @classmethod
    def deadline_exceeded(cls, timeout_seconds: int) -> _SlurmExecutionError:
        return cls(f"Slurm evaluation exceeded its {timeout_seconds}-second deadline")
