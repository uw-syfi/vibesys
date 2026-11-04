"""One round's attempt at its hypothesis, as pure bookkeeping.

An attempt is the implementer's work on one hypothesis in one candidate workspace: up to
`max_retries_per_round` paid turns, each ending in a reply that the guards judge, a candidate
that the trusted gates measure, or a failure. `SingleAttempt` holds what the turns established; its
methods decide what each reply, gate result and failure means and say whether to ask again.
Nothing here awaits or reads the run, so the rules are property-tested on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from vibesys.hypothesis import AgentAttribution, CandidateDisposition, HypothesisOutcome, rounds
from vibesys.metrics import MetricRow, reading_of
from vibesys.orchestration.review import Verdict
from vibesys.orchestration.single.search._prompts import RoundPrompt
from vibesys.serial_search import api as serial
from vs_core.api.search import Assessment, EvidenceKind, RevisionRef, Settlement

if TYPE_CHECKING:
    from vibesys.candidates.api import Baseline
    from vibesys.hypothesis import OrchestratorPlan
    from vibesys.metrics import AcceptedReading
    from vibesys.orchestration.single.models import SingleAgentRoundResponse
    from vibesys.orchestration.single.search._config import SingleConfig
    from vibesys.serial_search.api import GateResult
    from vs_core.api.search import TaskTerms

UNSETTLED: frozenset[HypothesisOutcome] = frozenset()
"""Single's implementer states no outcome of its own: no outcome makes a candidate ineligible."""


class Next(StrEnum):
    """What a reply or a gate result leaves to do."""

    AGAIN = "again"
    """The attempt did not pass: ask the implementer again if a paid turn is left."""
    GATES = "gates"
    """The self-review passed and the cadence asks for the trusted gates."""
    SETTLE = "settle"
    """The attempt is over; the workspace closes with what it established."""


