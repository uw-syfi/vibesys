"""Scripted executors: what the roles answer, and the issue board they work on.

An executor reports what it saw (a reply, a write that landed) and never core's conclusions.
The board is a real `IssueBoardOwner` over a `FakeIssueTracker`, so the world keeps its state
across a restart and a scripted fault can lose an acknowledgement after a write landed.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from vibesys.orchestration.issue_queue.strategy.api import (
    OPERATIONS,
    RenderRoleArtifacts,
    plain_operation_registry,
)
from vs_core.api import (
    AdoptRevision,
    ArtifactId,
    ArtifactRef,
    CancelOwnedResource,
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
    InspectRequest,
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
    FakeIssueTracker,
    LogProgress,
    MemoryProgressLog,
    board_operation_registrations,
)
from vs_project.api import Project
from vs_runtime.api.core import (
    Applied,
    ExecutionContext,
    ExecutionResult,
    IssueBoardOwner,
    NamespaceOperationReceipts,
    ObservationFactory,
    OperationEntry,
    OperationRole,
    ReceiptStore,
    RegisteredOperationRequests,
    build_operation_catalog,
    handle_for,
    operation_roles,
)
from vs_runtime.api.testing import ScriptedExecutors

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreState, OperationRequest


class Texts:
    """Board texts that name their input, so a test can tell which template produced them."""

    def issue_body(self, template: str) -> str:
        return f"rendered {template}"

    def progress_entry(self, request: LogProgress) -> str:
        return f"{request.step} {request.iteration}"


_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
    "profile": EvidenceKind.PROFILING,
}


class FakeRenderer:
    """The render owner: stores nothing and names the prompt after the request."""

    async def execute(
        self, request: OperationRequest, context: ExecutionContext
    ) -> dict[str, object]:
        del context
        render = cast("RenderRoleArtifacts", request)
        return {
            "status": "succeeded",
            "prompts": (
                ArtifactRef(
                    artifact_id=ArtifactId(root=f"prompt:{render.subject}:{render.ordinal}"),
                    digest="prompt",
                ),
            ),
        }

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Applied:
        return Applied(await self.execute(request, context))


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
    # Attempts whose turns change nothing: the workspace executor retains the revision it started
    # from, as a snapshot of an unchanged tree is.
    unchanged: set[str] = field(default_factory=set)
    bases: dict[str, RevisionRef] = field(default_factory=dict)
    # Whether the trusted accuracy gate passes for a commit.
    accuracy: Callable[[str], bool] = lambda _commit: True
    owner: IssueBoardOwner = field(init=False)
    operations: RegisteredOperationRequests = field(init=False)
    # Requests the real operation executor was handed, so an inspection of one is its to answer.
    operation_ids: set[str] = field(default_factory=set)
    # The order in which workspaces retained a revision: a later one built on the earlier ones,
    # so it is the better candidate of this world (the issue queue adopts its last tip).
    ranks: dict[str, int] = field(default_factory=dict)
    _disk: tempfile.TemporaryDirectory[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.owner = IssueBoardOwner(self.tracker, self.progress, Texts())
        self._disk = tempfile.TemporaryDirectory()
        root = Path(self._disk.name)
        (root / "project").mkdir()
        namespace = Project.open(root / "project").state.state_store_namespace("run")
        registrations = operation_roles(OPERATIONS)
        board = tuple(
            OperationEntry.owned(registration, self.owner)
            for registration in board_operation_registrations()
        )
        catalog = build_operation_catalog(
            registrations,
            {
                OperationRole.RENDER_ARTIFACTS: FakeRenderer(),
                OperationRole.VERIFY_REVISION: FakeRenderer(),
            },
            extra=board,
        )
        self.operations = RegisteredOperationRequests(
            catalog,
            NamespaceOperationReceipts(ReceiptStore(namespace)),
            ObservationFactory(ReceiptStore(namespace)),
        )

    def __call__(self, request: Request, core: CoreState) -> Answer:
        self.seen.append(request)
        match request:
            case ObserveOwnedJob():
                return self._observe(request, core)
            case ExecuteRegisteredOperation():
                return self._operation(request)
            case SubmitMeasurement():
                # A job is named by its request, as the agent evaluation bridge names the
                # handle it gives the agent.
                assert request.request_id is not None
                self.jobs += 1
                return Running(resource_id=ResourceId(root=handle_for(request.request_id)))
            case _:
                return self._answer(request)

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        """An operation answered in process: a board operation is performed by the real owner."""
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
        return Succeeded(outcome=self.owner.perform(typed))

    def headline(self, commit: str) -> float | None:
        """The throughput of a candidate in this world: its rank among the revisions retained."""
        rank = self.ranks.get(commit)
        return None if rank is None else float(rank + 1)

    def _retained(self, revision: RevisionRef) -> RevisionRef:
        self.ranks.setdefault(revision.revision_id.root, len(self.ranks))
        return revision

    def _answer(self, request: Request) -> Answer:
        """What an executor that needs no view of core reports for ``request``."""
        match request:
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
                attempt = request.attempt.attempt_id.root
                kept = self.bases[attempt] if attempt in self.unchanged else None
                answer = Succeeded(revision=self._retained(kept or self.revision_of(attempt)))
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
                    kind=(
                        EvidenceKind.LOCAL_VALIDATION
                        if plan.purpose == "local-validation"
                        else _KINDS[stage.stage_id]
                    ),
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

    def _ensure(self, request: EnsureWorkspace | EnsureSession) -> Answer:
        if isinstance(request, EnsureWorkspace):
            self.bases[request.attempt.attempt_id.root] = request.plan.base
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


class WorldTranslator(ScriptedExecutors):
    """Scripted executors whose operation role is the production operation executor.

    Operations go through real receipts and the real `IssueBoardOwner`, so a lost
    acknowledgement and a restart are recovered by inspection exactly as in production. Every
    other role answers from the script.
    """

    def _owns(self, request: Request) -> bool:
        world = self._script
        if not isinstance(world, Executors):
            return False
        if isinstance(request, ExecuteRegisteredOperation | CancelOwnedResource):
            return True
        return isinstance(request, InspectRequest) and request.target.root in world.operation_ids

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        """Route operation requests to the real executor; script everything else."""
        world = self._script
        if not self._owns(request) or not isinstance(world, Executors):
            return await super().execute(request, context)
        assert request.request_id is not None
        self.journal.issue(request)
        if isinstance(request, ExecuteRegisteredOperation):
            world.operation_ids.add(request.request_id.root)
        assert isinstance(
            request, ExecuteRegisteredOperation | CancelOwnedResource | InspectRequest
        )
        result = await world.operations.execute(request, context)
        self.journal.observe_event(result.observation)
        return result


__all__ = ["Executors", "Texts", "WorldTranslator"]
