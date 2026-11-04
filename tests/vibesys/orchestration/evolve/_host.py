"""A fake core for the evolve search: it answers the waits the flow opens, as a story or by script.

`Host` plays everything outside the flow: the workspace executor (open, settle), the agents (the
mutator, the judge, the profiler), the evaluator (measure, read), the parent verifier and the
patch exporter. A story answers one wait at a time (`answer`); a generated test chooses which
open wait to answer and how (`step`). Nothing here has a clock or a thread: `FlowRun` is the whole
world.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict, Unpack

from tests.vibesys.vibesys_flows import start_flow

from vibesys.metrics import EvidenceReading, MetricRow, MetricSpace
from vibesys.orchestration.evolve.models import JudgeResponse, MutatorResponse
from vibesys.orchestration.evolve.search.api import (
    EvidenceReadings,
    EvolveConfig,
    EvolveSnapshot,
    ExportPatch,
    InterpretEvidence,
    ParentVerification,
    PatchExport,
    evolve,
)
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.review import Verdict
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
    Settle,
    StartAttempt,
    TurnFailureKind,
    Withdraw,
)
from vs_core.api.search import TaskObjective, TaskStage, TaskTerms
from vs_core.testing.builders import initial_state

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_core.api.testing import Asked, FakeView, FlowRun

TERMS = TaskTerms(
    recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
    stages=(
        TaskStage(stage_id="accuracy", execution_seconds=10),
        TaskStage(stage_id="benchmark", execution_seconds=20),
    ),
    measured=("accuracy", "benchmark"),
    queue_allowance=5,
    accuracy_stage="accuracy",
    accuracy_gated=(EvidenceKind.BENCHMARK,),
    operation_seconds=30,
)
FACTS = initial_state().run.facts.model_copy(update={"task": TERMS})
"""The run facts every story starts from: the core's sample facts plus a task that measures."""

_KINDS = {"accuracy": EvidenceKind.CORRECTNESS, "benchmark": EvidenceKind.BENCHMARK}
CANDIDATE_THROUGHPUT = 100.0
PATCH = "diff --git a/server.py b/server.py\n+pass\n"


def config(**overrides: object) -> EvolveConfig:
    """A one-generation, one-child config with two cold-start attempts, overridden by the test."""
    return EvolveConfig.model_validate(
        {
            "max_generations": 1,
            "children_per_generation": 1,
            "k_top_inspirations": 0,
            "k_random_inspirations": 0,
            "selection_temperature": 1.0,
            "frontier_bias": 0.7,
            "bootstrap_max_attempts": 2,
            "seed": 7,
            **overrides,
        }
    )


def begin(
    cfg: EvolveConfig,
    *,
    restarting: bool,
    view: FakeView | None = None,
    host_type: type[Host] | None = None,
    metric_space: MetricSpace | None = None,
    **world: Unpack[World],
) -> Host:
    """Start the evolve search of ``cfg`` under a host; ``world`` sets how the far side behaves."""
    facts = (
        FACTS if metric_space is None else FACTS.model_copy(update={"task": terms_in(metric_space)})
    )
    run = start_flow(evolve, cfg, facts=facts, restarting=restarting, view=view)
    return (host_type or Host)(run, cfg, **world)


def terms_in(space: MetricSpace) -> TaskTerms:
    """The task's terms with its candidates ranked in ``space``."""
    return TERMS.model_copy(
        update={
            "objectives": tuple(
                TaskObjective(name=item.name, direction=item.direction) for item in space.objectives
            )
        }
    )


class World(TypedDict, total=False):
    """How the world around the flow differs from the ordinary one."""

    throughputs: dict[str, float]
    """The benchmark throughput of a candidate, by its revision's id; the default is 100."""
    failing: set[str]
    """Revisions whose accuracy gate fails."""
    verified: bool
    """Whether a parent revision still reproduces its retained content."""
    export_status: str
    """How a patch export ends."""
    unmeasurable: bool
    """Whether every measurement ends without trusted evidence."""


