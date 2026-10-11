"""Scripted executors: what the mutator, judge, profiler and evaluators answer.

An executor reports what it saw (a job accepted, a stage passed, a reply) and never core's
conclusions. Evidence identities are derived from the job's plan, so a reading and the evidence it
decodes always agree. A reply is either popped from the role's queue or, when ``replies`` is set,
computed from the child it answers (its key, such as ``g2.c1``), so a whole world of candidates can
be scripted as a pure function of their names.
"""

from __future__ import annotations

import hashlib
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibesys.orchestration.evolve.strategy.api import (
    EvidenceReading,
    EvidenceReadings,
    ExportPatch,
    InterpretEvidence,
    MetricRow,
    ParentVerification,
    PartialRow,
    PatchExport,
    RenderedArtifacts,
    RenderRoleArtifacts,
    VerifyParentRevision,
    evolve_operation_registry,
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
    RequestId,
    ResourceId,
    ResumeSessionTurn,
    RetainRevision,
    RevisionRef,
    Scope,
    SessionId,
    SnapshotAndRetain,
    SubmitMeasurement,
    VerifyAdoption,
)
from vs_core.testing.drive import Answer, Failed, Running, Succeeded, Unknown
from vs_runtime.api.core import handle_for

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreState

_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
}


def _lease(session_id: SessionId) -> ResourceId:
    """The lease an executor holds for a session: the same name from ensure through close."""
    return ResourceId(root=f"lease:{session_id.root}")


def commit_of(attempt: str) -> str:
    """The commit the workspace executor retains for an attempt's one checkpoint."""
    return hashlib.sha256(attempt.encode()).hexdigest()[:40]


def child_key(subject: str) -> str:
    """The child (`g2.c1`, `bootstrap.1`) a turn's invocation or session names."""
    return subject.rsplit(".", 1)[0]


@dataclass
class Executors:
    """Scripted answers for one run, popped in the order requests are authorized."""

    # A reply is the agent's JSON, or an `Answer` for a turn that ends another way (as a
    # `Failed` turn, which returns nothing usable).
    mutator: deque[str | Answer] = field(default_factory=deque)
    judge: deque[str | Answer] = field(default_factory=deque)
    profiler: deque[str | Answer] = field(default_factory=deque)
    # When set, the reply of (role, child key, serial) instead of the queues above.
    replies: Callable[[str, str, int], str | Answer] | None = None
    # False retains one commit per attempt, whatever its turns did.
    # True: after the attempt's first snapshot a turn that changed nothing records the same revision.
    unchanged_after_first_retain: bool = False
    # False: a turn changes nothing, so no checkpoint is retained.
    changes: Callable[[str], bool] = lambda _attempt: True
    # Throughput per candidate commit; None makes the benchmark fail.
    benchmark: Callable[[str], float | None] = lambda _commit: 100.0
    # Further metric rows reported with the throughput, per candidate commit.
    extra_metrics: Callable[[str], tuple[MetricRow, ...]] = lambda _commit: ()
    accuracy: Callable[[str], bool] = lambda _commit: True
    # True for a measurement whose job ends without any evidence (its infrastructure failed).
    infrastructure_failure: Callable[[MeasurementPlan], bool] = lambda _plan: False
    # How many times the evaluator reports a candidate's job still running before it ends, so a
    # test can make children complete in any order.
    job_polls: Callable[[str], int] = lambda _commit: 0
    # What happens to a measurement submission (the default starts a job that ends when polled).
    submit: Callable[[SubmitMeasurement], Answer] | None = None
    jobs: int = 0
    observed: Counter[str] = field(default_factory=Counter)
    seen: list[Request] = field(default_factory=list)
    readings: dict[EvidenceId, EvidenceReading] = field(default_factory=dict)
    retained: Counter[str] = field(default_factory=Counter)
    rendered: list[RenderRoleArtifacts] = field(default_factory=list)

    def __call__(self, request: Request, core: CoreState) -> Answer:
        self.seen.append(request)
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

    def _submit(self, request: SubmitMeasurement) -> Answer:
        if request.plan.purpose == "local-validation" and self.submit is None:
            return self.finished_agent_evaluation(request)
        if self.submit is not None:
            return self.submit(request)
        self.jobs += 1
        return Running(resource_id=ResourceId(root=f"job:{self.jobs}"))

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

    def _retain(self, request: SnapshotAndRetain) -> Answer:
        attempt = request.attempt.attempt_id.root
        if not self.changes(attempt):
            return Succeeded()
        if self.retained[attempt] and self.unchanged_after_first_retain:
            # Production snapshotting records the same revision when a turn changed nothing.
            return Succeeded(revision=RevisionRef.of_git_commit(commit_of(attempt)))
        self.retained[attempt] += 1
        return Succeeded(revision=RevisionRef.of_git_commit(commit_of(attempt)))

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

    # -- agents -----------------------------------------------------------

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        role = request.turn.session.role_id.root.removeprefix("evolve-")
        invocation = request.turn.invocation_id.root
        # `inv:<role>:<key>.<role>:<serial>`
        _, _, subject, serial = invocation.split(":")
        key = child_key(subject)
        if self.replies is not None:
            reply = self.replies(role, key, int(serial))
        else:
            queue = {"mutator": self.mutator, "judge": self.judge, "profiler": self.profiler}[role]
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
        if self.infrastructure_failure(plan):
            return Failed(accepted=True)
        commit = plan.candidate.revision_id.root
        self.observed[request.resource_id.root] += 1
        if self.observed[request.resource_id.root] <= self.job_polls(commit):
            return Running(resource_id=request.resource_id)
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
        accuracy = all(
            item.passed for item in self.readings.values() if item.kind is EvidenceKind.CORRECTNESS
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

    def _reading(
        self, plan: MeasurementPlan, kind: EvidenceKind, stage: str, commit: str
    ) -> EvidenceReading:
        identifier = EvidenceId(root=f"{stage}:{plan.purpose}:{commit}")
        if kind is EvidenceKind.CORRECTNESS:
            return EvidenceReading(
                evidence_id=identifier, kind=kind, passed=self.accuracy(commit), stage=stage
            )
        value = self.benchmark(commit)
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
            metrics=(
                MetricRow(name="throughput", value=value, direction="max", unit="tokens/s"),
                *self.extra_metrics(commit),
            ),
        )

    # -- registered operations -------------------------------------------

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        typed = evolve_operation_registry().decode(request.operation)
        if isinstance(typed, VerifyParentRevision):
            return Succeeded(outcome=ParentVerification(status="succeeded", verified=True))
        if isinstance(typed, InterpretEvidence):
            return Succeeded(
                outcome=EvidenceReadings(
                    status="succeeded",
                    readings=tuple(self.readings[item.evidence_id] for item in typed.evidence),
                )
            )
        if isinstance(typed, ExportPatch):
            return Succeeded(
                outcome=PatchExport(
                    status="succeeded", patch=f"patch for {typed.revision.revision_id.root}"
                )
            )
        assert isinstance(typed, RenderRoleArtifacts)
        self.rendered.append(typed)
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
