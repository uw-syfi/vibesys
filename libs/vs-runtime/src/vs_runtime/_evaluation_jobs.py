"""Pure translation between core measurement values and the evaluation executor port.

Nothing here performs I/O or holds state. ``measurement_request`` turns an
accepted plan into the ordered semantic request an executor runs, and
``job_view`` turns one executor poll into the observation facts the core
consumes: status, release, progress, scientific stage results and evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import ValidationError

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    BenchmarkFailure,
    ContractError,
    EvaluationStageOutcome,
    EvaluationStageResult,
    EvaluationTerminalFacts,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    JobProgress,
    MeasurementFailure,
    MeasurementPlan,
    Observation,
    ObservationStatus,
    RequestId,
    ResourceId,
    RevisionRef,
    Scope,
)
from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceOutcome,
    ExecutorObservation,
    ExecutorPoll,
    PollPhase,
    SemanticEvaluationStage,
    StageState,
    TrustedEvidence,
    failure_signature,
)
from vs_evaluation.api import EvidenceKind as StageKind
from vs_runtime._observation_factory import ObservationFacts, ObservationSubject

if TYPE_CHECKING:
    from vs_runtime._evidence_ledger import EvidenceRecorder

STAGE_KINDS: dict[str, StageKind] = {
    "accuracy": StageKind.ACCURACY,
    "benchmark": StageKind.BENCHMARK,
    "profile": StageKind.PROFILE,
}
_ACCURACY_STAGE = "accuracy"
_BENCHMARK_STAGE = "benchmark"


class PlanRejection(StrEnum):
    """Why a plan can never run, decided before any executor contact."""

    UNRESOLVED_CANDIDATE = "candidate is a snapshot result, not a resolved revision"
    UNKNOWN_STAGE = "stage id is not one of the executor's stages"
    BAD_DIGEST = "digest is not a sha256 content address"
    BAD_REVISION = "revision digest does not name its own git commit"


class RejectedPlanError(ValueError):
    """The plan is permanently unrunnable, and the message names the offending field."""

    def __init__(self, field: str, reason: PlanRejection) -> None:
        """Name the field and the closed reason."""
        super().__init__(f"{field}: {reason.value}")


def handle_for(request_id: RequestId) -> str:
    """The stable executor handle, and the core resource id, of one submission."""
    return "vs-" + hashlib.sha256(request_id.root.encode()).hexdigest()[:40]


def _digest(field: str, value: str) -> ContentDigest:
    try:
        return ContentDigest(value=value.removeprefix("sha256:"))
    except ValidationError as error:
        raise RejectedPlanError(field, PlanRejection.BAD_DIGEST) from error


def _candidate_fingerprint(candidate: RevisionRef) -> ContentDigest:
    """The content address of a revision, from core's one reading of its digest.

    A git commit id already determines its tree, so the fingerprint is the sha256 of
    the canonical ``git-commit:<commit>`` digest. A ref that is not canonical (another
    scheme, or a digest naming another commit) is rejected, not guessed at.
    """
    if candidate.git_commit is None:
        raise RejectedPlanError("plan.candidate.digest", PlanRejection.BAD_REVISION)
    return ContentDigest.sha256(candidate.digest.encode())


def _ordered(plan: MeasurementPlan) -> tuple[str, ...]:
    """Declared order, with every stage after the stages it depends on."""
    done: list[str] = []
    pending = list(plan.stages)
    while pending:
        ready = next(s for s in pending if set(s.depends_on) <= set(done))
        done.append(ready.stage_id)
        pending.remove(ready)
    return tuple(done)


def measurement_request(plan: MeasurementPlan, scope: Scope, handle_id: str) -> EvaluationRequest:
    """The ordered semantic request for an accepted plan, or a typed rejection."""
    if not isinstance(plan.candidate, RevisionRef):
        raise RejectedPlanError("plan.candidate", PlanRejection.UNRESOLVED_CANDIDATE)
    fingerprints = EvidenceFingerprints(
        candidate=_candidate_fingerprint(plan.candidate),
        evaluator=_digest("plan.evaluator_digest", plan.evaluator_digest),
        workload=_digest("plan.workload_digest", plan.workload_digest),
        environment=_digest("plan.environment_digest", plan.environment_digest),
    )
    steps = []
    for stage_id in _ordered(plan):
        kind = STAGE_KINDS.get(stage_id)
        if kind is None:
            raise RejectedPlanError(f"plan.stages.{stage_id}", PlanRejection.UNKNOWN_STAGE)
        stage = SemanticEvaluationStage(
            snapshot=plan.candidate.revision_id.root,
            kind=kind,
            fingerprints=fingerprints,
            submitted_at_s=plan.submitted_at,
            deadline_at_s=plan.deadline_at,
        )
        steps.append(EvaluationStep(name=stage_id, payload=stage.model_dump(mode="json")))
    return EvaluationRequest(
        key=handle_id,
        owner_scope=f"{scope.owner.kind}:{scope.owner.root}",
        owner_generation=scope.generation,
        stages=tuple(steps),
        stop_on_failure=plan.policy == "ordered",
    )


class _Observe(Protocol):
    def __call__(
        self, status: ObservationStatus, *, accepted: bool, terminal: bool, diagnostic: str = ""
    ) -> Observation: ...


class JobObserver(Protocol):
    """Issues the next observation of the job, sequence included (``ObservationFactory``)."""

    def __call__(self, facts: ObservationFacts) -> Observation: ...


@dataclass(frozen=True)
class JobView:
    """Everything the core learns from one poll of one job."""

    observation: Observation
    progress: JobProgress | None
    evidence: tuple[EvidenceRef, ...]
    facts: EvaluationTerminalFacts | None
    failure: MeasurementFailure | None


def job_view(  # noqa: PLR0913  # lint-waiver: LW-940004 [PLR0913]; the plan, identity, job and observer are independent facts of one view.
    poll: ExecutorPoll,
    plan: MeasurementPlan,
    subject: ObservationSubject,
    handle_id: str,
    now_at: float,
    observe: JobObserver,
    ledger: EvidenceRecorder,
) -> JobView:
    """Translate one executor poll into the facts of the job's next observation."""

    def observed(
        status: ObservationStatus, *, accepted: bool, terminal: bool, diagnostic: str = ""
    ) -> Observation:
        return observe(
            ObservationFacts(
                status=status,
                terminal=terminal,
                accepted=accepted,
                released=terminal,
                children_complete=terminal,
                resource_id=ResourceId(root=handle_id),
                diagnostic=diagnostic,
            )
        )

    def progress(
        observation: Observation,
        state: Literal["pending", "running", "unknown"],
        *,
        stage: str | None = None,
        reason: str | None = None,
    ) -> JobProgress:
        return JobProgress(
            observation_sequence=observation.sequence,
            observed_at=now_at,
            state=state,
            stage_id=stage,
            pending_reason=reason,
        )

    match poll.phase:
        case PollPhase.UNSUBMITTED | PollPhase.UNKNOWN:
            seen = observed(
                ObservationStatus.UNKNOWN,
                accepted=False,
                terminal=False,
                diagnostic=poll.detail or "executor holds no record of the job",
            )
            return JobView(seen, progress(seen, "unknown"), (), None, None)
        case PollPhase.QUEUED:
            seen = observed(ObservationStatus.PENDING, accepted=True, terminal=False)
            return JobView(
                seen, progress(seen, "pending", reason=poll.pending_reason), (), None, None
            )
        case PollPhase.RUNNING:
            known = {stage.stage_id for stage in plan.stages}
            stage = poll.current_stage if poll.current_stage in known else None
            seen = observed(ObservationStatus.PENDING, accepted=True, terminal=False)
            return JobView(seen, progress(seen, "running", stage=stage), (), None, None)
        case PollPhase.ENDED:
            if poll.terminal is None:
                raise ContractError(("poll", "terminal"), "an ended poll carries its terminal")
            return _terminal_view(poll.terminal, plan, observed, subject, ledger)


