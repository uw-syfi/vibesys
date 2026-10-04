"""Product semantic codec over the recoverable fused Slurm executor."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from vibesys.run.evaluation_backend import (
    SemanticEvaluationStage,
    evidence_identity,
    render_stage_failure,
)
from vs_evaluation.api import (
    AvailabilitySnapshot,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    EvidenceKind,
    EvidenceMetric,
    EvidenceOutcome,
    ExecutorObservation,
    ExecutorRejectedError,
    PartialMeasurement,
    ResourceRequirements,
    StageFailureKind,
    StageState,
    TrustedEvidence,
)
from vs_runtime.api import RunCleanupError
from vs_runtime.api.infrastructure import (
    TrustedEvaluationPlan,
    build_trusted_benchmark_command,
    decode_trusted_benchmark_run,
)
from vs_sandbox.api.slurm import (
    PROFILE_OUTPUT_ROOT,
    SharedSlurmAdmission,
    SlurmCommandResult,
    SlurmEvaluationExecutor,
    SlurmEvaluationPlan,
    SlurmStagePayload,
    SlurmTargetLifecycle,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import StateNamespace
    from vs_runtime.api import CandidateWorkspace, Workspaces
    from vs_sandbox.api.slurm import SlurmExecutionPolicy
    from vs_slurm.api import SlurmConfig, SlurmJobRunner

_CLEANUP_FAILURE = "cleanup failed"


_MAX_SUMMARY_CHARS = 16_384  # TrustedEvidence.semantic_summary limit


class _DurableSemanticSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: EvaluationRequest
    snapshot: str


@dataclass(slots=True)
class _Execution:
    workspace: CandidateWorkspace
    executor: SlurmEvaluationExecutor


class SlurmSemanticEvaluationExecutor:
    """Translate semantic evidence requests to one recoverable Slurm batch."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-930068 [PLR0913]; these arguments are independent injected ports or policy facts; grouping them in a DTO would add a shallow mutable carrier and obscure ownership.
        self,
        config: SlurmConfig,
        policy: SlurmExecutionPolicy,
        plan: SlurmEvaluationPlan,
        trusted_plan: TrustedEvaluationPlan,
        workspaces: Workspaces,
        namespace: StateNamespace,
        handle_root: Path,
        *,
        admission: SharedSlurmAdmission | None = None,
        runner: SlurmJobRunner | None = None,
    ) -> None:
        """Bind external Slurm policy to semantic evaluation state."""
        self._config = config
        self._policy = policy
        self._plan = plan
        self._trusted_plan = trusted_plan
        self._workspaces = workspaces
        self._namespace = namespace
        self._handle_root = handle_root
        self._admission = admission or SharedSlurmAdmission(config.evaluation_capacity)
        self._runner = runner
        self._executions: dict[str, _Execution] = {}
        self._lock = asyncio.Lock()
        self._availability = self._make_executor(workspaces.root.path)

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        """Report the shared scheduler admission queue."""
        return await self._availability.availability(requirements)

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        """Persist the semantic request before idempotent provider submission."""
        # Translate first: a stage this executor cannot run is rejected before
        # any record or candidate worktree exists.
        provider_request = self._provider_request(request)
        record = self._record(request)
        existing = self._load(handle_id)
        if existing is not None and existing != record:
            raise ValueError(f"semantic Slurm handle {handle_id!r} has different work")  # noqa: TRY003  # lint-waiver: LW-930069 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        if existing is None:
            self._namespace.save(self._path(handle_id), record)
        execution = await self._execution(handle_id, record)
        await execution.executor.submit(provider_request, handle_id=handle_id)

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        """Observe durable provider work without recreating a workspace or recovery task."""
        record = self._load(handle_id)
        if record is None:
            return None
        execution = self._executions.get(handle_id)
        executor = execution.executor if execution is not None else self._availability
        observed = await executor.inspect_only(handle_id)
        return None if observed is None else self._semantic_observation(record.request, observed)

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        """Recover the candidate worktree and provider handle on demand."""
        record = self._load(handle_id)
        if record is None:
            return None
        execution = await self._execution(handle_id, record)
        observed = await execution.executor.inspect(handle_id)
        return None if observed is None else self._semantic_observation(record.request, observed)

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        """Wait boundedly for provider progress when the handle is known."""
        record = self._load(handle_id)
        if record is None:
            return
        execution = await self._execution(handle_id, record)
        await execution.executor.wait_for_change(handle_id, timeout_s)

    async def cancel(self, handle_id: str) -> None:
        """Cancel a provider job, recovering its durable handle if needed."""
        record = self._load(handle_id)
        if record is None:
            return
        execution = await self._execution(handle_id, record)
        await execution.executor.cancel(handle_id)

    async def close(self) -> None:
        """Release candidate worktrees after provider tasks have settled."""
        async with self._lock:
            cleanups = tuple(
                cleanup
                for execution in self._executions.values()
                for cleanup in (execution.executor.close(), execution.workspace.discard())
            )
            results = await asyncio.gather(*cleanups, return_exceptions=True)
            self._executions.clear()
        errors: list[Exception] = []
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                errors.append(result)
            elif isinstance(result, BaseException):
                raise result
        if errors:
            raise RunCleanupError(_CLEANUP_FAILURE, tuple(errors))

    def _record(self, request: EvaluationRequest) -> _DurableSemanticSubmission:
        stages = tuple(
            SemanticEvaluationStage.model_validate(step.payload) for step in request.stages
        )
        snapshots = {stage.snapshot for stage in stages}
        if len(snapshots) != 1:
            raise ValueError("fused semantic stages must use one candidate snapshot")  # noqa: TRY003  # lint-waiver: LW-930070 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return _DurableSemanticSubmission(request=request, snapshot=next(iter(snapshots)))

    async def _execution(self, handle_id: str, record: _DurableSemanticSubmission) -> _Execution:
        async with self._lock:
            existing = self._executions.get(handle_id)
            if existing is not None:
                return existing
            workspace = await self._workspaces.create_candidate(record.snapshot)
            execution = _Execution(workspace, self._make_executor(workspace.path))
            self._executions[handle_id] = execution
            return execution

    def _make_executor(self, workspace: Path) -> SlurmEvaluationExecutor:
        return SlurmEvaluationExecutor(
            self._config,
            workspace=workspace,
            setup_script=self._policy.setup_script,
            service=self._policy.remote_service(),
            support_trees=self._plan.support_paths,
            handle_root=self._handle_root,
            supported_evidence_kinds=self._supported_evidence_kinds(),
            admission=self._admission,
            runner=self._runner,
        )

    def _supported_evidence_kinds(self) -> tuple[str, ...]:
        """Return the kinds this plan has a command for.

        A stage without a command would run nothing and exit 0, which the
        evidence mapping would record as a pass for a workload that never ran.
        """
        commands = (
            (EvidenceKind.ACCURACY, self._plan.accuracy_command),
            (EvidenceKind.BENCHMARK, self._plan.benchmark_command),
            (EvidenceKind.PROFILE, self._plan.profile_command),
        )
        return tuple(kind.value for kind, command in commands if command is not None)

    def _provider_request(self, request: EvaluationRequest) -> EvaluationRequest:
        return request.model_copy(
            update={
                "stages": tuple(
                    EvaluationStep(
                        name=step.name,
                        payload=self._provider_stage(
                            SemanticEvaluationStage.model_validate(step.payload)
                        ).model_dump(mode="json"),
                    )
                    for step in request.stages
                )
            }
        )

    def _provider_stage(self, stage: SemanticEvaluationStage) -> SlurmStagePayload:
        if stage.kind is EvidenceKind.ACCURACY:
            argv = self._plan.accuracy_command
            command = (
                None if argv is None else shlex.join((*argv, *self._policy.accuracy_arguments))
            )
            timeout = self._trusted_plan.accuracy_timeout_seconds
        elif stage.kind is EvidenceKind.BENCHMARK:
            argv = self._plan.benchmark_command
            command = (
                None if argv is None else shlex.join((*argv, *self._policy.benchmark_arguments))
            )
            contract = self._trusted_plan.benchmark_contract
            if command is not None and contract is not None:
                command = build_trusted_benchmark_command(
                    command, contract, ".vibesys-framework-benchmark.json"
                )
            timeout = self._trusted_plan.benchmark_timeout_seconds
        elif stage.kind is EvidenceKind.PROFILE and self._plan.profile_command is not None:
            # The capture starts, loads, and stops the service itself, inside
            # the job's timeout, and its traces come back into the run-owned
            # candidate worktree.
            return SlurmStagePayload(
                command=shlex.join(self._plan.profile_command),
                tree_artifact_refs=(PROFILE_OUTPUT_ROOT,),
                target_lifecycle=SlurmTargetLifecycle.COMMAND_MANAGED,
            )
        else:
            command = None
            timeout = None
        if command is None:
            supported = ", ".join(self._supported_evidence_kinds())
            message = f"Slurm semantic executor supports {supported} only, not {stage.kind.value}"
            raise ExecutorRejectedError(message)
        return SlurmStagePayload(command=command, timeout_seconds=timeout)

    def _semantic_observation(
        self, request: EvaluationRequest, observed: ExecutorObservation
    ) -> ExecutorObservation:
        results: list[EvaluationStepResult] = []
        failed_checks: list[tuple[str | None, EvidenceKind]] = []
        infrastructure_failure = False
        metadata = (
            SlurmCommandResult.model_validate(observed.stage_results[0].result).execution_metadata
            if observed.stage_results and observed.stage_results[0].result is not None
            else None
        )
        for step, raw_step in zip(request.stages, observed.stage_results, strict=False):
            if raw_step.result is None:
                results.append(raw_step.model_copy(update={"name": step.name}))
                continue
            stage = SemanticEvaluationStage.model_validate(step.payload)
            raw = SlurmCommandResult.model_validate(raw_step.result)
            completed = (
                raw.executed
                and raw.exit_code is not None
                and raw.collection_failure is None
                and metadata is not None
                and metadata.job_exit_code == 0
                and metadata.collection_failure is None
            )
            infrastructure_failure |= not completed
            evidence = self._evidence(stage, raw, raw_step.failure, completed=completed)
            if evidence.outcome is EvidenceOutcome.FAILED:
                failed_checks.append((evidence.semantic_summary, stage.kind))
            results.append(
                EvaluationStepResult(
                    name=step.name,
                    state=StageState.SUCCEEDED if completed else StageState.FAILED,
                    result=evidence.model_dump(mode="json"),
                    duration_s=raw_step.duration_s,
                    failure_kind=None if completed else StageFailureKind.COLLECTION,
                    failure=(
                        None
                        if completed
                        else raw_step.failure or observed.failure or evidence.semantic_summary
                    ),
                )
            )
        has_semantic_result = any(item.result is not None for item in results)
        skipped = any(item.state is StageState.SKIPPED for item in results)
        failure = observed.failure
        if infrastructure_failure and observed.state in {
            EvaluationState.SUCCEEDED,
            EvaluationState.FAILED,
        }:
            state = EvaluationState.FAILED
            failure = render_stage_failure(failed_checks, observed.failure)
        elif observed.state is EvaluationState.FAILED and has_semantic_result and not skipped:
            state = EvaluationState.SUCCEEDED
        elif skipped and has_semantic_result:
            # A successful evaluation must complete every planned stage. A failed
            # stage that skipped the rest makes the evaluation failed, and its
            # diagnostics are the failure the submitting agent reads.
            state = EvaluationState.FAILED
            failure = render_stage_failure(failed_checks, observed.failure)
        else:
            state = observed.state
        return ExecutorObservation(
            state=state,
            current_stage=(
                None
                if state
                in {
                    EvaluationState.SUCCEEDED,
                    EvaluationState.FAILED,
                    EvaluationState.CANCELED,
                    EvaluationState.SUPERSEDED,
                }
                else observed.current_stage
            ),
            stage_results=tuple(results),
            failure=None if state is EvaluationState.SUCCEEDED else failure,
        )

    def _evidence(
        self,
        stage: SemanticEvaluationStage,
        raw: SlurmCommandResult,
        failure: str | None,
        *,
        completed: bool,
    ) -> TrustedEvidence:
        passed = completed and raw.exit_code == 0
        metrics: tuple[EvidenceMetric, ...] = ()
        summary: str | None = None
        partial: PartialMeasurement | None = None
        contract = self._trusted_plan.benchmark_contract
        if stage.kind is EvidenceKind.BENCHMARK and contract is not None:
            decoded = decode_trusted_benchmark_run(
                raw.output, contract, frozenset(), exited_cleanly=passed
            )
            partial = decoded.partial
            # The evaluator's own stop reason leads, as on the local executor;
            # the raw output tail would end with its command line instead.
            summary = decoded.reason
            if decoded.violation is not None and passed:
                summary = decoded.violation
            elif decoded.violation is not None:
                failure = f"{failure or raw.output}\n{decoded.violation}"
            passed = decoded.passed
            metrics = tuple(
                EvidenceMetric(
                    name=name,
                    value=value,
                    direction=(
                        decoded.metrics[name].direction if name in decoded.metrics else None
                    ),
                    unit=decoded.metrics[name].unit if name in decoded.metrics else None,
                )
                for name, value in sorted((decoded.row or {}).items())
            )
        if stage.kind is EvidenceKind.PROFILE and passed:
            # The capture's printed summary is the profile's evidence; its end
            # holds the attribution tables.
            summary = (raw.stdout or raw.output)[-_MAX_SUMMARY_CHARS:] or None
        if not passed and summary is None:
            # The provider's stage failure already holds the stage output plus the
            # server log tail; keep its end, where the cause usually is.
            detail = failure or raw.output
            summary = detail[-_MAX_SUMMARY_CHARS:] or f"{stage.kind.value} command failed"
        outcome = EvidenceOutcome.PASSED if passed else EvidenceOutcome.FAILED
        evidence_id = evidence_identity(stage, outcome, summary, metrics, partial)
        return TrustedEvidence(
            evidence_id=evidence_id,
            evaluation_id=evidence_id,
            stage_name=stage.kind.value,
            kind=stage.kind,
            fingerprints=stage.fingerprints,
            trusted_inputs=stage.fingerprints.candidate,
            outcome=outcome,
            semantic_summary=summary,
            metrics=metrics,
            partial_measurement=partial,
            accepted_round=0,
        )

    def _load(self, handle_id: str) -> _DurableSemanticSubmission | None:
        return self._namespace.load_optional(self._path(handle_id), _DurableSemanticSubmission)

    @staticmethod
    def _path(handle_id: str) -> str:
        return f"slurm-semantic/{handle_id}.json"


__all__ = ["SlurmSemanticEvaluationExecutor"]
