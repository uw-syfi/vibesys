"""Scripted far side of a dynamic run: what its agents reply and what its benchmarks measure.

`Executors` answers the requests core makes of the host (workspaces, sessions, turns,
measurements, operations) for the dynamic search driven through the real core, whether by the
pure driver, the production shell or a hand-built shell. An executor reports what it saw (a job
accepted, a stage passed, a reply) and never core's conclusions. Evidence identities derive from
the job's plan, so a reading and the evidence it decodes always agree.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from vibesys.candidates.api import limits_for
from vibesys.orchestration.dynamic.core_policy.api import requirements_for
from vibesys.orchestration.dynamic.search.api import (
    DynamicConfig,
    EvidenceReading,
    EvidenceReadings,
    HandoffWritten,
    InterpretEvidence,
    MetricRow,
    ParentVerification,
    PartialRow,
    RenderedArtifacts,
    RenderRoleArtifacts,
    VerifyParentRevision,
    WriteHandoff,
    budget,
    dynamic,
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
    FlowState,
    Limits,
    ObservationStatus,
    ObserveOwnedJob,
    OwnedJob,
    ResourceId,
    ResumeSessionTurn,
    RetainRevision,
    RevisionRef,
    RunEnvelope,
    SessionId,
    SnapshotAndRetain,
    SubmitMeasurement,
    VerifyAdoption,
)
from vs_core.api.search import TaskStage, TaskTerms
from vs_core.testing.builders import initial_state, sample_retries
from vs_core.testing.drive import Answer, Harness, Succeeded, Unknown
from vs_runtime.api.core import handle_for

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_core.api import (
        CoreState,
        MeasurementPlan,
        OperationRegistry,
        Request,
        RequestId,
        Scope,
    )

_STAGE_SECONDS = 20_000
"""Longer than a host lease and any restart in a test, so a measurement outlives the host that began it."""

TERMS = TaskTerms(
    recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
    stages=(
        TaskStage(stage_id="accuracy", execution_seconds=_STAGE_SECONDS),
        TaskStage(stage_id="benchmark", execution_seconds=_STAGE_SECONDS),
        TaskStage(stage_id="profile", execution_seconds=_STAGE_SECONDS),
    ),
    measured=("accuracy", "benchmark"),
    queue_allowance=5,
    accuracy_stage="accuracy",
    accuracy_gated=(EvidenceKind.BENCHMARK,),
    operation_seconds=_STAGE_SECONDS,
)
FACTS = initial_state().run.facts.model_copy(update={"task": TERMS})
"""The run facts of a scripted run: the core's sample facts plus a task that measures."""

BASELINE_THROUGHPUT = 50.0
"""What the input measures; a candidate measures what ``throughputs`` says."""

_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
    "profile": EvidenceKind.PROFILING,
}


def _registry() -> OperationRegistry:
    found = dynamic.registry()
    assert found is not None
    return found


REGISTRY = _registry()
"""The codec of every operation a dynamic run records: the kit's and the handoff write."""


class _Bounds:
    """The declarations of a backend whose requests are retried twice and polled quickly.

    Its reconciliation bound outlasts a host's lease, so a request in flight when a host dies
    is still wanted by the host that resumes the run.
    """

    request_retries = sample_retries(2)
    observe_interval_seconds = 1.0
    observe_backoff_cap_seconds = 1.0
    cancellation_bound_seconds = 60.0
    cleanup_bound_seconds = 60.0
    reconciliation_bound_seconds = 5000.0
    queue_wait_budget_seconds = 43_200.0
    queue_notice_interval_seconds = 600.0


def config(**overrides: object) -> DynamicConfig:
    """A one-round, one-wide config, overridden by the caller."""
    return DynamicConfig.model_validate({"max_rounds": 1, "max_in_flight": 1, **overrides})


def limits(selected: DynamicConfig) -> Limits:
    """The core limits a run of ``selected`` is given, as the host derives them."""
    return limits_for(budget(selected), _Bounds())


def harness(selected: DynamicConfig, **overrides: object) -> Harness:
    """What hosts the dynamic search of ``selected`` on the real core."""
    hosting = Harness(
        registry=REGISTRY,
        facts=FACTS,
        limits=limits(selected),
        envelope_type=RunEnvelope[FlowState],
        requirements=requirements_for(TERMS),
        schemas=dynamic.reply_schemas,
    )
    return replace(hosting, **overrides)


