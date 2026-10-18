"""A fake core for the serial searches: it answers the waits their flows open.

`SerialHost` plays everything outside the flow that is not an agent: the workspace executor
(open, settle), the evaluator (measure, read) and the operations (read evidence, capture, validate
local, and the roadmap, through its production owner over a Fake roadmap). A search's own host adds its agents (`answer_agent`). A story answers one wait at a time
(`answer`); a generated test chooses which open wait to answer and how (`step`). Nothing here
has a clock or a thread: `FlowRun` is the whole world.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Self, TypedDict, Unpack, cast

from tests.vibesys.vibesys_flows import start_flow

from vibesys.hypothesis import HypothesisState
from vibesys.metrics import EvidenceReading, MetricRow
from vibesys.serial_roadmap import RoadmapOwner
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    AttemptRef,
    CaptureCommandBase,
    CommandCapture,
    Comparison,
    ComparisonVerdict,
    EventId,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    FailureReason,
    InterpretEvidenceBase,
    LocalValidationReport,
    Measure,
    MeasurementFinished,
    MeasurementResult,
    Observation,
    ObservationStatus,
    Operation,
    ParentVerification,
    RejectionCode,
    RequestId,
    RequestTurn,
    RevisionRef,
    Settle,
    StartAttempt,
    ValidateLocalBase,
    Withdraw,
)
from vs_core.api.search import TaskStage, TaskTerms
from vs_core.testing.builders import initial_state
from vs_issue_tracker.api import FakeRoadmap, PutRoadmapItems, ReadRoadmap

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_core.api import Scope, SearchDef
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

KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
    "profile": EvidenceKind.PROFILING,
}
INPUT_THROUGHPUT = 50.0
CANDIDATE_THROUGHPUT = 100.0

type Baseline = Literal["measured", "failed-workload", "no-evidence", "lost"]
type Validation = Literal["passed", "failed", "unusable", "errored"]


class World(TypedDict, total=False):
    """How the world around a serial flow differs from the ordinary one."""

    refusals: dict[str, int]
    refusal_code: RejectionCode
    throughputs: dict[str, float]
    comparisons: dict[str, tuple[float, float]]
    baseline: Baseline
    validation: Validation
    attribution_output: str
    attribution_exit_code: int
    roadmap: FakeRoadmap


@dataclass
class SerialHost:
    """The world around a serial flow, apart from its agents."""

    run: FlowRun[object]
    opened: list[AttemptRef] = field(default_factory=list)
    """Every attempt the host admitted, in order."""
    settled: set[str] = field(default_factory=set)
    """The attempts whose workspace the host closed."""
    refused_starts: int = 0
    refused_closes: int = 0
    refusals: dict[str, int] = field(default_factory=dict)
    """For a kind of wait (its label), how many times core still refuses it."""
    refusal_code: RejectionCode = RejectionCode.BUDGET
    throughputs: dict[str, float] = field(default_factory=dict)
    """The benchmark throughput of a candidate, by its revision's id; the default is 100."""
    comparisons: dict[str, tuple[float, float]] = field(default_factory=dict)
    """For a candidate, by its revision's id, the (ratio, noise) over its base that core's
    comparison of its measured samples gives; a candidate named here is compared, any other has
    no base's samples."""
    baseline: Baseline = "measured"
    validation: Validation = "passed"
    attribution_output: str = ""
    attribution_exit_code: int = 0
    roadmap: FakeRoadmap = field(default_factory=FakeRoadmap)
    """The run's roadmap, which the production owner reads and writes."""
    commits: int = 0
    events: int = 0
    measured: list[str] = field(default_factory=list)
    """The revision of every official measurement the host ran."""

    @classmethod
    def begin[C](
        cls,
        search: SearchDef[C],
        config: C,
        *,
        restarting: bool,
        view: FakeView | None = None,
        **world: Unpack[World],
    ) -> Self:
        """Start ``search`` of ``config`` under a host; ``world`` sets how the far side behaves."""
        run = start_flow(search, config, facts=FACTS, restarting=restarting, view=view)
        return cls(cast("FlowRun[object]", run), **world)

    @property
    def record(self) -> HypothesisState | None:
        """What the flow last published."""
        return self.run.published(HypothesisState)

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

    def retained(self) -> RevisionRef:
        """A fresh revision, as core's checkpoint of an agent's work names it."""
        self.commits += 1
        return RevisionRef.of_git_commit(f"c{self.commits}")

    # -- answering -----------------------------------------------------------------------------

    def answer_agent(self, asked: Asked, variant: int) -> None:
        """Answer a turn of one of the search's agents; the search's host says how."""
        raise NotImplementedError

    def answer(self, asked: Asked, variant: int = 0) -> None:
        """Answer one open wait; ``variant`` chooses among the ways that kind of wait can end."""
        request = asked.request
        if self.refusals.get(asked.label, 0) > 0:
            self.refusals[asked.label] -= 1
            self._refuse(asked)
            return
        match request:
            case RequestTurn():
                self.answer_agent(asked, variant)
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
            self.refused_closes += 1
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
        if not is_input:
            self.measured.append(candidate.revision_id.root)
        refs = (
            ()
            if is_input and self.baseline == "no-evidence"
            else tuple(
                EvidenceRef(
                    evidence_id=EvidenceId(root=f"{stage.stage_id}:{plan.purpose}:{index}"),
                    kind=KINDS[stage.stage_id],
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
        comparison = self._comparison(request.scope, candidate) if not is_input else None
        self.run.answer(
            asked,
            MeasurementFinished(decision=asked.decision, result=result, comparison=comparison),
        )

    def _comparison(self, scope: Scope, candidate: RevisionRef) -> Comparison | None:
        """What core's comparison of ``candidate`` with its base says, for a candidate named in
        `comparisons`: the ratio and noise are the world's, the rest follows from them."""
        named = self.comparisons.get(candidate.revision_id.root)
        if named is None:
            return None
        ratio, noise = named
        gain = ratio - 1
        verdict = (
            ComparisonVerdict.BETTER
            if gain > 2 * noise
            else ComparisonVerdict.WORSE
            if gain < -2 * noise
            else ComparisonVerdict.WITHIN_NOISE
        )
        return Comparison(
            scope=scope,
            metric="throughput",
            direction="max",
            candidate=candidate,
            candidate_input=f"input-{candidate.revision_id.root}",
            base=FACTS.baseline,
            base_input="input-base",
            candidate_value=INPUT_THROUGHPUT * ratio,
            base_value=INPUT_THROUGHPUT,
            ratio=ratio,
            noise=noise,
            verdict=verdict,
            candidate_samples=1,
            base_samples=1,
        )

    def _status_of(self, stage: str, *, is_input: bool) -> ObservationStatus:
        broken = is_input and self.baseline == "failed-workload" and stage == "benchmark"
        return ObservationStatus.FAILED if broken else ObservationStatus.SUCCEEDED

    def _operation(self, asked: Asked, request: Operation) -> None:
        typed = request.request
        outcome: BaseModel
        if isinstance(typed, InterpretEvidenceBase):
            outcome = typed.outcome_model.model_validate(
                {
                    "status": "succeeded",
                    "readings": tuple(self._reading_of(ref) for ref in typed.evidence),
                }
            )
        elif isinstance(typed, CaptureCommandBase):
            outcome = CommandCapture(
                status="succeeded",
                exit_code=self.attribution_exit_code,
                output=self.attribution_output,
            )
        elif isinstance(typed, ValidateLocalBase):
            outcome = self._validated()
        elif isinstance(typed, ReadRoadmap | PutRoadmapItems):
            outcome = RoadmapOwner(self.roadmap).perform(typed)
        else:
            outcome = ParentVerification(status="succeeded", verified=True)
        self.run.finish_operation(asked, outcome)

    def _validated(self) -> LocalValidationReport:
        match self.validation:
            case "passed":
                return LocalValidationReport(status="succeeded", passed=True)
            case "failed":
                return LocalValidationReport(
                    status="succeeded", recipe="check", error="boom", output="trace"
                )
            case "unusable":
                return LocalValidationReport(
                    status="succeeded", recipe_unusable=True, error="no such file"
                )
            case _:
                return LocalValidationReport(status="failed", detail="the runner broke")

    def _reading_of(self, ref: EvidenceRef) -> EvidenceReading:
        """What the evidence owner decodes of a record: the host's idea of how it measured.

        An agent's own evaluation is one ``local-validation`` record per stage, and its evidence
        ID starts with the stage's name (``benchmark:...``).
        """
        metrics = ()
        passed = ref.status is ObservationStatus.SUCCEEDED
        own = ref.purpose == "local-validation"
        stage = ref.evidence_id.root.split(":")[0] if own else ref.kind.value
        if passed and (ref.kind is EvidenceKind.BENCHMARK or (own and stage == "benchmark")):
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
            stage=stage,
            metrics=metrics,
        )

    def observation(self, scope: Scope) -> Observation:
        """A terminal observation of a request in ``scope``."""
        self.events += 1
        return Observation(
            event_id=EventId(root=f"event-{self.events}"),
            request_id=RequestId(root=f"request-{self.events}"),
            scope=scope,
            sequence=1,
            observed_at=self.run.view.now_at,
            status=ObservationStatus.SUCCEEDED,
            terminal=True,
            released=True,
        )
