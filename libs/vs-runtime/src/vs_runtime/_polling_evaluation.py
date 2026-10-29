"""In-process semantic evaluation executor for runs without a Slurm cluster.

It is the sibling of ``SemanticSlurmEvaluationExecutor``: both implement the
``PollingEvaluationExecutor`` protocol plus ``close``, so the evaluation service and
core bindings accept either. This one runs stages as tasks of the current
process and reports state through ``inspect``/``wait_for_change``.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_agent.api import AgentExecutionCancelledError
from vs_evaluation.api import (
    MAX_EVIDENCE_SUMMARY_CHARS,
    AvailabilitySnapshot,
    AvailabilityState,
    CostClass,
    EvaluationRequest,
    EvaluationState,
    EvaluationStepResult,
    EvidenceKind,
    EvidenceMetric,
    EvidenceOutcome,
    EvidenceResultIdentity,
    ExecutorCancellationUnknownError,
    ExecutorObservation,
    ExecutorPoll,
    LifecyclePublisher,
    PollPhase,
    ResourceRequirements,
    ReuseStatus,
    SemanticEvaluationStage,
    StageFailureKind,
    StageState,
    TrustedEvidence,
    evidence_identity,
    is_finished,
)
from vs_redaction.api import redact_diagnostic_tail
from vs_runtime._evaluation_failure_text import render_evaluation_failure, render_stage_failure
from vs_runtime._evaluation_recovery import (
    EvaluationReceiptUnconfirmedError,
    EvaluationRecoveryBlockedError,
)
from vs_runtime._failure_classification import is_unsettled
from vs_runtime._measurement_progress import StageRun
from vs_sim.api import DISCARD

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from vs_runtime._evaluation_recovery import LocalEvaluationReceipts
    from vs_runtime._measurement_progress import StageOutputOpener
    from vs_runtime._run_control import RunControlChannel
    from vs_runtime.contracts import CandidateWorkspace, Evaluation, Workspace, Workspaces
    from vs_sim.api import Clock, OutputSink


_GATING_KINDS = frozenset({EvidenceKind.BUILD, EvidenceKind.ACCURACY})
"""Stages whose failed outcome skips the stages after them: nothing later can count."""


@dataclass(frozen=True, slots=True)
class _SemanticStageObservation:
    evidence: TrustedEvidence
    completed: bool
    operator_summary: str | None


class PollingEvaluationExecutor:
    """Run semantic evaluation requests in-process, publishing observations a caller polls.

    Each submitted request evaluates its candidate snapshot in a fresh candidate
    workspace through the trusted ``Evaluation`` effects, one stage at a time.
    Requests run one at a time, as ``availability`` reports (capacity 1): every request
    executes on this host's one set of devices, so a second request stays queued until
    the first has ended and released its workspace.
    Optional receipts retain evidence and cleanup obligations for fresh replay owners.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-341001 [PLR0913]; receipts, execution effects, clock, output and control are independently owned interfaces; a carrier would couple unrelated owners and a mutable setter could enable durability after dispatch.
        self,
        evaluation: Evaluation,
        workspaces: Workspaces,
        *,
        clock: Clock,
        stage_output: StageOutputOpener | None = None,
        control: RunControlChannel | None = None,
        receipts: LocalEvaluationReceipts | None = None,
    ) -> None:
        """Evaluate through ``evaluation`` in candidate workspaces from ``workspaces``.

        ``clock`` stamps availability observations. ``stage_output`` opens the stream
        each stage's command writes its raw output to while it runs; ``None`` discards it.
        """
        self._receipts = receipts
        self._control = control
        self._clock = clock
        self._stage_output = stage_output
        self._evaluation = evaluation
        self._workspaces = workspaces
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lifecycle = LifecyclePublisher()
        self._admission = asyncio.Lock()

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        del requirements
        active = sum(not task.done() for task in self._tasks.values())
        return AvailabilitySnapshot(
            state=AvailabilityState.IMMEDIATE if active == 0 else AvailabilityState.BUSY,
            capacity=1,
            in_flight=active,
            queue_depth=max(0, active - 1),
            reuse_status=ReuseStatus.UNKNOWN,
            cost_class=CostClass.UNKNOWN,
            observed_at=self._clock.now(),
            fresh_for_s=1.0,
            supported_evidence_kinds=(
                EvidenceKind.BUILD.value,
                EvidenceKind.ACCURACY.value,
                EvidenceKind.BENCHMARK.value,
            ),
        )

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        if self._receipts is not None:
            first = SemanticEvaluationStage.model_validate(request.stages[0].payload)
            if not self._receipts.begin(
                handle_id, request, first.snapshot, str(self._workspaces.root.path)
            ):
                await self.recover(handle_id)
                return
        if handle_id in self._tasks or self._lifecycle.observation(handle_id) is not None:
            return
        self._publish(handle_id, ExecutorObservation(state=EvaluationState.QUEUED))
        self._tasks[handle_id] = asyncio.create_task(self._admitted(handle_id, request))

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        """Read process-local evidence without starting recovery tasks."""
        observed = self._lifecycle.observation(handle_id)
        task = self._tasks.get(handle_id)
        if self._receipts is not None and (task is None or task.done()):
            return self._receipts.observation(handle_id)
        return observed

    async def poll(self, handle_id: str) -> ExecutorPoll:
        """Inspect once from process-local state; never submits, cancels or creates a workspace."""
        observed = await self.inspect_only(handle_id)
        if observed is None:
            return ExecutorPoll(phase=PollPhase.UNSUBMITTED)
        if is_finished(observed.state):
            return ExecutorPoll(phase=PollPhase.ENDED, terminal=observed)
        phase = PollPhase.QUEUED if observed.state is EvaluationState.QUEUED else PollPhase.RUNNING
        return ExecutorPoll(phase=phase, current_stage=observed.current_stage)

    async def recover(self, handle_id: str) -> ExecutorPoll:
        """An empty new process cannot prove that an earlier delivery never began."""
        if await self.inspect_only(handle_id) is None:
            if self._receipts is not None:
                raise self._receipts.blocked(handle_id)
            return ExecutorPoll(
                phase=PollPhase.UNKNOWN,
                detail="the previous local execution is not present in this process",
            )
        return await self.poll(handle_id)

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        return await self.inspect_only(handle_id)

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        if handle_id not in self._tasks and self._receipts is not None:
            await self.recover(handle_id)
            return
        await self._lifecycle.wait_for_change(handle_id, timeout_s)

    async def cancel(self, handle_id: str) -> None:
        task = self._tasks.get(handle_id)
        if self._receipts is not None and (task is None or task.done()):
            observed = await self.inspect_only(handle_id)
            if observed is None:
                raise ExecutorCancellationUnknownError(handle_id)
            return
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        observed = self._lifecycle.observation(handle_id)
        self._publish(
            handle_id,
            ExecutorObservation(
                state=EvaluationState.CANCELED,
                stage_results=observed.stage_results if observed is not None else (),
            ),
        )

    async def close(self) -> None:
        # Through cancel(), not task.cancel(): a task cancelled before its first step
        # never runs its own handler, so only cancel() ends it in a published state.
        active = tuple(handle_id for handle_id, task in self._tasks.items() if not task.done())
        await asyncio.gather(*(self.cancel(handle_id) for handle_id in active))
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _admitted(self, handle_id: str, request: EvaluationRequest) -> None:
        """Run ``request`` once no other request holds the host; it stays queued until then."""
        async with self._admission:
            await self._run(handle_id, request)

    async def _run(self, handle_id: str, request: EvaluationRequest) -> None:
        first = SemanticEvaluationStage.model_validate(request.stages[0].payload)
        workspace: CandidateWorkspace | None = None
        results: list[EvaluationStepResult] = []
        failure: str | None = None
        operator_failure: str | None = None
        interrupted = False
        try:
            workspace = await self._acquire_workspace(handle_id, first.snapshot)

            self._execution_started(handle_id, request)
            for step in request.stages:
                stage = SemanticEvaluationStage.model_validate(step.payload)
                self._stage_started(handle_id, step.name)
                with self._open_output(
                    StageRun(handle_id, step.name, purpose=stage.purpose)
                ) as output:
                    observed = await self._evaluate(workspace, stage, handle_id, output)
                evidence = observed.evidence
                results.append(
                    EvaluationStepResult(
                        name=step.name,
                        state=StageState.SUCCEEDED if observed.completed else StageState.FAILED,
                        result=evidence.model_dump(mode="json"),
                        operator_summary=observed.operator_summary,
                        failure_kind=None if observed.completed else StageFailureKind.EXECUTION,
                        failure=(
                            None
                            if observed.completed
                            else render_stage_failure(
                                ((evidence.semantic_summary, evidence.kind),), None
                            )
                        ),
                        operator_failure=(
                            None
                            if observed.completed
                            else render_stage_failure(
                                ((observed.operator_summary, evidence.kind),), None
                            )
                        ),
                    )
                )
                self._record_observation(
                    handle_id,
                    ExecutorObservation(
                        state=EvaluationState.RUNNING,
                        current_stage=step.name,
                        stage_results=tuple(results),
                    ),
                )
                if not observed.completed or (
                    evidence.kind in _GATING_KINDS and evidence.outcome is EvidenceOutcome.FAILED
                ):
                    skipped = request.stages[len(results) :]
                    results.extend(
                        EvaluationStepResult(name=remaining.name, state=StageState.SKIPPED)
                        for remaining in skipped
                    )
                    if not observed.completed or skipped:
                        # Infrastructure failure or skipped stages prevent completion.
                        # Preserve diagnostics for the submitting agent.
                        failure = render_stage_failure(
                            ((evidence.semantic_summary, evidence.kind),), None
                        )
                        operator_failure = render_stage_failure(
                            ((observed.operator_summary, evidence.kind),), None
                        )
                    break
                if len(results) < len(request.stages):
                    # A waiting agent sees each finished stage and the one now running.
                    self._publish(
                        handle_id,
                        ExecutorObservation(
                            state=EvaluationState.RUNNING,
                            current_stage=request.stages[len(results)].name,
                            stage_results=tuple(results),
                        ),
                    )
            self._publish(
                handle_id,
                ExecutorObservation(
                    state=EvaluationState.SUCCEEDED if failure is None else EvaluationState.FAILED,
                    stage_results=tuple(results),
                    failure=failure,
                    operator_failure=operator_failure,
                ),
            )
        except (EvaluationReceiptUnconfirmedError, EvaluationRecoveryBlockedError):
            interrupted = True
            raise
        except (asyncio.CancelledError, AgentExecutionCancelledError) as cancelled:
            self._publish(
                handle_id,
                ExecutorObservation(
                    state=EvaluationState.CANCELED,
                    stage_results=tuple(results),
                ),
            )
            raise asyncio.CancelledError from cancelled
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-930049 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
            raw_failure = render_evaluation_failure(str(error) or None, type(error).__name__)
            self._publish(
                handle_id,
                ExecutorObservation(
                    state=EvaluationState.FAILED,
                    stage_results=tuple(results),
                    # Extensions may raise an exception without a message.
                    # Its type is still a failure fact; the existing template
                    # guarantees the terminal diagnostic stays nonempty.
                    failure=raw_failure,
                    operator_failure=redact_diagnostic_tail(
                        raw_failure,
                        max_chars=MAX_EVIDENCE_SUMMARY_CHARS,
                    ),
                ),
            )
        finally:
            if not interrupted:
                await self._cleanup(handle_id, workspace)

    def _execution_started(self, handle_id: str, request: EvaluationRequest) -> None:
        def started() -> None:
            self._publish(
                handle_id,
                ExecutorObservation(
                    state=EvaluationState.RUNNING, current_stage=request.stages[0].name
                ),
            )

        if self._control is None:
            started()
        else:
            self._control.start_execution(started, evaluation_id=request.key)

    async def _acquire_workspace(self, handle_id: str, snapshot: str) -> CandidateWorkspace:
        held = (
            nullcontext()
            if self._receipts is None
            else self._receipts.acquisition(
                handle_id, str(self._workspaces.root.path), tuple(self._tasks)
            )
        )
        with held:
            if self._receipts is not None:
                self._receipts.update(handle_id, cleanup_required=True)
            workspace = await self._workspaces.create_measured_candidate(snapshot)
            if self._receipts is not None:
                self._receipts.update(handle_id, workspace=str(workspace.path))
        return workspace

    def _stage_started(self, handle_id: str, name: str) -> None:
        if self._receipts is not None:
            self._receipts.start_stage(handle_id, name)

    async def _cleanup(self, handle_id: str, workspace: CandidateWorkspace | None) -> None:
        if self._receipts is not None:
            self._receipts.start_cleanup(handle_id)
        if workspace is not None:
            await workspace.discard()
        if self._receipts is not None:
            receipt = self._receipts.load(handle_id)
            if workspace is None and receipt is not None and receipt.cleanup_required:
                raise self._receipts.blocked(handle_id)
            self._receipts.update(handle_id, cleaned=True)
            observed = self._receipts.observation(handle_id)
            if observed is not None:
                self._lifecycle.publish(handle_id, observed)

    def _open_output(self, run: StageRun) -> AbstractContextManager[OutputSink]:
        if self._stage_output is None:
            return nullcontext(DISCARD)
        return self._stage_output(run)

    async def _evaluate(
        self,
        workspace: Workspace,
        stage: SemanticEvaluationStage,
        handle_id: str,
        output: OutputSink,
    ) -> _SemanticStageObservation:
        unmet: str | None = None
        if stage.kind is EvidenceKind.BUILD:
            built = await self._evaluation.build(workspace, output=output)
            completed = not is_unsettled(built.failure_kind)
            outcome = (
                EvidenceOutcome.PASSED if completed and built.passed else EvidenceOutcome.FAILED
            )
            summary = built.feedback or ("reused this commit's build" if built.reused else None)
            operator_summary = built.operator_feedback or summary
            metrics: tuple[EvidenceMetric, ...] = ()
            partial = None
            kind = built.failure_kind
        elif stage.kind is EvidenceKind.ACCURACY:
            result = await self._evaluation.accuracy(workspace, output=output)
            completed = not is_unsettled(result.failure_kind)
            outcome = (
                EvidenceOutcome.PASSED if completed and result.passed else EvidenceOutcome.FAILED
            )
            summary = result.feedback
            operator_summary = result.operator_feedback
            metrics = ()
            partial = None
            kind = result.failure_kind
        elif stage.kind is EvidenceKind.BENCHMARK:
            result = await self._evaluation.benchmark(workspace, output=output)
            completed = not is_unsettled(result.failure_kind)
            kind = result.failure_kind
            outcome = (
                EvidenceOutcome.PASSED if completed and result.passed else EvidenceOutcome.FAILED
            )
            summary = result.feedback
            operator_summary = result.operator_feedback
            partial = result.partial_measurement
            unmet = result.unmet_requirement
            metrics = tuple(
                EvidenceMetric(
                    name=name,
                    value=value,
                    direction=(
                        result.metric_direction.value
                        if name == result.metric_name and result.metric_direction is not None
                        else None
                    ),
                    unit=result.metric_unit if name == result.metric_name else None,
                )
                for name, value in sorted((result.row or {}).items())
            )
        else:
            message = "direct profile evaluation is not supported by the trusted runtime"
            raise ValueError(message)
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
        evidence = TrustedEvidence(
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
            failure_kind=None if outcome is EvidenceOutcome.PASSED else kind,
            unmet_requirement=None if outcome is EvidenceOutcome.PASSED else unmet,
            accepted_round=0,
        )
        return _SemanticStageObservation(
            evidence=evidence,
            completed=completed,
            operator_summary=operator_summary,
        )

    def _record_observation(self, handle_id: str, observation: ExecutorObservation) -> None:
        if self._receipts is not None:
            self._receipts.observe(handle_id, observation)

    def _publish(self, handle_id: str, observation: ExecutorObservation) -> None:
        self._record_observation(handle_id, observation)
        if self._receipts is not None and is_finished(observation.state):
            receipt = self._receipts.load(handle_id)
            if receipt is not None and not receipt.cleanup_required:
                self._receipts.update(handle_id, cleaned=True)
            else:
                return
        self._lifecycle.publish(handle_id, observation)
