"""Scripted executors for the pure driver (`vs_core.testing.drive`) and hand-built shells.

Tests that run the production shell use `world.DynamicWorld` (vs-dst) instead.

An executor reports what it saw (a job accepted, a stage passed, a reply) and never core's
conclusions. Evidence identities are derived from the job's plan, so a reading and the evidence
it decodes always agree.
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
    ObservationStatus,
    ObserveOwnedJob,
    OwnedJob,
    ResourceId,
    ResumeSessionTurn,
    RetainRevision,
    RevisionRef,
    SessionId,
    SnapshotAndRetain,
    SubmitMeasurement,
    VerifyAdoption,
)
from vs_core.testing.drive import Answer, Running, Succeeded, Unknown
from vs_runtime.api.core import handle_for

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreState, MeasurementPlan, Request, RequestId, Scope

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

    # A reply is the agent's JSON, or an `Answer` for a turn that ends another way.
    planner: deque[str | Answer] = field(default_factory=deque)
    implementer: deque[str | Answer] = field(default_factory=deque)
    judge: deque[str | Answer] = field(default_factory=deque)
    submit: Callable[[SubmitMeasurement], Answer] | None = None
    jobs: int = 0
    readings: dict[EvidenceId, EvidenceReading] = field(default_factory=dict)

    def __call__(self, request: Request, core: CoreState) -> Answer:
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
                digest = hashlib.sha256(request.attempt.attempt_id.root.encode()).hexdigest()
                answer = Succeeded(revision=RevisionRef.of_git_commit(digest[:40]))
            case RetainRevision():
                answer = Succeeded(revision=request.revision)
            case AdoptRevision() | VerifyAdoption():
                answer = Succeeded(revision=request.selection.revision)
        return answer

    def finished_agent_evaluation(self, request: SubmitMeasurement) -> Answer:
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
            resource_id=ResourceId(root=handle_for(request.request_id)),
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

    def _submit(self, request: SubmitMeasurement) -> Answer:
        if request.plan.purpose == "local-validation" and self.submit is None:
            return self.finished_agent_evaluation(request)
        if self.submit is not None:
            return self.submit(request)
        self.jobs += 1
        return Running(resource_id=ResourceId(root=f"job:{self.jobs}"))

    # -- agents -----------------------------------------------------------

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        role = request.turn.session.role_id.root.removeprefix("dynamic-")
        queue = {
            "orchestrator": self.planner,
            "implementer": self.implementer,
            "judge": self.judge,
        }[role]
        if not queue:
            return Unknown()
        reply = queue.popleft()
        if not isinstance(reply, str):
            return reply
        return Succeeded(output_json=reply, resource_id=_lease(request.turn.session.session_id))

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
            reading = self._reading(plan, _KINDS[stage.stage_id], stage.stage_id, commit)
            refs.append(self._ref(job.scope, job.submission_id, plan, reading))
            self.readings[reading.evidence_id] = reading
            outcome = (
                EvaluationStageOutcome.PASSED if reading.passed else EvaluationStageOutcome.FAILED
            )
            stages.append(EvaluationStageResult(stage_id=stage.stage_id, outcome=outcome))
        return Succeeded(
            resource_id=request.resource_id,
            evidence=tuple(refs),
            facts=EvaluationTerminalFacts(stages=tuple(stages), accuracy_passed=True),
        )

    @staticmethod
    def _ref(
        scope: Scope, source: RequestId, plan: MeasurementPlan, reading: EvidenceReading
    ) -> EvidenceRef:
        assert isinstance(plan.candidate, RevisionRef)
        return EvidenceRef(
            evidence_id=reading.evidence_id,
            kind=(
                EvidenceKind.LOCAL_VALIDATION
                if plan.purpose == "local-validation"
                else reading.kind
            ),
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

    @staticmethod
    def _reading(
        plan: MeasurementPlan, kind: EvidenceKind, stage: str, commit: str
    ) -> EvidenceReading:
        identifier = EvidenceId(root=f"{stage}:{plan.purpose}:{commit}")
        if kind is not EvidenceKind.BENCHMARK:
            return EvidenceReading(evidence_id=identifier, kind=kind, passed=True, stage=stage)
        value = 50.0 if plan.purpose == "baseline" else 100.0
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
            return Succeeded(outcome=ParentVerification(status="succeeded", verified=True))
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
