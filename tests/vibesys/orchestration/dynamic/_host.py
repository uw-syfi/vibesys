"""A fake core for the dynamic search: it answers the waits the flow opens, as a story or by script.

`Host` plays everything outside the flow: the workspace executor (open, settle), the agents (the
planner, the implementer, the judge), the evaluator (measure, read) and the parent verifier. A
story answers one wait at a time (`answer`); a generated test chooses which open wait to answer
and how (`step`). Nothing here has a clock or a thread: `FlowRun` is the whole world.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

from tests.support.dynamic_script import REGISTRY
from tests.vibesys.orchestration.dynamic._replies import (
    abandon,
    implement,
    implemented,
    merge,
    merged,
    outline_reply,
    plan_reply,
    reviewed,
    step,
)
from tests.vibesys.vibesys_flows import start_flow

from vibesys.metrics import EvidenceReading
from vibesys.orchestration.dynamic.models import ImplementerResult, MergeResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import (
    WRITE_HANDOFF_KIND,
    DynamicConfig,
    EvidenceReadings,
    HandoffWritten,
    InterpretEvidence,
    MetricRow,
    ParentVerification,
    PlannerCorrection,
    PlannerPrompt,
    RunSnapshot,
    WriteHandoff,
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
    MeasurementFailure,
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
    from pydantic import BaseModel

    from vs_core.api import OperationRequest, Scope
    from vs_core.api.testing import Asked, FakeView, FlowRun, RestartPolicy

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
UNMEASURED_TERMS = TERMS.model_copy(update={"measured": ()})
"""A task with no stage configured to measure the input with."""
FACTS = initial_state().run.facts.model_copy(update={"task": TERMS})
"""The run facts every story starts from: the core's sample facts plus a task that measures."""

type InputEnd = Literal["measured", "failed-workload", "failed-accuracy", "no-evidence", "lost"]
"""How the input's measurement ends: measured; its benchmark or its accuracy check fails (a
workload failure, typed); it ends without evidence; or it is lost."""

_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
    "profile": EvidenceKind.PROFILING,
}
_REGISTRY = REGISTRY
PLANNER_LABEL = "orchestrator"
OUTLINER_LABEL = "outliner"
INPUT_PATH = "input/"
"""Where the waits of the task that measures the input are asked from."""
INPUT_THROUGHPUT = 50.0
CANDIDATE_THROUGHPUT = 100.0


def handoff_path(call: int) -> str:
    """Where the host keeps the handoff file of the ``call``-th planning call."""
    return f".vibesys/state/runs/run/orchestrator-handoff/calls/call-{call:04d}.md"


def config(**overrides: object) -> DynamicConfig:
    """A one-round, one-wide config with no backlog, overridden by the test.

    A story steps its workstreams one after another, so a workstream waiting for its measurement
    keeps its agent slot (``max_backlog=0``). The search's default parks it; the stories of the
    evaluation queue and the root's properties run with a backlog.
    """
    fields = {"max_rounds": 1, "max_in_flight": 1, "max_backlog": 0}
    return DynamicConfig.model_validate({**fields, **overrides})


PLAN_DOCUMENTS = ["reference/README.md"]


def plan_config(*steps: dict[str, object], **overrides: object) -> DynamicConfig:
    """A config that follows a plan of ``steps`` (none: the run outlines them), as JSON parses it."""
    plan = {"documents": PLAN_DOCUMENTS, "steps": list(steps)}
    fields = {"max_rounds": 1, "max_in_flight": 1, "mode": "follow_plan", "plan": plan}
    return DynamicConfig.model_validate_json(json.dumps({**fields, **overrides}))