@dataclass
class SingleAttempt:
    """What the turns of one round have established so far."""

    config: SingleConfig
    terms: TaskTerms
    baseline: Baseline
    number: int
    plan: OrchestratorPlan
    base: RevisionRef
    ledger: rounds.Ledger
    feedback: str | None = None
    turns: int = 0
    candidate: RevisionRef | None = None
    response: SingleAgentRoundResponse | None = None
    verdict_pass: bool | None = None
    no_valid_reply: str | None = None
    dominated_by: tuple[int, ...] = ()
    official_reason: str | None = None
    readings: tuple[AcceptedReading, ...] = ()
    measurement_noise: serial.MeasurementNoise | None = None
    """The noise the latest trusted comparison stated, which the next designer is shown."""
    passed: bool = False
    gate_failed: bool = False
    unmeasured: bool = False
    failure: str | None = None

    # -- what the implementer is asked -------------------------------------------------------

    def prompt(self) -> RoundPrompt:
        """The implementation, self-review and profile request of the next turn."""
        plan = self.plan
        return RoundPrompt(
            round_number=self.number,
            retry=self.turns + 1,
            hypothesis_id=plan.hypothesis_id,
            title=plan.title,
            hypothesis=plan.hypothesis,
            task=plan.task,
            pass_criteria=plan.pass_criteria,
            invariants=plan.invariants,
            activation_evidence=plan.activation_evidence,
            falsification_criteria=plan.falsification_criteria,
            minimum_ratio=plan.minimum_ratio,
            base_revision=self.base,
            feedback=self.feedback,
            no_valid_reply=self.no_valid_reply,
            dominated_by_rounds=self.dominated_by,
        )

    # -- what a turn came to -----------------------------------------------------------------

    def no_reply(self, reason: str) -> Next:
        """The turn produced no valid reply: it is asked again while paid turns remain."""
        self.response = None
        self.no_valid_reply = reason
        self.verdict_pass = None
        self.feedback = None
        return Next.AGAIN

    def failed(self, reason: str) -> Next:
        """The attempt cannot go on; the workspace closes with this failure."""
        self.failure = self.failure or reason
        return Next.SETTLE

    def replied(self, reply: SingleAgentRoundResponse, revision: RevisionRef | None) -> Next:
        """Judge a valid reply: the Pareto guard, the candidate, then whether the gates are due."""
        hypotheses = self.config.hypotheses
        self.readings = ()  # a reading is of the candidate it measured, never of a later one
        conflict = rounds.pareto_conflict(hypotheses, self.ledger, reply)
        passing = reply.verdict is Verdict.PASS and conflict is None
        guarded = reply
        dominated: tuple[int, ...] = ()
        if conflict is not None and reply.verdict is Verdict.PASS:
            guarded = reply.model_copy(update={"verdict": Verdict.FAIL})
            dominated = tuple(item.round_number for item in conflict.dominators)
        self.response = guarded
        self.candidate = revision or self.candidate
        self.verdict_pass = passing
        self.no_valid_reply = None
        self.dominated_by = dominated
        if not passing:
            feedback = reply.feedback or "self-review failed"
            self.ledger = rounds.note_attempt_failure(hypotheses, self.ledger, feedback)
            self.feedback = feedback
            return Next.AGAIN
        if self.candidate is None or self.candidate == self.base:
            self.no_valid_reply = "the implementer retained no changed candidate"
            self.feedback = "retained no changed candidate"
            return Next.AGAIN
        reason = rounds.official_reason()
        if reason is None or not self.terms.measured:
            self.passed = True
            self.official_reason = None
            return Next.SETTLE
        self.official_reason = reason
        return Next.GATES

    def gated(self, result: GateResult) -> Next:
        """Fold the trusted gates: a rejection is feedback, a missing measurement is nobody's fault."""
        match result:
            case serial.Gated(readings=readings, noise=noise):
                self.readings = readings
                self.measurement_noise = noise or self.measurement_noise
                feedback = serial.failed_gate(readings)
                if feedback is None:
                    self.passed = True
                    return Next.SETTLE
                self._gate_failed(feedback)
                return Next.AGAIN
            case serial.NotMeasured(feedback=feedback):
                # The machinery could not measure it: the candidate is not charged another
                # attempt and the agent is not asked to repair the infrastructure.
                self.unmeasured = True
                self.feedback = feedback
                return Next.SETTLE
            case serial.Unreadable(reason=reason):
                return self.failed(reason)

    def _gate_failed(self, feedback: str) -> None:
        """A trusted gate rejected the candidate: another attempt may repair it."""
        commit = None if self.candidate is None else self.candidate.revision_id.root
        gate = rounds.GateFailure(commit, accuracy_passed=serial.accuracy_passed(self.readings))
        self.ledger = rounds.note_attempt_failure(
            self.config.hypotheses, self.ledger, feedback, gate
        )
        self.gate_failed = True
        self.passed = False
        self.feedback = feedback

    # -- how the attempt ends ----------------------------------------------------------------

    def finished(self) -> serial.FinishedRound:
        """The attempt as the candidate policy reads a finished one."""
        return serial.FinishedRound(
            readings=self.readings,
            candidate=self.candidate,
            review_passed=self.verdict_pass,
            failure=self.failure,
            passed=self.passed,
            disposition=CandidateDisposition.UNASSESSED
            if self.response is None
            else self.response.candidate_disposition,
        )

    def turned_down(self) -> bool:
        """Whether the round's own checks turned its work down (`serial_search.turned_down`)."""
        return serial.turned_down(self.finished(), unsettled=UNSETTLED)

    def reported(self) -> tuple[str | None, str] | None:
        """The implementer's own account: no outcome (single states none) and its summary."""
        reply = self.response
        return None if reply is None else (None, reply.summary)

    def asserted(self) -> tuple[RevisionRef | None, Assessment]:
        """What the workspace settles with."""
        return serial.settle(
            self.finished(), baseline=self.baseline, terms=self.terms, unsettled=UNSETTLED
        )

    def close(self, settlement: Settlement | None) -> rounds.Ledger:
        """Close the round in the ledger with what core settled (None: nothing was)."""
        retained = None if settlement is None else settlement.candidate
        benchmark, metric = serial.benchmark_outcome(self.readings)
        response = self.response
        passed = self.passed and self.failure is None
        official = passed and self.official_reason is not None
        if response is not None and official and metric is not None:
            response = response.model_copy(
                update={"perf_metric": metric, "perf_unit": benchmark.metric_name}
            )
        closed = rounds.close_round(
            self.config.hypotheses,
            self.ledger,
            rounds.ClosingFacts(
                round_number=self.number,
                plan=self.plan,
                reply=response,
                passed=passed,
                feedback=None if passed else (self.feedback or self.failure),
                official_reason=self.official_reason if passed else None,
                benchmark=benchmark,
                benchmark_metric=metric,
                candidate_commit=None if retained is None else retained.revision_id.root,
                attempts=self.turns,
                accuracy_configured="accuracy" in self.terms.measured,
                benchmark_configured="benchmark" in self.terms.measured,
                implementer=AgentAttribution("core", None, None),
                revalidation_required=self.gate_failed,
                verdict_pass=self.verdict_pass,
            ),
        )
        return serial.record_round(
            closed, serial.focus_policy(self.config.attribution), passed=passed
        )

    def benchmark_metrics(self) -> tuple[MetricRow, ...]:
        """The benchmark rows the trusted gates read, which rank the round among the kept."""
        reading = reading_of(self.readings, EvidenceKind.BENCHMARK)
        return () if reading is None else reading.metrics

    def last_profile(self) -> serial.LastProfile | None:
        """The profile fields the implementer reported, for the next designer prompt."""
        response = self.response
        if response is None:
            return None
        return serial.LastProfile(
            analysis=response.profile_analysis,
            bottlenecks=response.bottlenecks,
            suggestions=response.suggestions,
            perf_metric=response.perf_metric,
            perf_unit=response.perf_unit,
        )
