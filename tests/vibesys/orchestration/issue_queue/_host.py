"""A fake core for the issue-queue search: it answers the waits the flow opens, as a story or by script.

`Host` plays everything outside the flow: the workspace executor (open, settle), the agents (the
implementer, the judge, the performance evaluator), the evaluator (measure, read), the owner of
the issue board and the host's verifier. The board is the real board logic held in memory
(`FakeIssueTracker` behind the production `IssueBoardOwner`), so a write that is sent again is
recognized by its key exactly as the tracker does. A story answers one wait at a time (`answer`);
a generated test chooses which open wait to answer and how (`step`). Nothing here has a clock or
a thread: `FlowRun` is the whole world.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict, Unpack

from tests.vibesys.orchestration.issue_queue._replies import evaluated, implemented, judged
from tests.vibesys.vibesys_flows import start_flow

from vibesys.metrics import EvidenceReading
from vibesys.orchestration.issue_queue.search.api import (
    BOARD,
    EvidenceReadings,
    InterpretEvidence,
    IssueQueueConfig,
    ParentVerification,
    QueueSnapshot,
    issue_queue,
)
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    AttemptRef,
    CheckpointDecline,
    Declined,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    FailureReason,
    Measure,
    MeasurementFinished,
    MeasurementResult,
    ObservationStatus,
    Operation,
    RejectionCode,
    RequestId,
    RequestTurn,
    Retained,
    RevisionRef,
    RunFacts,
    Settle,
    StartAttempt,
    TurnFailureKind,
    Withdraw,
)
from vs_core.api.search import TaskStage, TaskTerms
from vs_core.api.testing import Asked, FakeView, FlowRun
from vs_core.testing.builders import initial_state
from vs_issue_tracker.api import (
    FakeIssueTracker,
    LogProgress,
    MemoryProgressLog,
)
from vs_runtime.api.core import IssueBoardOwner

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel

    from vibesys.orchestration.issue_queue.models import (
        IssueImplementerResult,
        IssueJudgeResult,
        IssuePerfEvalResult,
    )

TERMS = TaskTerms(
    recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
    stages=(
        TaskStage(stage_id="accuracy", execution_seconds=10),
        TaskStage(stage_id="benchmark", execution_seconds=20),
        TaskStage(stage_id="profile", execution_seconds=10),
    ),
    measured=("accuracy", "benchmark"),
    queue_allowance=5,
    accuracy_stage="accuracy",
    accuracy_gated=(EvidenceKind.BENCHMARK,),
    operation_seconds=30,
)
FACTS = initial_state().run.facts.model_copy(update={"task": TERMS})
"""The run facts every story starts from: the core's sample facts plus a task that measures."""

UNGATED = FACTS.model_copy(
    update={"task": TERMS.model_copy(update={"measured": ("benchmark",), "accuracy_stage": None})}
)
"""Facts of a task that measures no accuracy: adopting needs no proof."""

_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
    "profile": EvidenceKind.PROFILING,
}
IMPLEMENTER = "implementer"
JUDGE = "judge"
PERF = "perf-evaluator"
BOARD_KINDS = frozenset(item.descriptor.kind for item in BOARD)


def suspending_view() -> FakeView:
    """A host whose agents may end a turn waiting for their evaluations (a fresh view each time)."""
    capabilities = FakeView().current().capabilities
    return FakeView(
        changes={
            "capabilities": capabilities.model_copy(update={"lifecycle": frozenset({"suspend"})})
        }
    )


def config(**overrides: object) -> IssueQueueConfig:
    """Two rounds, two attempts per issue and room for four issues a round, unless told."""
    return IssueQueueConfig.model_validate(
        {
            "max_rounds": 2,
            "max_attempts_per_issue": 2,
            "max_issues_per_perf_eval": 2,
            "max_issues_per_round": 4,
            **overrides,
        }
    )