def begin(
    cfg: DynamicConfig,
    *,
    restarting: bool | RestartPolicy,
    view: FakeView | None = None,
    adopting: bool = True,
    input_first: bool = True,
    **world: Unpack[World],
) -> Host:
    """Start the dynamic search of ``cfg`` under a host; ``world`` sets how the far side behaves.

    The search plans while it measures the input. With ``input_first`` (the default) the host
    answers the input's measurement before anything else, so a story starts from a measured input
    with the first planner turn open; a story about the overlap passes False.
    """
    run = start_flow(dynamic, cfg, facts=FACTS, restarting=restarting, view=view, adopting=adopting)
    host = Host(run, cfg, **world)
    if input_first:
        host.measure_input()
    return host


class World(TypedDict, total=False):
    """How the world around the flow differs from the ordinary one."""

    throughputs: dict[str, float]
    """The benchmark throughput of a candidate, by its revision's id; the default is 100."""
    failing_benchmarks: set[str]
    """The revision ids of candidates whose benchmark check fails."""
    verified: bool
    """Whether a parent revision still reproduces its retained content."""
    baseline: InputEnd
    """How the measurement of the input ends."""
    input_blame: MeasurementFailure | None
    """How the backend classified the input's failed measurement; None leaves it unclassified."""
    input_recovers: bool
    """The input's measurement ends as ``baseline`` once only: measured again, it is measured."""
    handoff_writes_open: bool
    """Leave each handoff write open for the story to answer; else the owner writes it at once."""
    merging: bool
    """The planner merges the mergeable candidates whenever no merge runs."""
    merger_answers: list[int]
    """How the merger answers its turns, in order (merged, merged, refused, failed); then any."""
    merge_picks: list[int]
    """Where each merge the planner asks for starts in the buildable hypotheses, the mergeable
    ones first, taking two in turn: a merge may name one the prompt did not list as mergeable.
    Once these run out, the planner merges the first two mergeable ones."""
    replans: list[int]
    """Which hypothesis each implement entry the planner writes names, in order: 0 a fresh ID;
    any other pick an ID the planner already named (the pick modulo how many), continued when the
    pick is odd and re-planned as new when it is even. Once these run out, every ID is fresh."""
    abandons: list[int]
    """For each plan the planner writes, in order: 0 nothing; any other pick also abandons an ID
    the planner already named (the pick modulo how many). Once these run out, nothing."""


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
    made: list[tuple[Asked, RevisionRef]] = field(default_factory=list)
    """Every candidate revision the implementer replied with, and the turn it answered."""
    handoffs: dict[str, WriteHandoff] = field(default_factory=dict)
    """The handoff files the host's owner holds, by path: the last write of each."""
    handoff_writes: int = 0
    """Every handoff write the host performed, a replayed one included."""
    _hypotheses: int = 0
    _commits: int = 0
    _events: int = 0
    refusals: dict[str, int] = field(default_factory=dict)
    """For a kind of wait (its label), how many times core still refuses it."""
    refusal_code: RejectionCode = RejectionCode.BUDGET
    throughputs: dict[str, float] = field(default_factory=dict)
    failing_benchmarks: set[str] = field(default_factory=set)
    verified: bool = True
    baseline: InputEnd = "measured"
    input_blame: MeasurementFailure | None = None
    input_recovers: bool = False
    input_measurements: int = 0
    """How many times the host measured the input."""
    handoff_writes_open: bool = False
    merging: bool = False
    merger_answers: list[int] = field(default_factory=list)
    merge_picks: list[int] = field(default_factory=list)
    unlisted_merges: int = 0
    """The merges asked for that named a candidate the prompt did not list as mergeable."""
    merger_sessions: dict[str, set[str]] = field(default_factory=dict)
    """The sessions the merger turns ran in, by merge ID."""
    _merges: int = 0
    replans: list[int] = field(default_factory=list)
    abandons: list[int] = field(default_factory=list)
    named: list[str] = field(default_factory=list)
    """Every hypothesis ID the planner named fresh, in order."""

    def __post_init__(self) -> None:
        """Have the handoff owner write each file at once, unless the story answers the writes."""
        if not self.handoff_writes_open:
            self.run.own(WRITE_HANDOFF_KIND, self.write_handoff)

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

    def input_waits(self) -> list[Asked]:
        """The open waits of the task that measures the input."""
        return [item for item in self.run.open if item.path == INPUT_PATH]

    def measure_input(self, *, limit: int = 10) -> None:
        """Answer the input's measurement, and the decoding of its evidence, the ordinary way."""
        for _ in range(limit):
            waits = self.input_waits()
            if not waits:
                return
            self.answer(waits[0])
        message = f"the input's measurement did not end in {limit} steps: {self.run.timeline()}"
        raise AssertionError(message)

    def drain_beside_input(self, *, limit: int = 100) -> None:
        """Answer every open wait but the input's the ordinary way, without moving the clock."""
        for _ in range(limit):
            others = [
                item for item in self.run.open if item.until is not None and item.path != INPUT_PATH
            ]
            if not others:
                return
            self.answer(others[0])
        message = f"waits beside the input's did not end in {limit} steps: {self.run.timeline()}"
        raise AssertionError(message)

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
        elif asked.label == OUTLINER_LABEL:
            self._outliner(asked, variant)
        elif asked.label == "implementer":
            self._implementer(asked, variant)
        elif asked.label == "merger":
            self._merger(asked, variant)
        else:
            self._judge(asked, variant)

    def _planner(self, asked: Asked, variant: int) -> None:
        self.planner_calls += 1
        match variant % 5:
            case 0 | 1:
                size = self._portfolio_size() if variant % 5 == 0 else 1
                entries = self._merge_entries(asked)
                entries.extend(self._implement_entry() for _ in range(size - len(entries)))
                self.run.reply_raw(asked, plan_reply(*entries, updates=self._abandon()))
            case 2:
                self.run.reply_raw(asked, "not json")
            case 3:
                self.run.fail(asked, FailureReason.TIMED_OUT)
            case _:
                self.run.lose(asked)

    def _implement_entry(self) -> dict[str, object]:
        """A fresh hypothesis, or one already named, as the next of ``replans`` says."""
        pick = self.replans.pop(0) if self.replans else 0
        if pick == 0 or not self.named:
            self._hypotheses += 1
            self.named.append(f"h{self._hypotheses}")
            return implement(self.named[-1])
        return implement(self.named[pick % len(self.named)], continue_hypothesis=pick % 2 == 1)

    def _abandon(self) -> list[dict[str, object]]:
        """The hypothesis the next of ``abandons`` retires with this plan, if any."""
        pick = self.abandons.pop(0) if self.abandons else 0
        if pick == 0 or not self.named:
            return []
        return [abandon(self.named[pick % len(self.named)])]

    def _outliner(self, asked: Asked, variant: int) -> None:
        """Outline two steps, the second building on the first; or fail the turn."""
        match variant % 3:
            case 0:
                first, second = step("first"), step("second", "trusted", depends_on=("first",))
                self.run.reply_raw(asked, outline_reply(first, second))
            case 1:
                self.run.reply_raw(asked, outline_reply(step("loop", depends_on=("loop",))))
            case _:
                self.run.fail(asked, FailureReason.TIMED_OUT)

    def _merge_entries(self, asked: Asked) -> list[dict[str, object]]:
        """A merge, when the host merges and no merge runs, of what ``merge_picks`` chooses."""
        context = asked.context
        assert isinstance(context, PlannerPrompt | PlannerCorrection)
        if not self.merging or context.merge_running is not None:
            return []
        listed = [row.hypothesis_id for row in context.mergeable]
        pool = list(dict.fromkeys([*listed, *(row.hypothesis_id for row in context.buildable)]))
        if self.merge_picks and pool:
            start = self.merge_picks.pop(0)
            chosen = list(dict.fromkeys(pool[(start + n) % len(pool)] for n in range(2)))
        else:
            chosen = listed[:2]
        if not chosen:
            return []
        self._merges += 1
        self.unlisted_merges += int(not set(chosen) <= set(listed))
        return [merge(f"m{self._merges}", *chosen)]

    def _merger(self, asked: Asked, variant: int) -> None:
        request = asked.request
        assert isinstance(request, RequestTurn)
        merge_id = str(getattr(asked.context, "merge_id", ""))
        self.merger_sessions.setdefault(merge_id, set()).add(request.turn.session.session_id.root)
        if self.merger_answers:
            variant = self.merger_answers.pop(0)
        match variant % 4:
            case 0 | 1:
                result = MergeResult.model_validate_json(merged())
                self.run.reply(asked, result, checkpoint=self._retained())
            case 2:
                result = MergeResult.model_validate_json(merged("refused", "they conflict"))
                self.run.reply(asked, result)
            case _:
                self.run.fail_turn(asked, TurnFailureKind.PROVIDER_FAILED)

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
                retained = self._retained()
                self.made.append((asked, retained.revision))
                self.run.reply(asked, result, checkpoint=retained)
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
        if is_input:
            if self.input_recovers and self.input_measurements:
                self.baseline = "measured"
            self.input_measurements += 1
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
                    status=ObservationStatus.FAILED
                    if self._benchmark_fails(stage.stage_id, candidate, is_input=is_input)
                    else self._status_of(stage.stage_id, is_input=is_input),
                )
                for index, stage in enumerate(plan.stages)
            )
        )
        failed = not refs
        result = MeasurementResult(
            scope=request.scope,
            evidence=refs,
            status=ObservationStatus.FAILED if failed else ObservationStatus.SUCCEEDED,
            failure=self.input_blame if is_input and self.baseline != "measured" else None,
        )
        self.run.answer(asked, MeasurementFinished(decision=asked.decision, result=result))

    def _benchmark_fails(self, stage: str, candidate: RevisionRef, *, is_input: bool) -> bool:
        """Whether the benchmark of a candidate the world was told fails it does."""
        failing = candidate.revision_id.root in self.failing_benchmarks
        return not is_input and failing and stage == "benchmark"

    def _status_of(self, stage: str, *, is_input: bool) -> ObservationStatus:
        broken = is_input and (
            (self.baseline == "failed-workload" and stage == "benchmark")
            or (self.baseline == "failed-accuracy" and stage == "accuracy")
        )
        return ObservationStatus.FAILED if broken else ObservationStatus.SUCCEEDED

    def _operation(self, asked: Asked, request: Operation) -> None:
        typed = request.request
        outcome: BaseModel
        if isinstance(typed, InterpretEvidence):
            outcome = EvidenceReadings(
                status="succeeded", readings=tuple(self._reading_of(ref) for ref in typed.evidence)
            )
        elif isinstance(typed, WriteHandoff):
            outcome = self.write_handoff(typed)
        else:
            outcome = ParentVerification(status="succeeded", verified=self.verified)
        self.run.finish_operation(asked, outcome)

    def write_handoff(self, request: OperationRequest) -> HandoffWritten:
        """Keep a handoff file at the path a real owner would give the planning call."""
        assert isinstance(request, WriteHandoff)
        path = handoff_path(request.call)
        self.handoffs[path] = request
        self.handoff_writes += 1
        held = sorted(self.handoffs.items(), key=lambda item: item[1].call)
        return HandoffWritten(path=path, listed=tuple(name for name, _ in held))

    def _reading_of(self, ref: EvidenceRef) -> EvidenceReading:
        """What the evidence owner decodes of a record: the host's idea of how it measured."""
        metrics = ()
        passed = ref.status is ObservationStatus.SUCCEEDED
        if ref.kind is EvidenceKind.BENCHMARK and passed:
            value = (
                INPUT_THROUGHPUT
                if ref.purpose == "baseline"
                else self.throughputs.get(ref.candidate.revision_id.root, CANDIDATE_THROUGHPUT)
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
