"""A fake core and runtime for driving `DynamicStrategy` end to end without agents.

`FakeCore` answers each proposed decision the way core and the executors would,
deterministically: it admits attempts, records retained checkpoints and
measurements in the `RunView`, interprets evidence from scripted readings, and
returns scripted agent replies. It implements only the contracts the strategy
relies on and never inspects strategy state, so every assertion goes through the
strategy's public `decide`/`on_event` and the decisions it proposes.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic.strategy._views import empty_view, revision

from vibesys.orchestration.dynamic.strategy.api import (
    EvidenceReading,
    EvidenceReadings,
    InterpretEvidence,
    MetricRow,
    ParentVerification,
    PartialRow,
    RenderedArtifacts,
    VerifyParentRevision,
    dynamic_operation_registry,
)
from vs_core.api import (
    Accepted,
    AdoptionResult,
    ArtifactId,
    ArtifactRef,
    AttemptCheckpoint,
    AttemptPhase,
    AttemptReady,
    AttemptRef,
    AttemptSettled,
    AttemptView,
    Decision,
    EventId,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    InvocationRef,
    Measure,
    MeasurementResult,
    Observation,
    ObservationStatus,
    Operation,
    OperationId,
    OperationResult,
    ProposeWinner,
    RequestId,
    RequestTurn,
    RevisionRef,
    RunEnded,
    RunView,
    Scope,
    Settlement,
    SettlementId,
    StartAttempt,
    Stop,
    StrategyEvent,
    TurnResult,
    TurnSpec,
    Withdraw,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.orchestration.dynamic.strategy._strategy import DynamicStrategy


def plan_reply(*entries: dict[str, object], reasoning: str = "portfolio") -> str:
    """A planner reply of implement or profile entries."""
    return json.dumps({"reasoning": reasoning, "workstreams": list(entries)})


def implement(identifier: str, **extra: object) -> dict[str, object]:
    """One implement entry of a plan."""
    return {
        "kind": "implement",
        "hypothesis_id": identifier,
        "title": f"title {identifier}",
        "hypothesis": f"claim {identifier}",
        "task": f"task {identifier}",
        "pass_criteria": "faster",
        **extra,
    }


def implemented(outcome: str = "supported", summary: str = "done", **extra: object) -> str:
    """An implementer result reply."""
    return json.dumps({"summary": summary, "outcome": outcome, **extra})


def reviewed(*, passed: bool = True) -> str:
    """A judge result reply."""
    return json.dumps(
        {"passed": passed, "analysis": "analysis", "feedback": "" if passed else "fix"}
    )


@dataclass
class Script:
    """What the fake agents and evaluators answer, in order."""

    planner: deque[str] = field(default_factory=deque)
    implementer: deque[str] = field(default_factory=deque)
    judge: deque[str] = field(default_factory=deque)
    profiler: deque[str] = field(default_factory=deque)
    # Benchmark value per candidate revision id; None makes the benchmark fail.
    benchmark: Callable[[str], float | None] = lambda _revision: 100.0
    accuracy: Callable[[str], bool] = lambda _revision: True
    baseline_value: float | None = 50.0
    parent_verified: Callable[[str], bool] = lambda _revision: True


@dataclass
class FakeCore:
    """Core and executors answering one strategy's decisions."""

    strategy: DynamicStrategy
    script: Script
    view: RunView = field(default_factory=empty_view)
    duplicate: bool = False
    decisions: list[Decision] = field(default_factory=list)
    events: list[StrategyEvent] = field(default_factory=list)
    readings: dict[EvidenceId, EvidenceReading] = field(default_factory=dict)
    _count: int = 0
    _revisions: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def __post_init__(self) -> None:
        self.codec = dynamic_operation_registry()

    # -- driving ----------------------------------------------------------

    def step(self) -> tuple[Decision, ...]:
        """One `decide`, then answer every decision it proposed."""
        proposal = self.strategy.decide(self.view)
        self.strategy = self.strategy.bind(proposal.state)
        self.decisions.extend(proposal.decisions)
        for decision in proposal.decisions:
            self.feed(Accepted(decision_id=decision.decision_id))
            for event in self.execute(decision):
                self.feed(event)
        return proposal.decisions

    def feed(self, event: StrategyEvent) -> None:
        """Deliver one event to the strategy, twice when ``duplicate`` is set."""
        for _ in range(2 if self.duplicate else 1):
            self.events.append(event)
            self.strategy = self.strategy.bind(self.strategy.on_event(self.view, event))

    def run(self, limit: int = 400) -> None:
        """Step until the strategy proposes nothing or the run ended."""
        for _ in range(limit):
            if not self.step():
                return
            if any(isinstance(event, RunEnded) for event in self.events):
                return
        message = "strategy did not quiesce"
        raise AssertionError(message)

    # -- decisions --------------------------------------------------------

    def execute(self, decision: Decision) -> tuple[StrategyEvent, ...]:
        """Answer one decision as core and the executors would."""
        event: StrategyEvent
        if isinstance(decision, Operation):
            event = self._operation(decision)
        elif isinstance(decision, StartAttempt):
            event = self._start(decision)
        elif isinstance(decision, RequestTurn):
            event = self._turn(decision)
        elif isinstance(decision, Measure):
            event = self._measure(decision)
        elif isinstance(decision, Withdraw):
            event = self._settle(decision)
        elif isinstance(decision, ProposeWinner):
            event = AdoptionResult(
                selection=decision.selection, observation=self._observation(decision.scope)
            )
        elif isinstance(decision, Stop):
            event = RunEnded(result=decision.result)
        else:
            message = f"unhandled decision {decision!r}"
            raise TypeError(message)
        return (event,)

    def _observation(
        self, scope: Scope, status: ObservationStatus = ObservationStatus.SUCCEEDED
    ) -> Observation:
        self._count += 1
        return Observation(
            event_id=EventId(root=f"event:{self._count}"),
            request_id=RequestId(root=f"request:{self._count}"),
            scope=scope,
            sequence=self._count,
            observed_at=float(self._count),
            status=status,
            accepted=True,
            terminal=True,
            released=True,
        )

    def _operation(self, decision: Operation) -> OperationResult:
        request = decision.request
        if isinstance(request, VerifyParentRevision):
            outcome: object = ParentVerification(
                status="succeeded",
                verified=self.script.parent_verified(request.parent.revision_id.root),
            )
        elif isinstance(request, InterpretEvidence):
            outcome = EvidenceReadings(
                status="succeeded",
                readings=tuple(self.readings[item.evidence_id] for item in request.evidence),
            )
        else:
            outcome = RenderedArtifacts(
                status="succeeded",
                prompts=(
                    ArtifactRef(
                        artifact_id=ArtifactId(root=f"prompt:{decision.decision_id.root}"),
                        digest="prompt",
                    ),
                ),
            )
        descriptor = next(item for item in self.codec.descriptors if item.kind == request.kind)
        event = OperationResult(
            operation_id=OperationId(root=f"operation:{decision.decision_id.root}"),
            observation=self._observation(decision.scope),
            outcome_schema=descriptor.outcome_schema,
            operation_schema=self.codec.encode(request).schema_ref,
            outcome=outcome,  # type: ignore[arg-type]
        )
        return self.codec.validate_event(event)

    def _start(self, decision: StartAttempt) -> AttemptReady:
        live = AttemptView(
            attempt_id=decision.attempt_id,
            item_id=decision.item_id,
            generation=0,
            phase=AttemptPhase.ACTIVE,
            workspace=decision.workspace,
            budget=decision.budget,
        )
        self.view = self.view.model_copy(update={"attempts": (*self.view.attempts, live)})
        return AttemptReady(
            attempt=AttemptRef(attempt_id=decision.attempt_id, generation=0),
            admission_id=decision.decision_id,
        )

    def _retain(self, attempt_root: str, spec: TurnSpec) -> RevisionRef:
        self._revisions[attempt_root] += 1
        candidate = revision(f"rev:{attempt_root}:{self._revisions[attempt_root]}")
        attempts = tuple(
            item.model_copy(
                update={
                    "checkpoints": (
                        *item.checkpoints,
                        AttemptCheckpoint(
                            invocation=InvocationRef(
                                session_id=spec.session.session_id,
                                invocation_id=spec.invocation_id,
                                generation=0,
                            ),
                            request_id=RequestId(root=f"retain:{candidate.revision_id.root}"),
                            revision=candidate,
                            retention="candidate",
                        ),
                    )
                }
            )
            if item.attempt_id.root == attempt_root
            else item
            for item in self.view.attempts
        )
        self.view = self.view.model_copy(update={"attempts": attempts})
        return candidate

    def _turn(self, decision: RequestTurn) -> TurnResult:
        spec = decision.turn
        role = spec.session.role_id.root.removeprefix("dynamic-")
        queue = {
            "orchestrator": self.script.planner,
            "implementer": self.script.implementer,
            "judge": self.script.judge,
            "profiler": self.script.profiler,
        }[role]
        assert queue, f"no scripted {role} reply for {decision.decision_id.root}"
        reply = queue.popleft()
        if role == "implementer" and decision.scope.owner.kind == "attempt":
            body = json.loads(reply)
            if body.get("kind", "result") == "result" and body.get("outcome") not in {
                "blocked",
                "implementation_failed",
            }:
                self._retain(decision.scope.owner.root, spec)
        return TurnResult(
            invocation=InvocationRef(
                session_id=spec.session.session_id, invocation_id=spec.invocation_id, generation=0
            ),
            observation=self._observation(decision.scope),
            output_schema=spec.output_schema,
            output_json=reply,
        )

    def _measure(self, decision: Measure) -> MeasurementResult:
        plan = decision.plan
        candidate = plan.candidate
        assert isinstance(candidate, RevisionRef)
        root = candidate.revision_id.root
        evidence: list[EvidenceRef] = []
        source = RequestId(root=f"measurement:{decision.decision_id.root}")
        for stage in plan.stages:
            kind = {
                "accuracy": EvidenceKind.CORRECTNESS,
                "benchmark": EvidenceKind.BENCHMARK,
                "profile": EvidenceKind.PROFILING,
            }[stage.stage_id]
            identifier = EvidenceId(root=f"{stage.stage_id}:{plan.purpose}:{root}")
            ref = EvidenceRef(
                evidence_id=identifier,
                kind=kind,
                purpose=plan.purpose,
                scope=decision.scope,
                source_request=source,
                candidate=candidate,
                observation_sequence=len(self.view.measurements) + len(evidence) + 1,
                evaluator_digest=plan.evaluator_digest,
                workload_digest=plan.workload_digest,
                environment_digest=plan.environment_digest,
                provenance="trusted",
                status=ObservationStatus.SUCCEEDED,
            )
            evidence.append(ref)
            self.readings[identifier] = self._reading(
                identifier, kind, stage.stage_id, root, plan.purpose
            )
        self.view = self.view.model_copy(
            update={"measurements": (*self.view.measurements, *evidence)}
        )
        return MeasurementResult(
            scope=decision.scope,
            source_request=source,
            evidence=tuple(evidence),
            status=ObservationStatus.SUCCEEDED,
        )

    def _reading(
        self, identifier: EvidenceId, kind: EvidenceKind, stage: str, root: str, purpose: str
    ) -> EvidenceReading:
        if kind is EvidenceKind.CORRECTNESS:
            return EvidenceReading(
                evidence_id=identifier,
                kind=kind,
                passed=True if purpose == "baseline" else self.script.accuracy(root),
                stage=stage,
            )
        if kind is EvidenceKind.PROFILING:
            return EvidenceReading(evidence_id=identifier, kind=kind, passed=True, stage=stage)
        value = self.script.baseline_value if purpose == "baseline" else self.script.benchmark(root)
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

    def _settle(self, decision: Withdraw) -> AttemptSettled:
        disposition = decision.disposition
        assert disposition.kind == "settle"
        assert isinstance(decision.target, AttemptRef)
        settlement = Settlement(
            settlement_id=SettlementId(root=f"settlement:{decision.target.attempt_id.root}"),
            attempt=decision.target,
            candidate=disposition.candidate,
            assessments=disposition.assessments,
            eligible=disposition.eligible,
            retention=disposition.retention,
            outcome=disposition.outcome,
        )
        attempts = tuple(
            item.model_copy(update={"phase": AttemptPhase.TERMINAL})
            if item.attempt_id == decision.target.attempt_id
            else item
            for item in self.view.attempts
        )
        self.view = self.view.model_copy(
            update={"attempts": attempts, "settlements": (*self.view.settlements, settlement)}
        )
        return AttemptSettled(settlement=settlement)