def _terminal_view(
    terminal: ExecutorObservation,
    plan: MeasurementPlan,
    make: _Observe,
    subject: ObservationSubject,
    ledger: EvidenceRecorder,
) -> JobView:
    if terminal.state is EvaluationState.CANCELED:
        return JobView(
            make(ObservationStatus.CANCELLED, accepted=True, terminal=True), None, (), None, None
        )
    status = (
        ObservationStatus.SUCCEEDED
        if terminal.state is EvaluationState.SUCCEEDED
        else ObservationStatus.FAILED
    )
    observation = make(status, accepted=True, terminal=True, diagnostic=terminal.failure or "")
    evidence, outcomes = _evidence(terminal, plan, subject, observation.sequence, ledger)
    facts = _facts(terminal, plan, outcomes)
    failure = None
    if status is ObservationStatus.FAILED:
        failure = MeasurementFailure.UNKNOWN if evidence else MeasurementFailure.INFRASTRUCTURE
    return JobView(observation, None, evidence, facts, failure)


def _core_kind(kind: StageKind, purpose: str) -> EvidenceKind:
    if kind is StageKind.PROFILE:
        return EvidenceKind.PROFILING
    if purpose == "local-validation":
        return EvidenceKind.LOCAL_VALIDATION
    return EvidenceKind.CORRECTNESS if kind is StageKind.ACCURACY else EvidenceKind.BENCHMARK


