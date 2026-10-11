"""Scripted executors: what the roles answer, and the issue board they work on.

An executor reports what it saw (a reply, a write that landed) and never core's conclusions.
The board is a real `IssueBoardOwner` over a `FakeIssueTracker`, so the world keeps its state
across a restart and a scripted fault can lose an acknowledgement after a write landed.
"""

from __future__ import annotations

import hashlib
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibesys.orchestration.issue_queue.strategy.api import (
    RenderRoleArtifacts,
    plain_operation_registry,
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
    RenderedArtifacts,
    Request,
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
from vs_issue_tracker.api import (
    ApplyIssueChanges,
    FakeIssueTracker,
    LogProgress,
    MemoryProgressLog,
    ReadBoard,
)
from vs_runtime.api.core import IssueBoardOwner

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreState


class Texts:
    """Board texts that name their input, so a test can tell which template produced them."""

    def issue_body(self, template: str) -> str:
        return f"rendered {template}"

    def progress_entry(self, request: LogProgress) -> str:
        return f"{request.step} {request.iteration}"


def _lease(session_id: SessionId) -> ResourceId:
    """The lease an executor holds for a session: the same name from ensure through close."""
    return ResourceId(root=f"lease:{session_id.root}")


@dataclass
class Executors:
    """Scripted answers for one run, popped in the order requests are authorized."""

    # A reply is the agent's JSON, or an `Answer` for a turn that ends another way.
    implementer: deque[str | Answer] = field(default_factory=deque)
    judge: deque[str | Answer] = field(default_factory=deque)
    perf: deque[str | Answer] = field(default_factory=deque)
    tracker: FakeIssueTracker = field(default_factory=FakeIssueTracker)
    progress: MemoryProgressLog = field(default_factory=MemoryProgressLog)
    seen: list[Request] = field(default_factory=list)
    retained: Counter[str] = field(default_factory=Counter)
    agent_calls: int = 0
    jobs: int = 0
    # Whether the trusted accuracy gate passes for a commit.
    accuracy: Callable[[str], bool] = lambda _commit: True
    owner: IssueBoardOwner = field(init=False)

    def __post_init__(self) -> None:
        self.owner = IssueBoardOwner(self.tracker, self.progress, Texts())

    def __call__(self, request: Request, core: CoreState) -> Answer:
        self.seen.append(request)
        match request:
            case ObserveOwnedJob():
                return self._observe(request, core)
            case SubmitMeasurement():
                self.jobs += 1
                return Running(resource_id=ResourceId(root=f"job:{self.jobs}"))
            case _:
                return self._answer(request)

    def _answer(self, request: Request) -> Answer:
        """What an executor that needs no view of core reports for ``request``."""
        match request:
            case ExecuteRegisteredOperation():
                return self._operation(request)
            case DispatchTurn() | ResumeSessionTurn():
                return self._turn(request)
            case EnsureWorkspace() | EnsureSession():
                return self._ensure(request)
            case _:
                return self._lifecycle(request)

    def _lifecycle(self, request: Request) -> Answer:
        answer: Answer = Succeeded()
        match request:
            case CloseSession():
                answer = Succeeded(resource_id=_lease(request.session_id))
            case SnapshotAndRetain():
                answer = Succeeded(revision=self.revision_of(request.attempt.attempt_id.root))
            case RetainRevision():
                answer = Succeeded(revision=request.revision)
            case AdoptRevision() | VerifyAdoption():
                answer = Succeeded(revision=request.selection.revision)
        return answer

    def _observe(self, request: ObserveOwnedJob, core: CoreState) -> Answer:
        """A trusted measurement job that ends at once with the verdict the test set."""
        job = next(
            item
            for item in core.evaluation.jobs
            if isinstance(item, OwnedJob) and item.resource_id == request.resource_id
        )
        plan = job.plan
        assert isinstance(plan.candidate, RevisionRef)
        commit = plan.candidate.revision_id.root
        passed = self.accuracy(commit)
        refs = []
        stages = []
        for stage in plan.stages:
            identifier = EvidenceId(root=f"{stage.stage_id}:{plan.purpose}:{commit}")
            refs.append(
                EvidenceRef(
                    evidence_id=identifier,
                    kind=EvidenceKind.CORRECTNESS,
                    purpose=plan.purpose,
                    scope=job.scope,
                    source_request=job.submission_id,
                    candidate=plan.candidate,
                    observation_sequence=0,
                    evaluator_digest=plan.evaluator_digest,
                    workload_digest=plan.workload_digest,
                    environment_digest=plan.environment_digest,
                    provenance="trusted",
                    status=ObservationStatus.SUCCEEDED if passed else ObservationStatus.FAILED,
                )
            )
            outcome = EvaluationStageOutcome.PASSED if passed else EvaluationStageOutcome.FAILED
            stages.append(EvaluationStageResult(stage_id=stage.stage_id, outcome=outcome))
        return Succeeded(
            resource_id=request.resource_id,
            evidence=tuple(refs),
            facts=EvaluationTerminalFacts(stages=tuple(stages), accuracy_passed=passed),
        )

    @staticmethod
    def revision_of(attempt: str) -> RevisionRef:
        """The commit the workspace executor retains for an attempt."""
        return RevisionRef.of_git_commit(hashlib.sha256(attempt.encode()).hexdigest()[:40])

    @staticmethod
    def _ensure(request: EnsureWorkspace | EnsureSession) -> Answer:
        if isinstance(request, EnsureWorkspace):
            return Succeeded(
                resource_id=ResourceId(
                    root=f"workspace:{request.scope.owner.root}:{request.attempt.generation}"
                ),
                revision=request.plan.base,
            )
        return Succeeded(resource_id=_lease(request.spec.session_id))

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        role = request.turn.session.role_id.root.removeprefix("plain-")
        queue = {"implementer": self.implementer, "judge": self.judge, "perf-evaluator": self.perf}[
            role
        ]
        if not queue:
            return Unknown()
        reply = queue.popleft()
        if not isinstance(reply, str):
            return reply
        self.agent_calls += 1
        return Succeeded(output_json=reply, resource_id=_lease(request.turn.session.session_id))

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        typed = plain_operation_registry().decode(request.operation)
        if isinstance(typed, RenderRoleArtifacts):
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
        if isinstance(typed, ReadBoard | ApplyIssueChanges | LogProgress):
            return Succeeded(outcome=self.owner.perform(typed))
        return Succeeded()


__all__ = ["Executors", "Texts"]
