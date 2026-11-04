"""The issue queue's world on the production shell: what its roles reply, and the board they work on.

`IssueQueueWorld` is a vs-dst `World`: the search, its config and the scripted far side of its
run. A test builds one from plain data, simulates it and asserts on the run and on the board::

    world = IssueQueueWorld(judge=[judged(passed=False), judged()])
    run = simulate(world)

Keyword arguments that name no world option configure the search (`IssueQueueConfig`), so a
misspelling is refused by name. An executor reports what it saw (a reply, a write that landed)
and never core's conclusions. The board is a real `IssueBoardOwner` over a `FakeIssueTracker`,
so a crash copies it with the rest of the world and a scripted fault can lose an acknowledgement
after a write landed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic import BaseModel
from tests.support.checkpoint_faults import FaultyCheckpoints
from tests.support.dynamic_script import FACTS, TERMS, lease
from tests.vibesys.orchestration.issue_queue._replies import evaluated, implemented, judged

from vibesys.candidates.api import limits_for
from vibesys.orchestration.issue_queue.core_policy.api import requirements_for
from vibesys.orchestration.issue_queue.search.api import (
    EvidenceReadings,
    InterpretEvidence,
    IssueQueueConfig,
    RenderedArtifacts,
    RenderRoleArtifacts,
    budget,
    issue_queue,
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
    ObservationStatus,
    ResourceId,
    ResumeSessionTurn,
    RetainRevision,
    RevisionRef,
    RunEnvelope,
    SnapshotAndRetain,
    SubmitMeasurement,
    VerifyAdoption,
)
from vs_core.testing.builders import sample_retries
from vs_core.testing.drive import Answer, Harness, Succeeded, Unknown
from vs_dst.api import RunSetup
from vs_issue_tracker.api import FakeIssueTracker, LogProgress, MemoryProgressLog
from vs_runtime.api.core import IssueBoardOwner, handle_for
from vs_runtime.api.testing import Launch
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vs_core.api import CoreState, MeasurementPlan, Request, RequestId, Scope
    from vs_issue_tracker.api import FaultPhase

_REGISTRY = issue_queue.registry()
assert _REGISTRY is not None, "the issue-queue search declares its operations"
REGISTRY = _REGISTRY

_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
    "profile": EvidenceKind.PROFILING,
}
_ROLES = {"implementer": "implementer", "judge": "judge", "perf-evaluator": "perf"}


class _Bounds:
    """The declarations of a backend whose requests are retried twice and polled quickly."""

    request_retries = sample_retries(2)
    observe_interval_seconds = 1.0
    observe_backoff_cap_seconds = 1.0
    cancellation_bound_seconds = 60.0
    cleanup_bound_seconds = 60.0
    reconciliation_bound_seconds = 5000.0
    queue_wait_budget_seconds = 43_200.0
    queue_notice_interval_seconds = 600.0


def issue_queue_config(**settings: object) -> IssueQueueConfig:
    """The search's config: two rounds and two attempts per issue unless ``settings`` say."""
    return IssueQueueConfig.model_validate(
        {
            "max_rounds": 2,
            "max_attempts_per_issue": 2,
            "max_issues_per_perf_eval": 2,
            "max_issues_per_round": 4,
            **settings,
        }
    )


def issue_queue_harness(settings: IssueQueueConfig, **overrides: object) -> Harness:
    """What hosts the search: its codec, the run's facts, limits and evidence requirements."""
    hosting = Harness(
        registry=REGISTRY,
        facts=FACTS,
        limits=limits_for(budget(settings), _Bounds()),
        envelope_type=RunEnvelope[FlowState],
        requirements=requirements_for(),
        schemas=issue_queue.reply_schemas,
    )
    return replace(hosting, **overrides)


class _Texts:
    """Board texts that name their input, so a test can tell which template produced them."""

    def issue_body(self, template: str) -> str:
        """The description of an issue filed from ``template``."""
        return f"rendered {template}"

    def progress_entry(self, request: LogProgress) -> str:
        """The progress entry of a recorded turn."""
        return f"{request.step} {request.iteration}"


@dataclass
class ScheduledFaults:
    """The tracker fails around the calls ``schedule`` names, counting every write's two phases.

    The n-th call of the tracker's fault hook (a write's ``before`` phase, then its ``after``
    phase) raises when ``schedule[n]`` is that call's phase. A ``before`` fault means the write
    did not happen; ``after`` that it landed and its acknowledgement was lost. Reads never fail
    and are not counted.
    """

    schedule: dict[int, FaultPhase]
    calls: int = 0

    def __call__(self, operation: str, phase: FaultPhase) -> None:
        """Raise when the schedule says this call fails."""
        if operation in {"get", "list"}:
            return
        number = self.calls
        self.calls += 1
        if self.schedule.get(number) == phase:
            message = f"tracker {phase} {operation}"
            raise ConnectionError(message)


