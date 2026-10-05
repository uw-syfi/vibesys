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
    ArtifactId,
    ArtifactRef,
    CloseSession,
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
    ResourceId,
    RetainRevision,
    RevisionRef,
    SessionId,
    SnapshotAndRetain,
    SubmitMeasurement,
    VerifyAdoption,
)
from vs_core.testing.drive import Answer, Running, Succeeded, Unknown

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreState

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
    baseline_value: float = 50.0
    parent_verified: Callable[[str], bool] = lambda _commit: True
    submit: Callable[[SubmitMeasurement], Answer] | None = None
    jobs: int = 0
    seen: list[Request] = field(default_factory=list)
    readings: dict[EvidenceId, EvidenceReading] = field(default_factory=dict)

    def __call__(self, request: Request, core: CoreState) -> Answer:
        self.seen.append(request)
        if isinstance(request, ObserveOwnedJob):
            return self._observe(request, core)
        return self._answer(request)

    def _answer(self, request: Request) -> Answer:
        """What an executor that needs no view of core reports for ``request``."""
        match request:
            case ExecuteRegisteredOperation():
                return self._operation(request)
            case SubmitMeasurement():
                return self._submit(request)
            case DispatchTurn():
                return self._turn(request)
            case EnsureWorkspace() | EnsureSession():
                return self._ensure(request)
            case CloseSession():
                return Succeeded(resource_id=_lease(request.session_id))
            case SnapshotAndRetain():
                return self._retain(request)
            case RetainRevision():
                return Succeeded(revision=request.revision)
            case AdoptRevision() | VerifyAdoption():
                return Succeeded(revision=request.selection.revision)
            case _:
                return Succeeded()

    @staticmethod
    def _retain(request: SnapshotAndRetain) -> Answer:
        """The commit the workspace executor retained for the attempt, one per attempt."""
        digest = hashlib.sha256(request.attempt.attempt_id.root.encode()).hexdigest()[:40]
        return Succeeded(revision=RevisionRef.of_git_commit(digest))

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

    def _submit(self, request: SubmitMeasurement) -> Answer:
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
        commit = plan.candidate.revision_id.root
        refs: list[EvidenceRef] = []
        stages: list[EvaluationStageResult] = []
        for stage in plan.stages:
            kind = _KINDS[stage.stage_id]
            reading = self._reading(plan, kind, stage.stage_id, commit)
            refs.append(self._ref(job, plan, reading))
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
    def _ref(job: OwnedJob, plan: MeasurementPlan, reading: EvidenceReading) -> EvidenceRef:
        assert isinstance(plan.candidate, RevisionRef)
        return EvidenceRef(
            evidence_id=reading.evidence_id,
            kind=reading.kind,
            purpose=plan.purpose,
            scope=job.scope,
            source_request=job.submission_id,
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