class _Texts:
    """Board texts that name their input, so a test can tell which template produced them."""

    def issue_body(self, template: str) -> str:
        return f"rendered {template}"

    def progress_entry(self, request: LogProgress) -> str:
        return f"{request.step} {request.iteration}"


class World(TypedDict, total=False):
    """How the world around the flow differs from the ordinary one."""

    facts: RunFacts
    implementer: Sequence[IssueImplementerResult]
    """The implementer's replies, in order; past them it reports a change again."""
    judge: Sequence[IssueJudgeResult]
    perf: Sequence[IssuePerfEvalResult]
    unchanged: bool
    """The implementer's checkpoint is the revision it started from: the tree did not change."""
    lost_acknowledgements: frozenset[int]
    """The n-th board write (from 0) lands, and the owner's acknowledgement is lost."""
    dropped_writes: frozenset[int]
    """The n-th board write is lost before it lands."""
    accurate: bool
    """Whether the trusted accuracy measurement passes."""
    suspending: bool
    """An agent turn may end waiting for its evaluations (the view must offer it)."""


def begin(
    cfg: IssueQueueConfig, *, restarting: bool, view: FakeView | None = None, **world: Unpack[World]
) -> Host:
    """Start the issue-queue search of ``cfg`` under a host; ``world`` sets how the far side behaves."""
    facts = world.get("facts", FACTS)
    view = view or FakeView(changes={"facts": facts})
    run = start_flow(issue_queue, cfg, facts=facts, restarting=restarting, view=view)
    return Host(run, cfg, **world)