def _evidence(
    terminal: ExecutorObservation,
    plan: MeasurementPlan,
    subject: ObservationSubject,
    sequence: int,
    ledger: EvidenceRecorder,
) -> tuple[tuple[EvidenceRef, ...], dict[str, TrustedEvidence]]:
    if not isinstance(plan.candidate, RevisionRef):
        raise ContractError(("plan", "candidate"), "a submitted plan names a revision")
    known = {stage.stage_id for stage in plan.stages}
    refs: list[EvidenceRef] = []
    by_stage: dict[str, TrustedEvidence] = {}
    for step in terminal.stage_results:
        if step.result is None or step.name not in known:
            continue
        item = TrustedEvidence.model_validate(step.result)
        by_stage[step.name] = item
        ledger.record(subject.request_id, item, plan.purpose)
        refs.append(
            EvidenceRef(
                evidence_id=EvidenceId(root=item.evidence_id),
                kind=_core_kind(item.kind, plan.purpose),
                purpose=plan.purpose,
                scope=subject.scope,
                source_request=subject.request_id,
                candidate=plan.candidate,
                observation_sequence=sequence,
                evaluator_digest=plan.evaluator_digest,
                workload_digest=plan.workload_digest,
                environment_digest=plan.environment_digest,
                provenance="trusted",
                status=(
                    ObservationStatus.FAILED
                    if item.outcome is EvidenceOutcome.FAILED
                    else ObservationStatus.SUCCEEDED
                ),
                artifacts=tuple(
                    ArtifactRef(artifact_id=ArtifactId(root=a.path), digest=a.digest.value)
                    for a in item.artifacts
                ),
            )
        )
    return tuple(refs), by_stage


def _facts(
    terminal: ExecutorObservation,
    plan: MeasurementPlan,
    outcomes: dict[str, TrustedEvidence],
) -> EvaluationTerminalFacts | None:
    if not terminal.stage_results:
        return None
    skipped = {s.name for s in terminal.stage_results if s.state is StageState.SKIPPED}
    stages = []
    for stage in plan.stages:
        item = outcomes.get(stage.stage_id)
        if item is None or stage.stage_id in skipped:
            outcome = EvaluationStageOutcome.UNKNOWN
        elif item.outcome is EvidenceOutcome.FAILED:
            outcome = EvaluationStageOutcome.FAILED
        else:
            outcome = EvaluationStageOutcome.PASSED
        stages.append(EvaluationStageResult(stage_id=stage.stage_id, outcome=outcome))
    accuracy = outcomes.get(_ACCURACY_STAGE)
    benchmark = outcomes.get(_BENCHMARK_STAGE)
    partial = (
        benchmark.partial_measurement
        if benchmark is not None and benchmark.outcome is EvidenceOutcome.FAILED
        else None
    )
    failure_text = next(
        (s.failure for s in terminal.stage_results if s.failure is not None), terminal.failure
    )
    return EvaluationTerminalFacts(
        stages=tuple(stages),
        traceback_signature=None if failure_text is None else failure_signature(failure_text),
        failed_benchmark=(
            BenchmarkFailure(
                partial_rate=partial.value, rate_lower=partial.value, rate_upper=partial.value
            )
            if partial is not None and partial.value >= 0
            else None
        ),
        accuracy_passed=accuracy is not None and accuracy.outcome is not EvidenceOutcome.FAILED,
    )
