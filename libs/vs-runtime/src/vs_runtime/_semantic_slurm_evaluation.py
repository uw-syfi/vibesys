"""Semantic evidence codec over the recoverable fused Slurm executor.

The executor translates semantic stage requests into one Slurm batch and decodes
the batch's terminal stage results into trusted evidence. It is generic: the
product supplies the agent-visible failure text through ``stage_failure_text``.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from vs_agent.api import AgentExecutionCancelledError
from vs_evaluation.api import (
    MAX_EVIDENCE_SUMMARY_CHARS,
    STAGE_OUTPUT_TAIL_CHARS,
    AvailabilitySnapshot,
    EvaluationRequest,
    EvaluationState,
    EvaluationStateNamespace,
    EvaluationStep,
    EvaluationStepResult,
    EvidenceKind,
    EvidenceMetric,
    EvidenceOutcome,
    EvidenceResultIdentity,
    ExecutorObservation,
    ExecutorPoll,
    ExecutorRejectedError,
    ExecutorSubmissionError,
    PartialMeasurement,
    PollPhase,
    ResourceRequirements,
    SemanticEvaluationStage,
    StageFailureKind,
    StageState,
    TrustedEvidence,
    evidence_identity,
    is_finished,
    same_work,
)
from vs_redaction.api import redact_diagnostic_tail
from vs_runtime._evaluation_failure_text import render_stage_output_with_service_log
from vs_runtime._failure_classification import RecordState, classify, is_unsettled, signal_of
from vs_runtime._host_path_masking import mask_observation, private_paths
from vs_runtime._measurement_progress import StageRun
from vs_runtime._trusted_evaluation import (
    TrustedEvaluationPlan,
    build_trusted_benchmark_command,
    decode_trusted_benchmark_run,
)
from vs_runtime.contracts import BenchmarkFailureKind, RunCleanupError
from vs_sandbox.api.slurm import (
    PROFILE_OUTPUT_ROOT,
    SharedSlurmAdmission,
    SlurmCommandResult,
    SlurmEvaluationExecutor,
    SlurmEvaluationPlan,
    SlurmExecutionMetadata,
    SlurmStagePayload,
    SlurmTargetLifecycle,
    open_ledger_attachment,
    profile_capture_descriptor,
    require_profile_fields,
    split_profile_stats,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path

    from vs_runtime._measurement_progress import StageOutputOpener
    from vs_runtime._run_control import RunControlChannel
    from vs_runtime.contracts import Checkout, MeasuredCheckouts
    from vs_sandbox.api.slurm import (
        SlurmExecutionPolicy,
        SlurmLedgerAttachment,
        SlurmStageOutputOpener,
    )
    from vs_sim.api import Clock, OutputSink, Processes, Sleeper
    from vs_slurm.api import Cluster, SlurmConfig, WorkspaceRootProof

_CLEANUP_FAILURE = "cleanup failed"
SERVICE_LOG_SUMMARY_CHARS = 2_000
"""The most service log a failed stage's summary keeps; its own output matters more."""

type StageFailureText = Callable[[Sequence[tuple[str | None, EvidenceKind]], str | None], str]


def _completed(raw: SlurmCommandResult, metadata: SlurmExecutionMetadata | None) -> bool:
    """Whether the stage ran to an exit status inside a job that finished cleanly."""
    return (
        raw.executed
        and raw.exit_code is not None
        and raw.collection_failure is None
        and metadata is not None
        and metadata.job_exit_code == 0
        and metadata.collection_failure is None
    )


def _redacted_service_log(step: EvaluationStepResult) -> str | None:
    """The step's service log tail, credential-redacted and bounded like the output tails."""
    if step.service_log_tail is None:
        return None
    return redact_diagnostic_tail(step.service_log_tail, max_chars=STAGE_OUTPUT_TAIL_CHARS) or None


def _stage_failure_kind(evidence: TrustedEvidence) -> StageFailureKind:
    """An ambiguous stage ran and was cut short; every other unsettled one lost its result."""
    if evidence.failure_kind is BenchmarkFailureKind.AMBIGUOUS:
        return StageFailureKind.EXECUTION
    return StageFailureKind.COLLECTION