@dataclass
class Host:
    """The world around an issue-queue flow."""

    run: FlowRun[IssueQueueConfig]
    config: IssueQueueConfig
    facts: RunFacts = FACTS
    implementer: Sequence[IssueImplementerResult] = ()
    judge: Sequence[IssueJudgeResult] = ()
    perf: Sequence[IssuePerfEvalResult] = ()
    unchanged: bool = False
    lost_acknowledgements: frozenset[int] = frozenset()
    dropped_writes: frozenset[int] = frozenset()
    accurate: bool = True
    suspending: bool = False
    tracker: FakeIssueTracker = field(default_factory=FakeIssueTracker)
    opened: list[AttemptRef] = field(default_factory=list)
    """Every attempt the host admitted, in order."""
    settled: dict[str, Settle] = field(default_factory=dict)
    """What each closed attempt settled with, by its attempt id."""
    refused_starts: int = 0
    writes: int = 0
    measured: list[RevisionRef] = field(default_factory=list)
    """The revisions the host measured, in order."""
    _replies: dict[str, deque[BaseModel]] = field(default_factory=dict)
    _handed: dict[str, int] = field(default_factory=lambda: {IMPLEMENTER: 0, JUDGE: 0, PERF: 0})
    _commits: int = 0
    _sequence: int = 0

    @property
    def baseline(self) -> RevisionRef:
        """The input revision the run starts from."""
        return self.facts.baseline

    def __post_init__(self) -> None:
        """Hold the scripted replies as queues, so a reply is handed out once."""
        self._replies = {
            IMPLEMENTER: deque(self.implementer),
            JUDGE: deque(self.judge),
            PERF: deque(self.perf),
        }
        self.owner = IssueBoardOwner(self.tracker, MemoryProgressLog(), _Texts())

    # -- what a test reads back ----------------------------------------------------------------

    @property
    def record(self) -> QueueSnapshot | None:
        """What the flow last published."""
        return self.run.published(QueueSnapshot)

    @property
    def unsettled(self) -> list[AttemptRef]:
        """Attempts that were opened and not closed."""
        return [item for item in self.opened if item.attempt_id.root not in self.settled]

    def board(self) -> dict[int, tuple[str, int]]:
        """Every issue's status and attempts, by number."""
        return {issue.id: (issue.status.value, issue.attempts) for issue in self.tracker.list()}

    def titles(self) -> list[str]:
        """The title of every issue, in the order the board lists them."""
        return [issue.title for issue in self.tracker.list()]

    def statuses(self) -> dict[str, str]:
        """Every issue's status, by title."""
        return {issue.title: issue.status.value for issue in self.tracker.list()}

    def answered(self, role: str) -> int:
        """How many of the scripted replies of ``role`` were handed out."""
        return self._handed[role]

    def open(self, kind: type, *, label: str | None = None) -> list[Asked]:
        """The open waits for a ``kind`` request."""
        return [
            asked
            for asked in self.run.open
            if isinstance(asked.request, kind) and label in (None, asked.label)
        ]

    # -- answering -----------------------------------------------------------------------------

    def answer(self, asked: Asked, variant: int = 0) -> None:
        """Answer one open wait; ``variant`` chooses among the ways that kind of wait can end."""
        request = asked.request
        match request:
            case RequestTurn():
                self._agent(asked, variant)
            case StartAttempt():
                self._start(asked, variant)
            case Withdraw():
                self._withdraw(asked, variant)
            case Measure():
                self._measure(asked, request, variant)
            case Operation():
                self._operation(asked, request)
            case _:
                message = f"nothing to answer {type(request).__name__}"
                raise TypeError(message)

    def step(self, pick: int, variant: int = 0) -> bool:
        """Answer the ``pick``-th open wait (modulo how many), or move the clock; False if idle."""
        answerable = [item for item in self.run.open if item.until is not None]
        if answerable:
            self.answer(answerable[pick % len(answerable)], variant)
            return True
        if self.run.sleeping_until:
            self.run.advance_to(min(self.run.sleeping_until))
            return True
        return False

    def drain_until_turn(self, role: str, *, limit: int = 80) -> None:
        """Answer the ordinary way until a turn of ``role`` is open."""
        for _ in range(limit):
            if any(item.label == role for item in self.run.open):
                return
            if not self.step(0):
                break
        message = f"no {role} turn opened: {self.run.timeline()}"
        raise AssertionError(message)

    def drain_until_measure(self, *, limit: int = 200) -> None:
        """Answer the ordinary way until a measurement is open."""
        for _ in range(limit):
            if self.open(Measure):
                return
            if not self.step(0):
                break
        message = f"no measurement opened: {self.run.timeline()}"
        raise AssertionError(message)

    def drain(self, *, limit: int = 500) -> None:
        """Answer everything the ordinary way until the flow waits for nothing more."""
        for _ in range(limit):
            if not self.step(0):
                return
        message = f"the flow did not settle in {limit} steps: {self.run.timeline()}"
        raise AssertionError(message)

    # -- agents --------------------------------------------------------------------------------

    def _agent(self, asked: Asked, variant: int) -> None:
        match variant % 7:
            case 6 if self.suspending and asked.label != PERF:
                self.run.resume_ready(self.run.suspend(asked))
            case 1:
                self.run.reply_raw(asked, "not json")
            case 2:
                self.run.lose(asked)
            case 3:
                self.run.fail_turn(asked, TurnFailureKind.PROVIDER_FAILED)
            case 4:
                self.run.fail(asked, FailureReason.TIMED_OUT)
            case _:
                self._reply(asked, variant)

    def _reply(self, asked: Asked, variant: int) -> None:
        role = asked.label
        queue = self._replies[role]
        self._handed[role] += 1
        reply = (
            queue.popleft()
            if queue
            else {IMPLEMENTER: implemented, JUDGE: judged, PERF: evaluated}[role]()
        )
        if role != IMPLEMENTER:
            self.run.reply(asked, reply)
        elif variant % 7 == 5:
            self.run.reply(asked, reply, checkpoint=Declined(reason=next(iter(CheckpointDecline))))
        else:
            self.run.reply(asked, reply, checkpoint=self._retained())

    def _retained(self) -> Retained:
        if self.unchanged:
            return Retained(revision=self.baseline)
        self._commits += 1
        return Retained(revision=RevisionRef.of_git_commit(f"c{self._commits}"))

    # -- workspace executor --------------------------------------------------------------------

    def _start(self, asked: Asked, variant: int) -> None:
        if variant % 4 == 3:
            self.refused_starts += 1
            self.run.fail(asked, FailureReason.REFUSED, cause=RejectionCode.BUDGET)
            return
        self.opened.append(self.run.open_workspace(asked))

    def _withdraw(self, asked: Asked, variant: int) -> None:
        request = asked.request
        assert isinstance(request, Withdraw)
        target = request.target
        assert isinstance(target, AttemptRef)
        disposition = request.disposition
        if variant % 4 == 3 and isinstance(disposition, Settle) and disposition.outcome != "failed":
            self.run.fail(asked, FailureReason.REFUSED, cause=RejectionCode.BUDGET)
            return  # core takes the plain close instead
        if isinstance(disposition, Settle):
            self.settled[target.attempt_id.root] = disposition
        self.run.close_workspace(asked)

    # -- evaluator and operations --------------------------------------------------------------

    def _measure(self, asked: Asked, request: Measure, variant: int) -> None:
        plan = request.plan
        assert isinstance(plan.candidate, RevisionRef)
        self.measured.append(plan.candidate)
        if variant % 7 == 2:
            self.run.fail(asked, FailureReason.LOST)
            return
        refs = tuple(
            EvidenceRef(
                evidence_id=EvidenceId(root=f"{stage.stage_id}:{plan.purpose}:{index}"),
                kind=_KINDS[stage.stage_id],
                purpose=plan.purpose,
                scope=request.scope,
                source_request=RequestId(root=f"measure-{index}"),
                candidate=plan.candidate,
                observation_sequence=0,
                evaluator_digest=plan.evaluator_digest,
                workload_digest=plan.workload_digest,
                environment_digest=plan.environment_digest,
                provenance="trusted",
                status=ObservationStatus.SUCCEEDED if self.accurate else ObservationStatus.FAILED,
            )
            for index, stage in enumerate(plan.stages)
        )
        result = MeasurementResult(
            scope=request.scope, evidence=refs, status=ObservationStatus.SUCCEEDED
        )
        self.run.answer(asked, MeasurementFinished(decision=asked.decision, result=result))

    def _operation(self, asked: Asked, request: Operation) -> None:
        typed = request.request
        if isinstance(typed, InterpretEvidence):
            readings = tuple(self._reading_of(ref) for ref in typed.evidence)
            self.run.finish_operation(
                asked, EvidenceReadings(status="succeeded", readings=readings)
            )
            return
        if typed.kind not in BOARD_KINDS:
            self.run.finish_operation(asked, ParentVerification(status="succeeded", verified=True))
            return
        is_write = typed.kind == "issue_board.apply_changes"
        number = self.writes
        if is_write:
            self.writes += 1
            if number in self.dropped_writes:
                self.run.fail(asked, FailureReason.LOST)
                return
        outcome = self.owner.perform(typed)
        if is_write and number in self.lost_acknowledgements:
            self.run.fail(asked, FailureReason.LOST)
            return
        self.run.finish_operation(asked, outcome)

    def _reading_of(self, ref: EvidenceRef) -> EvidenceReading:
        passed = ref.status is ObservationStatus.SUCCEEDED
        return EvidenceReading(
            evidence_id=ref.evidence_id,
            kind=ref.kind,
            passed=passed,
            stage=ref.kind.value,
            metrics=(),
        )


def play(host: Host, steps: Sequence[tuple[int, int]]) -> None:
    """Answer the open waits as ``steps`` say (which wait, which way), then in the ordinary way."""
    for pick, variant in steps:
        if not host.step(pick, variant):
            break
    host.drain()


def assert_flow_closed(host: Host) -> None:
    """The flow finished the run and left no attempt open and no wait unanswered."""
    assert host.run.finished is not None, host.run.timeline()
    assert not host.unsettled, host.run.timeline()
    assert not [item for item in host.run.open if item.until is not None], host.run.timeline()