def lease(session_id: SessionId) -> ResourceId:
    """The lease an executor holds for a session: the same name from ensure through close."""
    return ResourceId(root=f"lease:{session_id.root}")


def _handoff_path(call: int) -> str:
    return f".vibesys/state/runs/run/orchestrator-handoff/calls/call-{call:04d}.md"


@dataclass
class Executors:
    """Scripted answers for one run, popped in the order requests are authorized.

    A reply is the agent's JSON, or an `Answer` for a turn that ends another way. A role past
    its replies is never answered, unless ``exhausted`` gives the reply of such a turn. ``throughputs`` is what candidates measure (None: the
    benchmark fails), picked by a candidate's commit; ``submit`` replaces how a measurement
    submission is accepted (a timed cluster, say).
    """

    planner: deque[str | Answer] = field(default_factory=deque)
    implementer: deque[str | Answer] = field(default_factory=deque)
    judge: deque[str | Answer] = field(default_factory=deque)
    profiler: deque[str | Answer] = field(default_factory=deque)
    submit: Callable[[SubmitMeasurement], Answer] | None = None
    throughputs: Sequence[float | None] = (100.0,)
    baseline_throughput: float | None = BASELINE_THROUGHPUT
    exhausted: str | None = None
    jobs: int = 0
    readings: dict[EvidenceId, EvidenceReading] = field(default_factory=dict)
    handoffs: set[int] = field(default_factory=set)
    """The planning calls whose handoff file the owner wrote."""

    def headline(self, commit: str) -> float | None:
        """What the candidate of ``commit`` measures (a pure function of its commit)."""
        return self.throughputs[int(commit, 16) % len(self.throughputs)]

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """What the executor of ``request`` reports."""
        if isinstance(request, ObserveOwnedJob):
            return self._observe(request, core)
        return self._answer(request)

    def _answer(self, request: Request) -> Answer:
        answer: Answer = Succeeded()
        match request:
            case ExecuteRegisteredOperation():
                answer = self._operation(request)
            case SubmitMeasurement():
                answer = self._submit(request)
            case DispatchTurn() | ResumeSessionTurn():
                answer = self._turn(request)
            case EnsureWorkspace() | EnsureSession():
                answer = self._ensure(request)
            case CloseSession():
                answer = Succeeded(resource_id=lease(request.session_id))
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
            reading = self._reading(request.plan, _KINDS[stage.stage_id], stage.stage_id, commit)
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
        if isinstance(request, EnsureWorkspace):
            return Succeeded(
                resource_id=ResourceId(
                    root=f"workspace:{request.scope.owner.root}:{request.attempt.generation}"
                ),
                revision=request.plan.base,
            )
        return Succeeded(resource_id=lease(request.spec.session_id))

    def _submit(self, request: SubmitMeasurement) -> Answer:
        if request.plan.purpose == "local-validation" and self.submit is None:
            return self.finished_agent_evaluation(request)
        if self.submit is not None:
            return self.submit(request)
        return self.finished_agent_evaluation(request)

    # -- agents -----------------------------------------------------------

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        role = request.turn.session.role_id.root.removeprefix("dynamic-")
        queue = {
            "orchestrator": self.planner,
            "implementer": self.implementer,
            "judge": self.judge,
            "profiler": self.profiler,
        }[role]
        if not queue and self.exhausted is None:
            return Unknown()
        reply = queue.popleft() if queue else self.exhausted
        assert reply is not None
        if not isinstance(reply, str):
            return reply
        return Succeeded(output_json=reply, resource_id=lease(request.turn.session.session_id))

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
        if kind is not EvidenceKind.BENCHMARK:
            return EvidenceReading(evidence_id=identifier, kind=kind, passed=True, stage=stage)
        value = self.baseline_throughput if plan.purpose == "baseline" else self.headline(commit)
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
        typed = REGISTRY.decode(request.operation)
        if isinstance(typed, WriteHandoff):
            self.handoffs.add(typed.call)
            listed = tuple(_handoff_path(call) for call in sorted(self.handoffs))
            return Succeeded(outcome=HandoffWritten(path=_handoff_path(typed.call), listed=listed))
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
