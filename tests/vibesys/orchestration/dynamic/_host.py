"""A fake core for the dynamic search: it answers the waits the flow opens, as a story or by script.

`Host` plays everything outside the flow: the workspace executor (open, settle), the agents (the
planner, the implementer, the judge), the evaluator (measure, read) and the parent verifier. A
story answers one wait at a time (`answer`); a generated test chooses which open wait to answer
and how (`step`). Nothing here has a clock or a thread: `FlowRun` is the whole world.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

from tests.support.vibesys_flows import start_flow
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)

from vibesys.metrics import EvidenceReading
from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import (
    OPERATIONS,
    DynamicConfig,
    EvidenceReadings,
    InterpretEvidence,
    MetricRow,
    ParentVerification,
    RunSnapshot,
    dynamic,
)
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    AttemptRef,
    CheckpointDecline,
    Declined,
    EventId,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    FailureReason,
    InvocationId,
    InvocationRef,
    Measure,
    MeasurementFinished,
    MeasurementResult,
    Observation,
    ObservationStatus,
    Operation,
    OperationFinished,
    OperationId,
    OperationResult,
    RejectionCode,
    RequestId,
    RequestTurn,
    Retained,
    RevisionRef,
    SessionId,
    Settle,
    StartAttempt,
    TurnFailureKind,
    Withdraw,
)
from vs_core.api.search import TaskStage, TaskTerms
from vs_core.testing.builders import initial_state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel

    from vs_core.api import OperationRequest, Scope
    from vs_core.api.testing import Asked, FakeView, FlowRun

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

_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
    "profile": EvidenceKind.PROFILING,
}
_REGISTRY = OPERATIONS.registry()
PLANNER_LABEL = "orchestrator"
INPUT_THROUGHPUT = 50.0
_CANDIDATE_THROUGHPUT = 100.0


def config(**overrides: object) -> DynamicConfig:
    """A one-round, one-wide config, overridden by the test."""
    return DynamicConfig.model_validate({"max_rounds": 1, "max_in_flight": 1, **overrides})


def begin(
    cfg: DynamicConfig,
    *,
    restarting: bool,
    view: FakeView | None = None,
    adopting: bool = True,
    **world: Unpack[World],
) -> Host:
    """Start the dynamic search of ``cfg`` under a host; ``world`` sets how the far side behaves."""
    run = start_flow(dynamic, cfg, facts=FACTS, restarting=restarting, view=view, adopting=adopting)
    return Host(run, cfg, **world)


class World(TypedDict, total=False):
    """How the world around the flow differs from the ordinary one."""

    throughputs: dict[str, float]
    """The benchmark throughput of a candidate, by its revision's id; the default is 100."""
    verified: bool
    """Whether a parent revision still reproduces its retained content."""
    baseline: Literal["measured", "failed-workload", "no-evidence", "lost"]
    """How the measurement of the input ends."""


