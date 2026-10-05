"""Scripted executors: what the planner, implementer, judge and evaluators answer.

An executor reports what it saw (a job accepted, a stage passed, a reply) and
never core's conclusions. Evidence identities are derived from the job's plan, so
a reading and the evidence it decodes always agree.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic.strategy._run import FACTS

from vibesys.orchestration.dynamic.strategy.api import (
    EvidenceReading,
    EvidenceReadings,
    InterpretEvidence,
    MetricRow,
    ParentVerification,
    PartialRow,
    RenderedArtifacts,
    RenderRoleArtifacts,
    VerifyParentRevision,
    dynamic_operation_registry,
)
from vs_core.api import (
    AdoptRevision,
    AgentMeasurementRequested,
    ArtifactId,
    ArtifactRef,
    CloseSession,
    CoreEvent,
    DispatchTurn,
    EnsureSession,
    EnsureWorkspace,
    EvaluationStageOutcome,
    EvaluationStageResult,
    EvaluationTerminalFacts,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    ExecuteRegisteredOperation,
    MeasurementPlan,
    ObservationStatus,
    ObserveOwnedJob,
    OwnedJob,
    Request,
    RequestId,
    ResourceId,
    RetainRevision,
    RevisionRef,
    Scope,
    SessionId,
    SnapshotAndRetain,
    SubmitMeasurement,
    VerifyAdoption,
)
from vs_core.testing.drive import Answer, Failed, Running, Succeeded, Unknown
from vs_runtime.api.core import AgentEvaluationPolicy

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreState, Transition

_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
    "profile": EvidenceKind.PROFILING,
}


def _lease(session_id: SessionId) -> ResourceId:
    """The lease an executor holds for a session: the same name from ensure through close."""
    return ResourceId(root=f"lease:{session_id.root}")


@dataclass
class Executors:
    """Scripted answers for one run, popped in the order requests are authorized."""

    planner: deque[str] = field(default_factory=deque)
    implementer: deque[str] = field(default_factory=deque)
    judge: deque[str] = field(default_factory=deque)
    # Throughput per candidate commit; None makes the benchmark fail.
    benchmark: Callable[[str], float | None] = lambda _commit: 100.0
    accuracy: Callable[[str], bool] = lambda _commit: True
    # None makes the input's benchmark fail, as a workload the input cannot run does.
    baseline_value: float | None = 50.0
    # True for a measurement whose job ends without any evidence (its infrastructure failed).
    infrastructure_failure: Callable[[MeasurementPlan], bool] = lambda _plan: False
    parent_verified: Callable[[str], bool] = lambda _commit: True
    submit: Callable[[SubmitMeasurement], Answer] | None = None
    # True for an implementer that measures its workspace through the evaluation tool during
    # its turn: the production bridge's `AgentMeasurementRequested`, admitted by core.
    agent_evaluation: bool = False
    admit: Callable[[CoreEvent, float], Transition] | None = None
    jobs: int = 0
    agent_calls: int = 0
    now_at: float = 0.0
    seen: list[Request] = field(default_factory=list)
    readings: dict[EvidenceId, EvidenceReading] = field(default_factory=dict)

    def __call__(self, request: Request, core: CoreState) -> Answer:
        self.seen.append(request)
        self.now_at = core.run.now_at
        if isinstance(request, ObserveOwnedJob):
            return self._observe(request, core)
        return self._answer(request)

    def _answer(self, request: Request) -> Answer:
        """What an executor that needs no view of core reports for ``request``."""
        answer: Answer = Succeeded()
        match request:
            case ExecuteRegisteredOperation():
                answer = self._operation(request)
            case SubmitMeasurement():
                answer = self._submit(request)
            case DispatchTurn():
                answer = self._turn(request)
            case EnsureWorkspace() | EnsureSession():
                answer = self._ensure(request)
            case CloseSession():
                answer = Succeeded(resource_id=_lease(request.session_id))
            case SnapshotAndRetain():
                answer = self._retain(request)
            case RetainRevision():
                answer = Succeeded(revision=request.revision)
            case AdoptRevision() | VerifyAdoption():
                answer = Succeeded(revision=request.selection.revision)
        return answer

    @staticmethod
    def _retained(attempt: str) -> RevisionRef:
        """The commit the workspace executor retains for an attempt, one per attempt."""
        return RevisionRef.of_git_commit(hashlib.sha256(attempt.encode()).hexdigest()[:40])

    def _retain(self, request: SnapshotAndRetain) -> Answer:
        return Succeeded(revision=self._retained(request.attempt.attempt_id.root))

    def _finished_agent_evaluation(self, request: SubmitMeasurement) -> Answer:
        """An agent's evaluation that ends as soon as it is accepted, with trusted evidence."""
        assert isinstance(request.plan.candidate, RevisionRef)
        assert request.request_id is not None
        commit = request.plan.candidate.revision_id.root
        refs: list[EvidenceRef] = []
        stages: list[EvaluationStageResult] = []
        for stage in request.plan.stages:
            reading = self._reading(
                request.plan, _KINDS[stage.stage_id], stage.stage_id, commit
            ).model_copy(update={"kind": EvidenceKind.LOCAL_VALIDATION})
            self.readings[reading.evidence_id] = reading
            refs.append(self._ref(request.scope, request.request_id, request.plan, reading))
            outcome = (
                EvaluationStageOutcome.PASSED if reading.passed else EvaluationStageOutcome.FAILED
            )
            stages.append(EvaluationStageResult(stage_id=stage.stage_id, outcome=outcome))
        self.jobs += 1
        return Succeeded(
            resource_id=ResourceId(root=f"job:{self.jobs}"),
            evidence=tuple(refs),
            facts=EvaluationTerminalFacts(
                stages=tuple(stages), accuracy_passed=all(i.passed for i in self.readings.values())
            ),
        )

    @staticmethod
    def _ensure(request: EnsureWorkspace | EnsureSession) -> Answer:
        """A workspace or session lease the executor acquired, named by the request."""
        if isinstance(request, EnsureWorkspace):
            return Succeeded(
                resource_id=ResourceId(
                    root=f"workspace:{request.scope.owner.root}:{request.attempt.generation}"
                ),
                revision=request.plan.base,
            )
        return Succeeded(resource_id=_lease(request.spec.session_id))

    def _evaluate_from_turn(self, request: DispatchTurn) -> None:
        """The implementer measures the revision core will retain for its attempt."""
        assert self.admit is not None, "the shell must attach its admission before a run"
        facts = FACTS
        self.agent_calls += 1
        candidate = self._retained(request.scope.owner.root)
        policy = AgentEvaluationPolicy(
            evaluator_digest=facts.evaluator_digest,
            workload_digest=facts.workload_digest,
            environment_digest=facts.environment_digest,
            recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
            stages=(("accuracy", 60.0), ("benchmark", 60.0)),
            queue_allowance=10.0,
            accuracy_stage="accuracy",
        )
        now = self.now_at
        event = AgentMeasurementRequested(
            scope=request.scope,
            plan=policy.plan(candidate, now_at=now),
            call_id=f"agent-call-{self.agent_calls}",
        )
        self.admit(event, now)

    def _submit(self, request: SubmitMeasurement) -> Answer:
        if request.plan.purpose == "local-validation":
            return self._finished_agent_evaluation(request)
        if self.submit is not None:
            return self.submit(request)
        self.jobs += 1
        return Running(resource_id=ResourceId(root=f"job:{self.jobs}"))

    # -- agents -----------------------------------------------------------

    def _turn(self, request: DispatchTurn) -> Answer:
        role = request.turn.session.role_id.root.removeprefix("dynamic-")
        queue = {
            "orchestrator": self.planner,
            "implementer": self.implementer,
            "judge": self.judge,
        }[role]
        if not queue:
            return Unknown()
        if role == "implementer" and self.agent_evaluation:
            self._evaluate_from_turn(request)
        lease = _lease(request.turn.session.session_id)
        return Succeeded(output_json=queue.popleft(), resource_id=lease)

    # -- evaluator --------------------------------------------------------

    def _observe(self, request: ObserveOwnedJob, core: CoreState) -> Answer:
        job = next(
            item
            for item in core.evaluation.jobs
            if isinstance(item, OwnedJob) and item.resource_id == request.resource_id
        )
        plan = job.plan
        assert isinstance(plan.candidate, RevisionRef)
        if self.infrastructure_failure(plan):
            return Failed(accepted=True)
        commit = plan.candidate.revision_id.root
        refs: list[EvidenceRef] = []
        stages: list[EvaluationStageResult] = []
        for stage in plan.stages:
            kind = _KINDS[stage.stage_id]
            reading = self._reading(plan, kind, stage.stage_id, commit)
            refs.append(self._ref(job.scope, job.submission_id, plan, reading))
            self.readings[reading.evidence_id] = reading
            outcome = (
                EvaluationStageOutcome.PASSED if reading.passed else EvaluationStageOutcome.FAILED
            )
            stages.append(EvaluationStageResult(stage_id=stage.stage_id, outcome=outcome))
        accuracy = (
            all(
                item.passed
                for item in self.readings.values()
                if item.kind is EvidenceKind.CORRECTNESS
            )
            or plan.purpose == "baseline"
        )
        return Succeeded(
            resource_id=request.resource_id,
            evidence=tuple(refs),
            facts=EvaluationTerminalFacts(stages=tuple(stages), accuracy_passed=accuracy),
        )

    @staticmethod
    def _ref(
        scope: Scope, source: RequestId, plan: MeasurementPlan, reading: EvidenceReading
    ) -> EvidenceRef:
        assert isinstance(plan.candidate, RevisionRef)
        return EvidenceRef(
            evidence_id=reading.evidence_id,
            kind=reading.kind,
            purpose=plan.purpose,
            scope=scope,
            source_request=source,
            candidate=plan.candidate,
            observation_sequence=0,
            evaluator_digest=plan.evaluator_digest,
            workload_digest=plan.workload_digest,
            environment_digest=plan.environment_digest,
            provenance="trusted",
            status=ObservationStatus.SUCCEEDED if reading.passed else ObservationStatus.FAILED,
        )

    def _reading(
        self, plan: MeasurementPlan, kind: EvidenceKind, stage: str, commit: str
    ) -> EvidenceReading:
        identifier = EvidenceId(root=f"{stage}:{plan.purpose}:{commit}")
        if kind is EvidenceKind.CORRECTNESS:
            passed = plan.purpose == "baseline" or self.accuracy(commit)
            return EvidenceReading(evidence_id=identifier, kind=kind, passed=passed, stage=stage)
        if kind is EvidenceKind.PROFILING:
            return EvidenceReading(evidence_id=identifier, kind=kind, passed=True, stage=stage)
        value = self.baseline_value if plan.purpose == "baseline" else self.benchmark(commit)
        if value is None:
            return EvidenceReading(
                evidence_id=identifier,
                kind=kind,
                passed=False,
                stage=stage,
                partial=PartialRow(name="throughput", value=1.0, direction="max", unit="tokens/s"),
                feedback="benchmark failed",
            )
        return EvidenceReading(
            evidence_id=identifier,
            kind=kind,
            passed=True,
            stage=stage,
            metrics=(MetricRow(name="throughput", value=value, direction="max", unit="tokens/s"),),
        )

    # -- registered operations -------------------------------------------

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        typed = dynamic_operation_registry().decode(request.operation)
        if isinstance(typed, VerifyParentRevision):
            return Succeeded(
                outcome=ParentVerification(
                    status="succeeded", verified=self.parent_verified(typed.parent.revision_id.root)
                )
            )
        if isinstance(typed, InterpretEvidence):
            return Succeeded(
                outcome=EvidenceReadings(
                    status="succeeded",
                    readings=tuple(self.readings[item.evidence_id] for item in typed.evidence),
                )
            )
        assert isinstance(typed, RenderRoleArtifacts)
        return Succeeded(
            outcome=RenderedArtifacts(
                status="succeeded",
                prompts=(
                    ArtifactRef(
                        artifact_id=ArtifactId(root=f"prompt:{request.operation_id.root}"),
                        digest="prompt",
                    ),
                ),
            )
        )