@dataclass
class Host:
    """The world around an evolve flow."""

    run: FlowRun[EvolveConfig]
    config: EvolveConfig
    opened: list[AttemptRef] = field(default_factory=list)
    """Every attempt the host admitted, in order."""
    settled: set[str] = field(default_factory=set)
    """The attempts whose workspace the host closed."""
    commits: int = 0
    refusals: dict[str, int] = field(default_factory=dict)
    """For a kind of wait (its label), how many times core still refuses it."""
    throughputs: dict[str, float] = field(default_factory=dict)
    failing: set[str] = field(default_factory=set)
    verified: bool = True
    export_status: str = "succeeded"
    unmeasurable: bool = False
    produced: list[str] = field(default_factory=list)
    """The revisions the mutator produced, in order."""

    @property
    def record(self) -> EvolveSnapshot | None:
        """What the flow last published."""
        return self.run.published(EvolveSnapshot)

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
            self.run.fail(asked, FailureReason.REFUSED, cause=RejectionCode.BUDGET)
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

    def drain_until_turn(self, role: str, *, limit: int = 100) -> None:
        """Answer the ordinary way until a turn of ``role`` is open."""
        for _ in range(limit):
            if any(item.label == role for item in self.run.open):
                return
            if not self.step(0):
                break
        message = f"no {role} turn opened: {self.run.timeline()}"
        raise AssertionError(message)

    def drain(self, *, limit: int = 1000) -> None:
        """Answer everything the ordinary way until the flow waits for nothing more."""
        for _ in range(limit):
            if not self.step(0):
                return
        message = f"the flow did not settle in {limit} steps: {self.run.timeline()}"
        raise AssertionError(message)

    # -- agents --------------------------------------------------------------------------------

    def _agent(self, asked: Asked, variant: int) -> None:
        if asked.label == "mutator":
            self._mutator(asked, variant)
        elif asked.label == "judge":
            self._judge(asked, variant)
        else:
            self._profiler(asked, variant)

    def mutated(self, asked: Asked, *, revision: str | None = None) -> str:
        """The mutator leaves a candidate; its revision."""
        self.commits += 1
        name = revision or f"c{self.commits}"
        self.produced.append(name)
        self.run.reply(
            asked,
            MutatorResponse(summary=f"change {name}", hypothesis="faster", expected_behavior="up"),
            checkpoint=Retained(revision=RevisionRef.of_git_commit(name)),
        )
        return name

    def _mutator(self, asked: Asked, variant: int) -> None:
        match variant % 5:
            case 0 | 4:
                self.mutated(asked)
            case 1:
                reply = MutatorResponse(summary="x", hypothesis="y", expected_behavior="z")
                declined = Declined(reason=next(iter(CheckpointDecline)))
                self.run.reply(asked, reply, checkpoint=declined)
            case 2:
                self.run.fail_turn(asked, TurnFailureKind.PROVIDER_FAILED)
            case _:
                self.run.fail(asked, FailureReason.TIMED_OUT)

    def _judge(self, asked: Asked, variant: int) -> None:
        if variant % 3 == 2:
            self.run.fail_turn(asked, TurnFailureKind.PROVIDER_FAILED)
            return
        passed = variant % 3 == 0
        verdict = Verdict.PASS if passed else Verdict.FAIL
        feedback = "" if passed else "needs work"
        self.run.reply(asked, JudgeResponse(analysis="a", feedback=feedback, verdict=verdict))

    def _profiler(self, asked: Asked, variant: int) -> None:
        if variant % 2 == 1:
            self.run.fail_turn(asked, TurnFailureKind.PROVIDER_FAILED)
            return
        summary = ProfilerSummary(
            analysis="a",
            bottlenecks="b",
            suggestions="s",
            perf_metric=1.0,
            perf_unit="x",
            metrics={"cache_hit": 0.5},
        )
        self.run.reply(asked, summary)

    # -- workspace executor --------------------------------------------------------------------

    def _start(self, asked: Asked, variant: int) -> None:
        if variant % 4 == 3:
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
        self.settled.add(target.attempt_id.root)
        self.run.close_workspace(asked)

    # -- evaluator and operations --------------------------------------------------------------

    def _measure(self, asked: Asked, request: Measure) -> None:
        plan = request.plan
        assert isinstance(plan.candidate, RevisionRef)
        refs = tuple(
            EvidenceRef(
                evidence_id=EvidenceId(
                    root=f"{stage.stage_id}:{plan.purpose}:{plan.candidate.revision_id.root}"
                ),
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
                status=self._status_of(stage.stage_id, plan.candidate.revision_id.root),
            )
            for index, stage in enumerate(plan.stages)
        )
        result = MeasurementResult(
            scope=request.scope,
            evidence=() if self.unmeasurable else refs,
            status=ObservationStatus.FAILED if self.unmeasurable else ObservationStatus.SUCCEEDED,
        )
        self.run.answer(asked, MeasurementFinished(decision=asked.decision, result=result))

    def _status_of(self, stage: str, revision: str) -> ObservationStatus:
        broken = stage == "accuracy" and revision in self.failing
        return ObservationStatus.FAILED if broken else ObservationStatus.SUCCEEDED

    def _operation(self, asked: Asked, request: Operation) -> None:
        typed = request.request
        outcome: BaseModel
        if isinstance(typed, InterpretEvidence):
            outcome = EvidenceReadings(
                status="succeeded", readings=tuple(self._reading_of(ref) for ref in typed.evidence)
            )
        elif isinstance(typed, ExportPatch):
            outcome = self._export(typed)
        else:
            outcome = ParentVerification(status="succeeded", verified=self.verified)
        self.run.finish_operation(asked, outcome)

    def _export(self, request: ExportPatch) -> PatchExport:
        """What the patch exporter answers."""
        del request
        return PatchExport(status=self.export_status, patch=PATCH)  # ty: ignore[invalid-argument-type]

    def _reading_of(self, ref: EvidenceRef) -> EvidenceReading:
        """What the evidence owner decodes of a record: the host's idea of how it measured."""
        metrics = ()
        passed = ref.status is ObservationStatus.SUCCEEDED
        if ref.kind is EvidenceKind.BENCHMARK and passed:
            value = self.throughputs.get(ref.candidate.revision_id.root, CANDIDATE_THROUGHPUT)
            metrics = (MetricRow(name="throughput", value=value, direction="max", unit="tokens/s"),)
        return EvidenceReading(
            evidence_id=ref.evidence_id,
            kind=ref.kind,
            passed=passed,
            stage=ref.kind.value,
            metrics=metrics,
            feedback="" if passed else "accuracy gate failed",
        )