@dataclass
class Host:
    """The world around a dynamic flow."""

    run: FlowRun[DynamicConfig]
    config: DynamicConfig
    opened: list[AttemptRef] = field(default_factory=list)
    """Every attempt the host admitted, in order."""
    refused_starts: int = 0
    settled: set[str] = field(default_factory=set)
    """The attempts whose workspace the host closed."""
    planner_calls: int = 0
    _hypotheses: int = 0
    _commits: int = 0
    _events: int = 0
    refusals: dict[str, int] = field(default_factory=dict)
    """For a kind of wait (its label), how many times core still refuses it."""
    refusal_code: RejectionCode = RejectionCode.BUDGET
    throughputs: dict[str, float] = field(default_factory=dict)
    verified: bool = True
    baseline: Literal["measured", "failed-workload", "no-evidence", "lost"] = "measured"

    @property
    def record(self) -> RunSnapshot | None:
        """What the flow last published."""
        return self.run.published(RunSnapshot)

    @property
    def unsettled(self) -> list[AttemptRef]:
        """Attempts that were opened and not closed."""
        return [item for item in self.opened if item.attempt_id.root not in self.settled]

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
        if self.refusals.get(asked.label, 0) > 0:
            self.refusals[asked.label] -= 1
            self._refuse(asked)
            return
        match request:
            case RequestTurn():
                self._agent(asked, variant)
            case StartAttempt():
                self._start(asked, variant)
            case Withdraw():
                self._withdraw(asked, variant)
            case Measure():
                self._measure(asked, request)
            case Operation():
                self._operation(asked, request)
            case _:
                message = f"nothing to answer {type(request).__name__}"
                raise TypeError(message)

    def _refuse(self, asked: Asked) -> None:
        if isinstance(asked.request, StartAttempt):
            self.refused_starts += 1
        self.run.fail(asked, FailureReason.REFUSED, cause=self.refusal_code)

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

    def drain_until_turn(self, role: str, *, limit: int = 50) -> None:
        """Answer the ordinary way until a turn of ``role`` is open."""
        for _ in range(limit):
            if any(item.label == role for item in self.run.open):
                return
            if not self.step(0):
                break
        message = f"no {role} turn opened: {self.run.timeline()}"
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
        if asked.label == PLANNER_LABEL:
            self._planner(asked, variant)
        elif asked.label == "implementer":
            self._implementer(asked, variant)
        else:
            self._judge(asked, variant)

    def _planner(self, asked: Asked, variant: int) -> None:
        self.planner_calls += 1
        match variant % 5:
            case 0 | 1:
                size = self._portfolio_size() if variant % 5 == 0 else 1
                entries = []
                for _ in range(size):
                    self._hypotheses += 1
                    entries.append(implement(f"h{self._hypotheses}"))
                self.run.reply_raw(asked, plan_reply(*entries))
            case 2:
                self.run.reply_raw(asked, "not json")
            case 3:
                self.run.fail(asked, FailureReason.TIMED_OUT)
            case _:
                self.run.lose(asked)

    def _portfolio_size(self) -> int:
        """How many workstreams fill the slots free now, within the budget left."""
        record = self.record
        busy = 0 if record is None else len(record.running)
        started = 0 if record is None else busy + len(record.finished)
        left = self.config.start_budget - started + self.refused_starts
        return max(1, min(self.config.max_in_flight - busy, left))

    def _implementer(self, asked: Asked, variant: int) -> None:
        match variant % 6:
            case 0 | 2:
                outcome = "supported" if variant % 6 == 0 else "implementation_failed"
                result = ImplementerResult.model_validate_json(implemented(outcome))
                self.run.reply(asked, result, checkpoint=self._retained())
            case 1:
                self.run.reply(asked, ImplementerResult.model_validate_json(implemented("blocked")))
            case 3:
                self.run.fail_turn(asked, TurnFailureKind.PROVIDER_FAILED)
            case 4:
                self.run.fail(asked, FailureReason.TIMED_OUT)
            case _:
                result = ImplementerResult.model_validate_json(implemented())
                declined = Declined(reason=next(iter(CheckpointDecline)))
                self.run.reply(asked, result, checkpoint=declined)

    def _retained(self) -> Retained:
        self._commits += 1
        return Retained(revision=RevisionRef.of_git_commit(f"c{self._commits}"))

    def _judge(self, asked: Asked, variant: int) -> None:
        if variant % 3 == 2:
            self.run.fail_turn(asked, TurnFailureKind.PROVIDER_FAILED)
            return
        self.run.reply(asked, ReviewResult.model_validate_json(reviewed(passed=variant % 3 == 0)))

    def _turn(self) -> InvocationRef:
        self._events += 1
        return InvocationRef(
            session_id=SessionId(root="session"),
            invocation_id=InvocationId(root=f"lost-{self._events}"),
            generation=0,
        )

    # -- workspace executor --------------------------------------------------------------------

    def _start(self, asked: Asked, variant: int) -> None:
        if variant % 4 == 3:
            self._refuse(asked)
            return
        self.opened.append(self.run.open_workspace(asked))

    def _withdraw(self, asked: Asked, variant: int) -> None:
        request = asked.request
        assert isinstance(request, Withdraw)
        target = request.target
        assert isinstance(target, AttemptRef)
        disposition = request.disposition
        if variant % 4 == 3 and isinstance(disposition, Settle) and disposition.outcome != "failed":
            self._refuse(asked)  # core takes the plain close instead
            return
        self.settled.add(target.attempt_id.root)
        self.run.close_workspace(asked)

    # -- evaluator and operations --------------------------------------------------------------

    def _measure(self, asked: Asked, request: Measure) -> None:
        plan = request.plan
        assert isinstance(plan.candidate, RevisionRef)
        candidate = plan.candidate
        is_input = plan.purpose == "baseline"
        if is_input and self.baseline == "lost":
            self.run.fail(asked, FailureReason.LOST)
            return
        refs = (
            ()
            if is_input and self.baseline == "no-evidence"
            else tuple(
                EvidenceRef(
                    evidence_id=EvidenceId(root=f"{stage.stage_id}:{plan.purpose}:{index}"),
                    kind=_KINDS[stage.stage_id],
                    purpose=plan.purpose,
                    scope=request.scope,
                    source_request=RequestId(root=f"measure-{index}"),
                    candidate=candidate,
                    observation_sequence=0,
                    evaluator_digest=plan.evaluator_digest,
                    workload_digest=plan.workload_digest,
                    environment_digest=plan.environment_digest,
                    provenance="trusted",
                    status=self._status_of(stage.stage_id, is_input=is_input),
                )
                for index, stage in enumerate(plan.stages)
            )
        )
        failed = not refs
        result = MeasurementResult(
            scope=request.scope,
            evidence=refs,
            status=ObservationStatus.FAILED if failed else ObservationStatus.SUCCEEDED,
        )
        self.run.answer(asked, MeasurementFinished(decision=asked.decision, result=result))

    def _status_of(self, stage: str, *, is_input: bool) -> ObservationStatus:
        broken = is_input and self.baseline == "failed-workload" and stage == "benchmark"
        return ObservationStatus.FAILED if broken else ObservationStatus.SUCCEEDED

    def _operation(self, asked: Asked, request: Operation) -> None:
        typed = request.request
        outcome: BaseModel
        if isinstance(typed, InterpretEvidence):
            outcome = EvidenceReadings(
                status="succeeded", readings=tuple(self._reading_of(ref) for ref in typed.evidence)
            )
        else:
            outcome = ParentVerification(status="succeeded", verified=self.verified)
        self.run.finish_operation(asked, outcome)

    def _reading_of(self, ref: EvidenceRef) -> EvidenceReading:
        """What the evidence owner decodes of a record: the host's idea of how it measured."""
        metrics = ()
        passed = ref.status is ObservationStatus.SUCCEEDED
        if ref.kind is EvidenceKind.BENCHMARK and passed:
            value = (
                INPUT_THROUGHPUT
                if ref.purpose == "baseline"
                else self.throughputs.get(ref.candidate.revision_id.root, _CANDIDATE_THROUGHPUT)
            )
            metrics = (MetricRow(name="throughput", value=value, direction="max", unit="tokens/s"),)
        return EvidenceReading(
            evidence_id=ref.evidence_id,
            kind=ref.kind,
            passed=passed,
            stage=ref.kind.value,
            metrics=metrics,
        )

    def _finished(
        self, asked: Asked, typed: OperationRequest, outcome: BaseModel, request: Operation
    ) -> OperationFinished:
        schema = _REGISTRY.encode(typed).schema_ref
        result = OperationResult(
            operation_id=OperationId(root=f"operation-{asked.decision.root}"),
            observation=self._observation(request.scope),
            outcome_schema=schema.outcome_schema,
            operation_schema=schema,
            outcome=outcome,
        )
        return OperationFinished(decision=asked.decision, result=_REGISTRY.validate_event(result))

    def _observation(self, scope: Scope) -> Observation:
        self._events += 1
        return Observation(
            event_id=EventId(root=f"event-{self._events}"),
            request_id=RequestId(root=f"request-{self._events}"),
            scope=scope,
            sequence=1,
            observed_at=self.run.view.now_at,
            status=ObservationStatus.SUCCEEDED,
            terminal=True,
            released=True,
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
