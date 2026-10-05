"""In-process semantic evaluation executor for runs without a Slurm cluster.

It is the sibling of ``SemanticSlurmEvaluationExecutor``: both implement the
``EvaluationExecutor`` protocol plus ``close``, so the evaluation service and
core bindings accept either. This one runs stages as tasks of the current
process and reports state through ``inspect``/``wait_for_change``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    ExecutorObservation,
    ResourceRequirements,
    ReuseStatus,
    SemanticEvaluationStage,
    StageFailureKind,
    StageState,
    TrustedEvidence,
    evidence_identity,
)
from vs_runtime._evaluation_failure_text import render_evaluation_failure, render_stage_failure
from vs_runtime.contracts import BenchmarkFailureKind

if TYPE_CHECKING:
    from vs_runtime.contracts import CandidateWorkspace, Evaluation, Workspace, Workspaces


@dataclass(frozen=True, slots=True)
class _SemanticStageObservation:
    evidence: TrustedEvidence
    completed: bool


class PollingEvaluationExecutor:
    """Run semantic evaluation requests in-process, publishing observations a caller polls.

    Each submitted request evaluates its candidate snapshot in a fresh candidate
    workspace through the trusted ``Evaluation`` effects, one stage at a time.
    Observations are process-local: nothing survives a restart.
    """

    def __init__(self, evaluation: Evaluation, workspaces: Workspaces) -> None:
        """Evaluate through ``evaluation`` in candidate workspaces from ``workspaces``."""
        self._evaluation = evaluation
        self._workspaces = workspaces
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._observations: dict[str, ExecutorObservation] = {}
        self._changes: dict[str, asyncio.Event] = {}

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
            observed_at=time.monotonic(),
            fresh_for_s=1.0,
            supported_evidence_kinds=(EvidenceKind.ACCURACY.value, EvidenceKind.BENCHMARK.value),
        )

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        if handle_id in self._tasks or handle_id in self._observations:
            return
        self._publish(handle_id, ExecutorObservation(state=EvaluationState.QUEUED))
        self._tasks[handle_id] = asyncio.create_task(self._run(handle_id, request))

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        """Read process-local evidence without starting recovery tasks."""
        return self._observations.get(handle_id)

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        return self._observations.get(handle_id)

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        event = self._changes.setdefault(handle_id, asyncio.Event())
        if event.is_set():
            event.clear()
            return
        try:
            await asyncio.wait_for(event.wait(), timeout_s)
        except TimeoutError:
            return
        event.clear()

    async def cancel(self, handle_id: str) -> None:
        task = self._tasks.get(handle_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        observed = self._observations.get(handle_id)
        self._publish(
            handle_id,
            ExecutorObservation(
                state=EvaluationState.CANCELED,
                stage_results=observed.stage_results if observed is not None else (),
            ),
        )

    async def close(self) -> None:
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, handle_id: str, request: EvaluationRequest) -> None:
        first = SemanticEvaluationStage.model_validate(request.stages[0].payload)
        workspace: CandidateWorkspace | None = None
        results: list[EvaluationStepResult] = []
        failure: str | None = None
        try:
            workspace = await self._workspaces.create_candidate(first.snapshot)
            self._publish(
                handle_id,
                ExecutorObservation(
                    state=EvaluationState.RUNNING, current_stage=request.stages[0].name
                ),
            )
            for step in request.stages:
                stage = SemanticEvaluationStage.model_validate(step.payload)
                observed = await self._evaluate(workspace, stage, handle_id)
                evidence = observed.evidence
                results.append(
                    EvaluationStepResult(
                        name=step.name,
                        state=StageState.SUCCEEDED if observed.completed else StageState.FAILED,
                        result=evidence.model_dump(mode="json"),
                        failure_kind=None if observed.completed else StageFailureKind.EXECUTION,
                        failure=(
                            None
                            if observed.completed
                            else render_stage_failure(
                                ((evidence.semantic_summary, evidence.kind),), None
                            )
                        ),
                    )
                )
                if not observed.completed or (
                    evidence.kind is EvidenceKind.ACCURACY
                    and evidence.outcome is EvidenceOutcome.FAILED
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
                ),
            )
        except asyncio.CancelledError:
            self._publish(
                handle_id,
                ExecutorObservation(
                    state=EvaluationState.CANCELED,
                    stage_results=tuple(results),
                ),
            )
            raise
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-930049 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
            self._publish(
                handle_id,
                ExecutorObservation(
                    state=EvaluationState.FAILED,
                    stage_results=tuple(results),
                    # Extensions may raise an exception without a message.
                    # Its type is still a failure fact; the existing template
                    # guarantees the terminal diagnostic stays nonempty.
                    failure=render_evaluation_failure(str(error) or None, type(error).__name__),
                ),
            )
        finally:
            if workspace is not None:
                await workspace.discard()

    async def _evaluate(
        self, workspace: Workspace, stage: SemanticEvaluationStage, handle_id: str
    ) -> _SemanticStageObservation:
        completed = True
        if stage.kind is EvidenceKind.ACCURACY:
            result = await self._evaluation.accuracy(workspace)
            outcome = EvidenceOutcome.PASSED if result.passed else EvidenceOutcome.FAILED
            summary = result.feedback
            metrics: tuple[EvidenceMetric, ...] = ()
            partial = None
        elif stage.kind is EvidenceKind.BENCHMARK:
            result = await self._evaluation.benchmark(workspace)
            completed = result.failure_kind is not BenchmarkFailureKind.INFRASTRUCTURE
            outcome = (
                EvidenceOutcome.PASSED if completed and result.passed else EvidenceOutcome.FAILED
            )
            summary = result.feedback
            partial = result.partial_measurement
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
        evidence_id = evidence_identity(
            stage,
            EvidenceResultIdentity(
                evaluation_id=handle_id,
                outcome=outcome,
                summary=summary,
                metrics=metrics,
                partial=partial,
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
            accepted_round=0,
        )
        return _SemanticStageObservation(evidence=evidence, completed=completed)

    def _publish(self, handle_id: str, observation: ExecutorObservation) -> None:
        self._observations[handle_id] = observation
        self._changes.setdefault(handle_id, asyncio.Event()).set()