def _first_defined(*values: str | None) -> str | None:
    """Return the first supplied projection, preserving an explicitly empty safe value."""
    return next((value for value in values if value is not None), None)


class _DurableSemanticSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: EvaluationRequest
    snapshot: str


@dataclass(slots=True)
class _Execution:
    workspace: Checkout
    executor: SlurmEvaluationExecutor


class SemanticSlurmEvaluationExecutor:
    """Translate semantic evidence requests to one recoverable Slurm batch."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-930068 [PLR0913]; these arguments are independent injected ports or policy facts; grouping them in a DTO would add a shallow mutable carrier and obscure ownership.
        self,
        config: SlurmConfig,
        policy: SlurmExecutionPolicy,
        plan: SlurmEvaluationPlan,
        trusted_plan: TrustedEvaluationPlan,
        workspaces: MeasuredCheckouts,
        namespace: EvaluationStateNamespace,
        handle_root: Path,
        *,
        stage_failure_text: StageFailureText,
        clock: Clock,
        sleeper: Sleeper,
        admission: SharedSlurmAdmission | None = None,
        cluster: Cluster | None = None,
        processes: Processes | None = None,
        ledger: SlurmLedgerAttachment | None = None,
        ledger_identity: tuple[str, str] | None = None,
        stage_output: StageOutputOpener | None = None,
        root_proof: WorkspaceRootProof | None = None,
        control: RunControlChannel | None = None,
    ) -> None:
        """Bind external Slurm policy to semantic evaluation state.

        ``clock`` times job deadlines; ``sleeper`` paces the polls of running jobs. Each
        executor uses ``cluster``, or reaches the configured cluster by starting ``processes``.

        When the operator's policy enables the evaluation ledger (#2757), evaluations
        go through ``ledger``, or through the project's ledger on the configured
        cluster, which ``ledger_identity`` (the project id and the run id) names.

        ``stage_output`` opens each stage's stream, which the stage's output on the cluster
        reaches at each poll while the job runs and in full when it settles.

        ``root_proof`` is the run's workspace-root probe; no evaluation is submitted
        before it proves the root, and none at all once it failed.
        """
        self._control = control
        self._root_proof = root_proof
        self._stage_failure_text = stage_failure_text
        self._clock = clock
        self._sleeper = sleeper
        self._config = config
        self._policy = policy
        self._private_paths = private_paths(config, policy.private_paths())
        self._plan = plan
        self._trusted_plan = trusted_plan
        self._workspaces = workspaces
        self._namespace = namespace
        self._handle_root = handle_root
        self._admission = admission or SharedSlurmAdmission(config.evaluation_capacity)
        self._cluster = cluster
        self._processes = processes
        self._stage_output = stage_output
        self._executions: dict[str, _Execution] = {}
        self._lock = asyncio.Lock()
        # Set by ``close``: no submission makes a new candidate executor after it.
        self._closing = False
        self._ledger = ledger if ledger is not None else self._open_ledger(ledger_identity)
        self._availability = self._make_executor(workspaces.root.path)

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        """Report the shared scheduler admission queue."""
        snapshot = await self._availability.availability(requirements)
        return snapshot.model_copy(
            update={
                "supported_profile_fields": profile_capture_descriptor(
                    self._plan.profile_command
                ).supported_fields
            }
        )

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        """Persist the semantic request before idempotent provider submission."""
        # Validate first: a request this executor cannot run, or that conflicts with
        # the work already recorded under its handle, is refused before any record
        # or candidate worktree exists. Each refusal is a typed rejection so the
        # requester resolves at once; any other exception reads as an ambiguous
        # submission and leaves the requester waiting.
        self._refuse_if_closing(handle_id)
        try:
            provider_request = self._provider_request(request)
            record = self._record(request)
        except ExecutorRejectedError:
            raise
        except ValueError as error:
            raise ExecutorRejectedError(str(error)) from error
        existing = self._load(handle_id)
        if existing is not None and (
            existing.snapshot != record.snapshot or not same_work(existing.request, record.request)
        ):
            message = f"semantic Slurm handle {handle_id!r} has different work"
            raise ExecutorRejectedError(message)
        if existing is not None and existing != record:
            # The same work, recorded before its stages named their purpose.
            self._namespace.save(self._path(handle_id), record)
        if existing is None:
            # The revision to measure is named by a search, often from an agent's reply. A
            # revision the repository does not hold can never run, so refuse it before a
            # durable record exists that nothing could ever launch or cancel.
            if not await self._workspaces.root.has_revision(record.snapshot):
                message = f"candidate revision {record.snapshot!r} is not a commit of this run"
                raise ExecutorRejectedError(message)
            self._namespace.save(self._path(handle_id), record)
        execution = await self._execution(handle_id, record, submitting=True)
        await execution.executor.submit(provider_request, handle_id=handle_id)

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        """Observe durable provider work without recreating a workspace or recovery task."""
        record = self._load(handle_id)
        if record is None:
            return None
        execution = self._executions.get(handle_id)
        executor = execution.executor if execution is not None else self._availability
        observed = await executor.inspect_only(handle_id)
        return (
            None
            if observed is None
            else self._semantic_observation(handle_id, record.request, observed)
        )

    async def poll(self, handle_id: str) -> ExecutorPoll:
        """Inspect provider work once, without a workspace, recovery task or submission."""
        record = self._load(handle_id)
        if record is None:
            return ExecutorPoll(phase=PollPhase.UNSUBMITTED)
        execution = self._executions.get(handle_id)
        executor = execution.executor if execution is not None else self._availability
        polled = await executor.poll(handle_id)
        if polled.terminal is None:
            return polled
        return polled.model_copy(
            update={
                "terminal": self._semantic_observation(handle_id, record.request, polled.terminal)
            }
        )

    async def recover(self, handle_id: str) -> ExecutorPoll:
        """The durable submission and provider identity make pure polling authoritative."""
        return await self.poll(handle_id)

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        """Recover the candidate worktree and provider handle on demand."""
        record = self._load(handle_id)
        if record is None:
            return None
        execution = await self._execution(handle_id, record)
        observed = await execution.executor.inspect(handle_id)
        return (
            None
            if observed is None
            else self._semantic_observation(handle_id, record.request, observed)
        )

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
        """Refuse new submissions, then release candidate worktrees once provider tasks settled.

        A submission already holding its candidate executor is refused by that
        executor's own ``close``.
        """
        self._closing = True
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
            message = "fused semantic stages must use one candidate snapshot"
            raise ValueError(message)
        return _DurableSemanticSubmission(request=request, snapshot=next(iter(snapshots)))

    async def _execution(
        self, handle_id: str, record: _DurableSemanticSubmission, *, submitting: bool = False
    ) -> _Execution:
        async with self._lock:
            existing = self._executions.get(handle_id)
            if existing is not None:
                return existing
            if submitting:
                # ``close`` may have run while this submission awaited: an executor made
                # now would never be closed, and its job never cancelled.
                self._refuse_if_closing(handle_id)
            workspace = await self._workspaces.create_measured_candidate(record.snapshot)
            executor = self._make_executor(
                workspace.path,
                self._stage_streams(record.request),
                candidate=record.snapshot,
            )
            execution = _Execution(workspace, executor)
            self._executions[handle_id] = execution
            return execution

    def _refuse_if_closing(self, handle_id: str) -> None:
        """Refuse a submission once ``close`` began: ambiguous, so a resume may submit it."""
        if self._closing:
            message = f"semantic Slurm evaluation {handle_id!r} arrived while closing"
            raise ExecutorSubmissionError(RuntimeError(message))

    def _open_ledger(self, identity: tuple[str, str] | None) -> SlurmLedgerAttachment | None:
        """The run's attachment to its project's ledger, if the operator enabled one."""
        policy = self._policy.ledger
        if policy is None:
            return None
        if identity is None or self._processes is None:
            message = "the evaluation ledger needs the project and run identity and a transport"
            raise ValueError(message)
        project, run = identity
        return open_ledger_attachment(
            self._config,
            policy,
            project=project,
            holder=run,
            clock=self._clock,
            processes=self._processes,
            state_root=self._handle_root / "ledger",
        )

    def _stage_streams(self, request: EvaluationRequest) -> SlurmStageOutputOpener | None:
        """The provider's per-stage opener, naming each stage as its progress reports do."""
        opener = self._stage_output
        if opener is None:
            return None
        purposes = {
            step.name: SemanticEvaluationStage.model_validate(step.payload).purpose
            for step in request.stages
        }

        def open_stage(handle_id: str, stage_id: str) -> AbstractContextManager[OutputSink]:
            return opener(StageRun(handle_id, stage_id, purpose=purposes.get(stage_id)))

        return open_stage

    def _make_executor(
        self,
        workspace: Path,
        stage_output: SlurmStageOutputOpener | None = None,
        *,
        candidate: str | None = None,
    ) -> SlurmEvaluationExecutor:
        return SlurmEvaluationExecutor(
            self._config,
            workspace=workspace,
            setup_script=self._policy.setup_script,
            service=self._policy.remote_service(),
            support_trees=self._plan.support_paths,
            handle_root=self._handle_root,
            supported_evidence_kinds=self._supported_evidence_kinds(),
            clock=self._clock,
            sleeper=self._sleeper,
            admission=self._admission,
            cluster=self._cluster,
            processes=self._processes,
            ledger=self._ledger,
            ledger_candidate=candidate,
            stage_output=stage_output,
            root_proof=self._root_proof,
            before_dispatch=self._admit_execution,
        )

    def _admit_execution(self, request: EvaluationRequest) -> None:
        if self._control is not None:
            try:
                self._control.start_execution(
                    lambda: None,
                    evaluation_id=request.key,
                )
            except AgentExecutionCancelledError as error:
                raise ExecutorRejectedError(str(error)) from error

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
                command=shlex.join(
                    require_profile_fields(
                        self._plan.profile_command, stage.required_profile_fields
                    )
                ),
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
        self, handle_id: str, request: EvaluationRequest, observed: ExecutorObservation
    ) -> ExecutorObservation:
        observed = mask_observation(observed, self._private_paths)
        results: list[EvaluationStepResult] = []
        failed_checks: list[tuple[str | None, EvidenceKind]] = []
        operator_failed_checks: list[tuple[str | None, EvidenceKind]] = []
        operator_observed_failure = (
            None
            if observed.failure is None
            else observed.operator_failure
            if observed.operator_failure is not None
            else redact_diagnostic_tail(observed.failure, max_chars=MAX_EVIDENCE_SUMMARY_CHARS)
        )
        metadata = (
            SlurmCommandResult.model_validate(observed.stage_results[0].result).execution_metadata
            if observed.stage_results and observed.stage_results[0].result is not None
            else None
        )
        infrastructure_failure = metadata is None or metadata.aggregate_unknown is not None
        for step, raw_step in zip(request.stages, observed.stage_results, strict=False):
            if raw_step.result is None:
                operator_step_failure = (
                    None
                    if raw_step.failure is None
                    else raw_step.operator_failure
                    if raw_step.operator_failure is not None
                    else redact_diagnostic_tail(
                        raw_step.failure, max_chars=MAX_EVIDENCE_SUMMARY_CHARS
                    )
                )
                results.append(
                    raw_step.model_copy(
                        update={
                            "name": step.name,
                            "operator_failure": operator_step_failure,
                            "service_log_tail": _redacted_service_log(raw_step),
                        }
                    )
                )
                continue
            stage = SemanticEvaluationStage.model_validate(step.payload)
            raw = SlurmCommandResult.model_validate(raw_step.result)
            completed = _completed(raw, metadata)
            evidence, operator_evidence_summary = self._evidence(
                stage,
                raw,
                raw_step,
                handle_id=handle_id,
                metadata=metadata,
            )
            # A job that exited cleanly can still hold a benchmark whose evaluator died
            # before writing its result, so the evidence's own kind counts too. An
            # ambiguous stage ends the job as failed as well, so that core may measure it
            # once more, but its evidence stays: it is the last tail if it happens again.
            completed &= not is_unsettled(evidence.failure_kind)
            infrastructure_failure |= not completed
            operator_step_failure = (
                None
                if raw_step.failure is None
                else raw_step.operator_failure
                if raw_step.operator_failure is not None
                else redact_diagnostic_tail(raw_step.failure, max_chars=MAX_EVIDENCE_SUMMARY_CHARS)
            )
            if evidence.outcome is EvidenceOutcome.FAILED:
                failed_checks.append((evidence.semantic_summary, stage.kind))
                operator_failed_checks.append(
                    (
                        operator_evidence_summary,
                        stage.kind,
                    )
                )
            results.append(
                EvaluationStepResult(
                    name=step.name,
                    state=StageState.SUCCEEDED if completed else StageState.FAILED,
                    result=evidence.model_dump(mode="json"),
                    operator_summary=operator_evidence_summary,
                    duration_s=raw_step.duration_s,
                    host=raw_step.host,
                    stdout_tail=(
                        redact_diagnostic_tail(raw.stdout, max_chars=STAGE_OUTPUT_TAIL_CHARS)
                        or None
                    ),
                    stderr_tail=(
                        redact_diagnostic_tail(raw.stderr, max_chars=STAGE_OUTPUT_TAIL_CHARS)
                        or None
                    ),
                    service_log_tail=None if completed else _redacted_service_log(raw_step),
                    failure_kind=None if completed else _stage_failure_kind(evidence),
                    failure=(
                        None
                        if completed
                        else raw_step.failure or observed.failure or evidence.semantic_summary
                    ),
                    operator_failure=(
                        None
                        if completed
                        else _first_defined(
                            operator_step_failure,
                            operator_observed_failure,
                            operator_evidence_summary,
                            f"{stage.kind.value} command failed",
                        )
                    ),
                )
            )
        has_semantic_result = any(item.result is not None for item in results)
        skipped = any(item.state is StageState.SKIPPED for item in results)
        failure = observed.failure
        operator_failure = operator_observed_failure
        if infrastructure_failure and observed.state in {
            EvaluationState.SUCCEEDED,
            EvaluationState.FAILED,
        }:
            state = EvaluationState.FAILED
            failure = self._stage_failure_text(failed_checks, observed.failure)
            operator_failure = redact_diagnostic_tail(
                self._stage_failure_text(operator_failed_checks, operator_observed_failure),
                max_chars=MAX_EVIDENCE_SUMMARY_CHARS,
            )
        elif observed.state is EvaluationState.FAILED and has_semantic_result and not skipped:
            state = EvaluationState.SUCCEEDED
        elif skipped and has_semantic_result:
            # A successful evaluation must complete every planned stage. A failed
            # stage that skipped the rest makes the evaluation failed, and its
            # diagnostics are the failure the submitting agent reads.
            state = EvaluationState.FAILED
            failure = self._stage_failure_text(failed_checks, observed.failure)
            operator_failure = redact_diagnostic_tail(
                self._stage_failure_text(operator_failed_checks, operator_observed_failure),
                max_chars=MAX_EVIDENCE_SUMMARY_CHARS,
            )
        else:
            state = observed.state
        return ExecutorObservation(
            state=state,
            current_stage=None if is_finished(state) else observed.current_stage,
            stage_results=tuple(results),
            failure=None if state is EvaluationState.SUCCEEDED else failure,
            operator_failure=None if state is EvaluationState.SUCCEEDED else operator_failure,
            run_environment_unavailable=state is EvaluationState.FAILED
            and observed.run_environment_unavailable,
        )

    def _evidence(
        self,
        stage: SemanticEvaluationStage,
        raw: SlurmCommandResult,
        raw_step: EvaluationStepResult,
        *,
        handle_id: str,
        metadata: SlurmExecutionMetadata | None,
    ) -> tuple[TrustedEvidence, str | None]:
        failure = raw_step.failure
        service_log_tail = raw_step.service_log_tail
        passed = _completed(raw, metadata) and raw.exit_code == 0
        service_not_ready = metadata is not None and metadata.service_not_ready
        metrics: tuple[EvidenceMetric, ...] = ()
        summary: str | None = None
        operator_summary: str | None = None
        partial: PartialMeasurement | None = None
        unmet: str | None = None
        # Whose fault a failure is follows from how the stage ended and what its evaluator
        # wrote (`classify`). Only a benchmark has a result record to read; every other
        # stage's exit status is its whole outcome.
        ran = raw.executed and raw.exit_code is not None and raw.collection_failure is None
        signal = signal_of(
            raw.exit_code if ran else None,
            service_not_ready=service_not_ready,
            service_log=raw.output,
            candidate_startup_causes=self._trusted_plan.candidate_startup_causes,
        )
        record = RecordState.OUTCOME
        contract = self._trusted_plan.benchmark_contract
        if stage.kind is EvidenceKind.BENCHMARK and contract is not None:
            decoded = decode_trusted_benchmark_run(
                raw.output, contract, frozenset(), exited_cleanly=passed
            )
            partial = decoded.partial
            unmet = decoded.requirement
            # The evaluator's own stop reason leads, as on the local executor;
            # the raw output tail would end with its command line instead.
            summary = decoded.reason
            operator_summary = (
                None
                if decoded.reason is None
                else redact_diagnostic_tail(decoded.reason, max_chars=MAX_EVIDENCE_SUMMARY_CHARS)
            )
            if decoded.violation is not None:
                # A clean exit with no valid record still leaves the output the evaluator
                # printed before it stopped, which is all that says what happened.
                failure = f"{failure or raw.output}\n{decoded.violation}"
                if passed:
                    summary = failure
                    operator_summary = redact_diagnostic_tail(
                        failure, max_chars=MAX_EVIDENCE_SUMMARY_CHARS
                    )
            passed = decoded.passed
            record = decoded.record
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
            # holds the attribution tables. Its typed statistics line is evidence
            # metrics, not summary text.
            try:
                text, stats = split_profile_stats(raw.stdout or raw.output)
            except ValueError as exc:
                passed = False
                failure = str(exc)
            else:
                summary = text[-MAX_EVIDENCE_SUMMARY_CHARS:] or None
                operator_summary = (
                    redact_diagnostic_tail(text, max_chars=MAX_EVIDENCE_SUMMARY_CHARS) or None
                )
                metrics = () if stats is None else stats.metrics()
        if not passed and summary is None:
            # The stage's own output ends the text, where its verdict is and where the
            # agent's excerpt cuts; the capped service log precedes it.
            detail = render_stage_output_with_service_log(
                failure or raw.output, (service_log_tail or "")[-SERVICE_LOG_SUMMARY_CHARS:]
            )
            summary = detail[-MAX_EVIDENCE_SUMMARY_CHARS:] or f"{stage.kind.value} command failed"
            operator_summary = (
                redact_diagnostic_tail(detail, max_chars=MAX_EVIDENCE_SUMMARY_CHARS)
                or f"{stage.kind.value} command failed"
            )
        outcome = EvidenceOutcome.PASSED if passed else EvidenceOutcome.FAILED
        if summary is not None:
            summary = summary[-MAX_EVIDENCE_SUMMARY_CHARS:]
        if operator_summary is not None:
            operator_summary = operator_summary[-MAX_EVIDENCE_SUMMARY_CHARS:]
        evidence_id = evidence_identity(
            stage,
            EvidenceResultIdentity(
                evaluation_id=handle_id,
                outcome=outcome,
                summary=summary,
                metrics=metrics,
                partial=partial,
                unmet_requirement=unmet,
            ),
        )
        return TrustedEvidence(
            evidence_id=evidence_id,
            evaluation_id=handle_id,
            stage_name=stage.kind.value,
            kind=stage.kind,
            fingerprints=stage.fingerprints,
            trusted_inputs=stage.fingerprints.candidate,
            outcome=outcome,
            semantic_summary=summary,
            metrics=metrics,
            partial_measurement=partial,
            failure_kind=None if passed else classify(signal, record),
            unmet_requirement=None if passed else unmet,
            accepted_round=0,
        ), operator_summary

    def _load(self, handle_id: str) -> _DurableSemanticSubmission | None:
        return self._namespace.load_optional(self._path(handle_id), _DurableSemanticSubmission)

    @staticmethod
    def _path(handle_id: str) -> str:
        return f"slurm-semantic/{handle_id}.json"


__all__ = ["SemanticSlurmEvaluationExecutor", "StageFailureText"]