def revision_of(attempt: str) -> RevisionRef:
    """The commit the workspace executor retains for an attempt: fixed by the attempt."""
    return RevisionRef.of_git_commit(hashlib.sha256(attempt.encode()).hexdigest()[:40])


class IssueQueueWorld:
    """The roles of one issue-queue run and the board they work on, answering from plain data.

    A reply is handed out once, in the order requests are authorized, so a copy of the world
    taken mid-run continues from where the run stood. A role with no reply left answers with a
    reply that is no valid JSON.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-994710 [PLR0913]; each option is one independent fact of the scripted world, and a test names the ones it varies.
        # > Grouping them in a holder would make every test build the holder before the world.
        self,
        *,
        implementer: Sequence[BaseModel | str | Answer] | None = None,
        judge: Sequence[BaseModel | str | Answer] | None = None,
        perf: Sequence[BaseModel | str | Answer] | None = None,
        accurate: bool = True,
        silent_official: bool = False,
        unchanged: Sequence[str] = (),
        tracker_faults: Mapping[int, FaultPhase] | None = None,
        record_requests: bool = False,
        checkpoints: Sequence[str] = (),
        **settings: object,
    ) -> None:
        """Script a run.

        ``implementer``, ``judge`` and ``perf`` are the replies each role gives, in order: a
        reply model or its JSON, or an `Answer` for a turn that ends another way; by default
        each role gives one ordinary reply (implemented, approved, no issues filed). ``accurate``
        is whether the trusted accuracy gate passes; ``silent_official`` makes the final
        (official) measurement never report. ``unchanged`` names the attempts whose turns change
        nothing: the workspace executor retains the revision the attempt started from.
        ``tracker_faults`` maps the n-th call of the tracker's fault hook to the phase it fails
        in (see `ScheduledFaults`). ``record_requests`` keeps every request the executors
        received in ``requests``. ``checkpoints`` answers the n-th checkpoint request ``succeeded``, ``failed``
        or ``retryable`` (cycling). Any other option is an `IssueQueueConfig` field.
        """
        self.config = issue_queue_config(**settings)
        config = self.config
        self.setup = RunSetup(
            new_launch=lambda: Launch(issue_queue, config),
            harness=issue_queue_harness(config),
            schemas=issue_queue.reply_schemas,
        )
        self.tracker = FakeIssueTracker(
            faults=None if tracker_faults is None else ScheduledFaults(dict(tracker_faults)),
            clock=ManualClock(),
            threads=SimThreads(),
        )
        self.progress = MemoryProgressLog()
        self.requests: list[Request] = []
        self._record = record_requests
        self._replies: dict[str, list[BaseModel | str | Answer]] = {
            "implementer": list([implemented()] if implementer is None else implementer),
            "judge": list([judged()] if judge is None else judge),
            "perf": list([evaluated()] if perf is None else perf),
        }
        self._accurate = accurate
        self._silent_official = silent_official
        self._unchanged = frozenset(unchanged)
        self._checkpoints = FaultyCheckpoints(self._answer, tuple(checkpoints))
        self._bases: dict[str, RevisionRef] = {}
        self._ranks: dict[str, int] = {}

    # -- what a test reads back -------------------------------------------------------------

    def board(self) -> dict[int, tuple[str, int]]:
        """Every issue's status and attempts, by number."""
        return {issue.id: (issue.status.value, issue.attempts) for issue in self.tracker.list()}

    def titles(self) -> list[str]:
        """The title of every issue, in the order the board lists them."""
        return [issue.title for issue in self.tracker.list()]

    def history(self) -> list[tuple[object, ...]]:
        """Every issue with its status, attempts and the actions recorded on it."""
        return [
            (issue.id, issue.status.value, issue.attempts, [e.action for e in issue.history])
            for issue in self.tracker.list()
        ]

    def headline(self, commit: str) -> float | None:
        """The throughput of a candidate in this world: its rank among the revisions retained."""
        rank = self._ranks.get(commit)
        return None if rank is None else float(rank + 1)

    # -- the executors ------------------------------------------------------------------------

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """What the executors report for ``request``."""
        if self._record:
            self.requests.append(request)
        return self._checkpoints(request, core)

    def _answer(self, request: Request, core: CoreState) -> Answer:
        """What an executor that needs no view beyond ``core`` reports for ``request``."""
        del core
        answer: Answer
        match request:
            case ExecuteRegisteredOperation():
                answer = self._operation(request)
            case SubmitMeasurement():
                answer = self._submit(request)
            case DispatchTurn() | ResumeSessionTurn():
                answer = self._turn(request)
            case _:
                answer = self._environment(request)
        return answer

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        """An operation answered in process: a board operation is performed by the real owner."""
        typed = REGISTRY.decode(request.operation)
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
        if isinstance(typed, InterpretEvidence):
            return Succeeded(outcome=EvidenceReadings(status="succeeded", readings=()))
        owner = IssueBoardOwner(self.tracker, self.progress, _Texts())
        try:
            return Succeeded(outcome=owner.perform(typed))
        except ConnectionError:
            # The tracker failed around the write: whether it landed is for core to find out.
            return Unknown()

    def _submit(self, request: SubmitMeasurement) -> Answer:
        """A job is named by its request, as the agent evaluation bridge names the handle."""
        if self._silent_official and request.plan.purpose == "official":
            return Unknown()
        assert request.request_id is not None
        # The step loop ends a job's step when the job ends, so the submission is answered with
        # the end and the evidence: there is no poll for the world to answer later.
        return self._ended(
            request.plan,
            request.scope,
            request.request_id,
            ResourceId(root=handle_for(request.request_id)),
        )

    def _environment(self, request: Request) -> Answer:
        """What the workspace, session and adoption executors report: they need no script."""
        answer: Answer = Succeeded()
        match request:
            case EnsureWorkspace():
                self._bases[request.attempt.attempt_id.root] = request.plan.base
                answer = Succeeded(
                    resource_id=ResourceId(
                        root=f"workspace:{request.scope.owner.root}:{request.attempt.generation}"
                    ),
                    revision=request.plan.base,
                )
            case EnsureSession():
                answer = Succeeded(resource_id=lease(request.spec.session_id))
            case CloseSession():
                answer = Succeeded(resource_id=lease(request.session_id))
            case SnapshotAndRetain():
                attempt = request.attempt.attempt_id.root
                kept = self._bases[attempt] if attempt in self._unchanged else None
                answer = Succeeded(revision=self._retained(kept or revision_of(attempt)))
            case RetainRevision():
                answer = Succeeded(revision=request.revision)
            case AdoptRevision() | VerifyAdoption():
                answer = Succeeded(revision=request.selection.revision)
        return answer

    def _retained(self, revision: RevisionRef) -> RevisionRef:
        self._ranks.setdefault(revision.revision_id.root, len(self._ranks))
        return revision

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        role = _ROLES[request.turn.session.role_id.root.removeprefix("plain-")]
        queue = self._replies[role]
        # A turn past the script gets a reply that is no valid JSON, never silence: a silent
        # turn is lost only after its time limit, and a host that dies meanwhile leaves a
        # recovery check core cannot end (the gap named in `vs_core/_waits.py`).
        reply = queue.pop(0) if queue else "not json"
        if isinstance(reply, str | BaseModel):
            text = reply if isinstance(reply, str) else reply.model_dump_json()
            return Succeeded(output_json=text, resource_id=lease(request.turn.session.session_id))
        return reply

    def _ended(
        self, plan: MeasurementPlan, scope: Scope, submission: RequestId, resource_id: ResourceId
    ) -> Answer:
        """The job's end: its evidence, with the verdict the world was set to."""
        assert isinstance(plan.candidate, RevisionRef)
        commit = plan.candidate.revision_id.root
        status = ObservationStatus.SUCCEEDED if self._accurate else ObservationStatus.FAILED
        outcome = EvaluationStageOutcome.PASSED if self._accurate else EvaluationStageOutcome.FAILED
        refs = []
        stages = []
        for stage in plan.stages:
            refs.append(
                EvidenceRef(
                    evidence_id=EvidenceId(root=f"{stage.stage_id}:{plan.purpose}:{commit}"),
                    kind=_KINDS[stage.stage_id],
                    purpose=plan.purpose,
                    scope=scope,
                    source_request=submission,
                    candidate=plan.candidate,
                    observation_sequence=0,
                    evaluator_digest=plan.evaluator_digest,
                    workload_digest=plan.workload_digest,
                    environment_digest=plan.environment_digest,
                    provenance="trusted",
                    status=status,
                )
            )
            stages.append(EvaluationStageResult(stage_id=stage.stage_id, outcome=outcome))
        return Succeeded(
            resource_id=resource_id,
            evidence=tuple(refs),
            facts=EvaluationTerminalFacts(stages=tuple(stages), accuracy_passed=self._accurate),
        )


__all__ = [
    "FACTS",
    "TERMS",
    "IssueQueueWorld",
    "ScheduledFaults",
    "issue_queue_config",
    "issue_queue_harness",
    "revision_of",
]
