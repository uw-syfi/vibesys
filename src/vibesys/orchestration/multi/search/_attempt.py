"""One round's attempt at its hypothesis, as pure bookkeeping.

An attempt is the implementer's work on one hypothesis in one candidate workspace: up to
`max_retries_per_round` paid turns, each ending in a reply that the review (when the cadence
asks for one), the validation of its recipes and the trusted gates judge, or in a failure.
`MultiAttempt` holds what the turns established; its methods decide what each reply, verdict,
validation and gate result means and say what to do next. Nothing here awaits or reads the run,
so the rules are property-tested on their own.

Multi differs from dynamic in what eligibility requires: a candidate is adopted only when an
independent review passed it (`asserted`). Dynamic treats a round no review reached as a pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from vibesys.hypothesis import (
    AgentAttribution,
    ArchiveConflict,
    CandidateDisposition,
    Hypothesis,
    HypothesisOutcome,
    JudgeOutcome,
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    OrchestratorPlan,
    reviewed,
    rounds,
)
from vibesys.metrics import MetricRow, reading_of
from vibesys.orchestration.multi.search._prompts import (
    ArchiveConflictView,
    Claim,
    ConflictMetric,
    Dominator,
    GateFacts,
    HypothesisPlan,
    ImplementerClaims,
    ImplementPrompt,
    JudgePrompt,
    ValidationFailure,
)
from vibesys.orchestration.review import Verdict
from vibesys.serial_search import api as serial
from vs_core.api.search import (
    Assessment,
    EvidenceKind,
    EvidenceRef,
    InvocationRef,
    LocalValidationReport,
    RevisionRef,
    Settlement,
    effect_verdict,
    terms_policy,
)

if TYPE_CHECKING:
    from vibesys.candidates.api import Baseline
    from vibesys.metrics import AcceptedReading
    from vibesys.orchestration.multi.models import ImplementerResponse, JudgeResponse
    from vibesys.orchestration.multi.search._config import MultiConfig
    from vibesys.serial_search.api import GateResult
    from vs_core.api.search import EffectVerdict, TaskTerms

UNSETTLED = frozenset({HypothesisOutcome.BLOCKED, HypothesisOutcome.IMPLEMENTATION_FAILED})
"""Implementer outcomes that make a candidate ineligible however well it measures."""

DOMINATED = "the live Pareto archive dominates the claimed frontier row"


class Next(StrEnum):
    """What a reply, a verdict, a validation or a gate result leaves to do."""

    AGAIN = "again"
    """The attempt did not pass: ask the implementer again if a paid turn is left."""
    REVIEW = "review"
    """The reply is acceptable and a review is due."""
    VALIDATE = "validate"
    """The review passed and the implementer wrote validation recipes to run."""
    GATES = "gates"
    """The claims are approved and the cadence asks for the trusted gates."""
    SETTLE = "settle"
    """The attempt is over; the workspace closes with what it established."""


def conflict_view(conflict: ArchiveConflict) -> ArchiveConflictView:
    """The archive conflict as prompt data."""
    return ArchiveConflictView(
        dominators=tuple(
            Dominator(
                round_number=item.round_number,
                metrics=tuple(
                    ConflictMetric(name=m.name, value=m.value, direction=m.direction)
                    for m in item.metrics
                ),
            )
            for item in conflict.dominators
        )
    )


def hypothesis_plan(plan: OrchestratorPlan) -> HypothesisPlan:
    """The plan fields every round prompt shows."""
    return HypothesisPlan(
        hypothesis_id=plan.hypothesis_id,
        title=plan.title,
        hypothesis=plan.hypothesis,
        task=plan.task,
        pass_criteria=plan.pass_criteria,
        invariants=plan.invariants,
        activation_evidence=plan.activation_evidence,
        falsification_criteria=plan.falsification_criteria,
        minimum_ratio=plan.minimum_ratio,
    )


def gate_facts(hypothesis: Hypothesis | None) -> GateFacts:
    """What the framework already established for the active hypothesis."""
    if hypothesis is None:
        return GateFacts()
    return GateFacts(
        revert_applied=hypothesis.revert_applied,
        revert_round=hypothesis.parent_round,
        revert_commit=hypothesis.revert_commit,
        revalidation_pending=hypothesis.gate_revalidation_pending,
        approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
        approved_perf_metric=hypothesis.gate_approved_perf_metric,
        approved_perf_unit=hypothesis.gate_approved_perf_unit,
    )


def _claims(values: dict[str, float]) -> tuple[Claim, ...]:
    return tuple(Claim(name=name, value=value) for name, value in values.items())


def claims_of(reply: ImplementerResponse) -> ImplementerClaims:
    """The implementer's reply as untrusted data for the judge."""
    return ImplementerClaims(
        summary=reply.summary,
        expected_behavior=reply.expected_behavior,
        status=reply.status.value,
        evidence=reply.evidence,
        next_step=reply.next_step,
        perf_metric=reply.perf_metric,
        perf_unit=reply.perf_unit,
        metrics=_claims(reply.metrics),
        evaluation_artifact=reply.evaluation_artifact,
        candidate_disposition=reply.candidate_disposition.value,
        candidate_metrics=_claims(reply.candidate_metrics),
        candidate_evaluation_artifact=reply.candidate_evaluation_artifact,
        candidate_operating_point=reply.candidate_operating_point,
        candidate_retention_reason=reply.candidate_retention_reason,
        validation_recipe_artifact=reply.validation_recipe_artifact,
    )


@dataclass(frozen=True)
class Evaluated:
    """What the trusted gates read of a revision the agent evaluated with no ready candidate.

    The measurement is evidence for the next designer; it adopts nothing, because the
    implementer offered no candidate for review (it was blocked, failed, or asked to continue).
    """

    revision: RevisionRef
    readings: tuple[AcceptedReading, ...]
    effect: EffectVerdict | None
    """What the framework's comparison says of the plan's typed bar; None without a comparison."""


@dataclass
class MultiAttempt:
    """What the turns of one round have established so far."""

    config: MultiConfig
    terms: TaskTerms
    baseline: Baseline
    number: int
    plan: OrchestratorPlan
    base: RevisionRef
    ledger: rounds.Ledger
    validation_offered: bool
    """Whether the run offers the trusted run of validation recipes."""
    """Whether the trusted gates will measure the candidate once it passes review."""
    revalidation_required: bool
    feedback: str | None = None
    turns: int = 0
    candidate: RevisionRef | None = None
    response: ImplementerResponse | None = None
    no_valid_reply: str | None = None
    conflict: ArchiveConflictView | None = None
    validation_failure: ValidationFailure | None = None
    review_started: bool = False
    review_skipped: bool = False
    judge_pass: bool | None = None
    judge_invocation: InvocationRef | None = None
    official_reason: str | None = None
    readings: tuple[AcceptedReading, ...] = ()
    effect: EffectVerdict | None = None
    """What the framework's comparison says of the plan's typed bar; None without a comparison."""
    measurement_noise: serial.MeasurementNoise | None = None
    """The noise the latest trusted comparison stated, which the next designer is shown."""
    evaluated: Evaluated | None = None
    """The measurement of the revision the agent evaluated when the attempt ended unsettled."""
    passed: bool = False
    gate_failed: bool = False
    unmeasured: bool = False
    failure: str | None = None

    # -- what the agents are asked -----------------------------------------------------------

    def prompt(self) -> ImplementPrompt:
        """The implementation request of the next paid turn: the first brief, or a continuation."""
        hypothesis = self.ledger.search.active_hypothesis
        focus = serial.focus_of(self.ledger, serial.focus_policy(self.config.attribution))
        return ImplementPrompt(
            round_number=self.number,
            retry=self.turns + 1,
            plan=hypothesis_plan(self.plan),
            base_revision=self.base,
            framework=gate_facts(hypothesis),
            continuation_step=None if hypothesis is None else (hypothesis.next_step or None),
            feedback=self.feedback,
            validation_failure=self.validation_failure,
            no_valid_reply=self.no_valid_reply,
            pareto_archive_conflict=self.conflict,
            benchmark_enabled="benchmark" in self.terms.measured,
            active_component=None if focus is None else (focus.active_component or None),
        )

    def judge_prompt(self, cited: tuple[EvidenceRef, ...]) -> JudgePrompt:
        """The independent review request for the implementer's latest reply."""
        if self.response is None:
            message = "a review needs the implementer's reply"
            raise RuntimeError(message)
        return JudgePrompt(
            round_number=self.number,
            retry=self.turns,
            plan=hypothesis_plan(self.plan),
            claims=claims_of(self.response),
            framework=gate_facts(self.ledger.search.active_hypothesis),
            pareto_archive_conflict=self.conflict,
            benchmark_enabled="benchmark" in self.terms.measured,
            cited_evidence=cited,
        )

    # -- what the implementer's turn came to -------------------------------------------------

    def _fresh_attempt(self) -> None:
        """What a new paid turn starts without: the last turn's verdict, validation and gates."""
        self.readings = ()
        self.effect = None
        self.judge_pass = None
        self.judge_invocation = None
        self.review_skipped = False
        self.official_reason = None

    def no_reply(self, reason: str) -> Next:
        """The turn produced no valid reply: it is asked again while paid turns remain."""
        self._fresh_attempt()
        self.response = None
        self.no_valid_reply = reason
        self.feedback = None
        self.validation_failure = None
        self.conflict = None
        return Next.AGAIN

    def failed(self, reason: str) -> Next:
        """The attempt cannot go on; the workspace closes with this failure."""
        self.failure = self.failure or reason
        return Next.SETTLE

    def replied(self, reply: ImplementerResponse, revision: RevisionRef | None) -> Next:
        """Accept a valid reply: review it when due, else end the attempt."""
        self._fresh_attempt()
        self.response = reply
        self.candidate = revision or self.candidate
        self.no_valid_reply = None
        self.feedback = None
        self.conflict = None
        self.validation_failure = None
        hypotheses = self.config.hypotheses
        if self.candidate is None and reviewed.candidate_ready(reply):
            self.ledger = rounds.note_attempt_failure(
                hypotheses, self.ledger, "retained no changed candidate"
            )
            self.no_valid_reply = "the implementer retained no changed candidate"
            return Next.AGAIN
        due = reviewed.review_due(
            hypotheses,
            self.ledger,
            reply,
            round_number=self.number,
            review_started=self.review_started,
            revalidation_required=self.revalidation_required,
        )
        if not due:
            self.review_skipped = True
            return Next.SETTLE
        conflict = rounds.pareto_conflict(hypotheses, self.ledger, reply)
        self.conflict = None if conflict is None else conflict_view(conflict)
        self.review_started = True
        self.revalidation_required = False
        return Next.REVIEW

    # -- what the review, the validation and the gates came to -------------------------------

    def judged(self, verdict: JudgeResponse, invocation: InvocationRef) -> Next:
        """Apply the Pareto guard to the verdict, then repair, validate or measure."""
        conflict = self.conflict
        passing = verdict.verdict is Verdict.PASS and conflict is None
        self.judge_pass = passing
        self.judge_invocation = invocation
        if passing:
            return self._after_review()
        feedback = verdict.feedback or (DOMINATED if conflict is not None else "review failed")
        self.ledger = rounds.note_attempt_failure(self.config.hypotheses, self.ledger, feedback)
        guarded = conflict if verdict.verdict is Verdict.PASS else None
        self.feedback = verdict.feedback or (None if guarded is not None else feedback)
        self.conflict = guarded
        return Next.AGAIN

    def review_lost(self) -> Next:
        """No verdict exists to act on and the candidate is intact: the round ends unreviewed.

        The candidate is kept as work in progress instead of paying for another implementation.
        """
        self.judge_pass = None
        self.judge_invocation = None
        return Next.SETTLE

    def _after_review(self) -> Next:
        """A passing review: validate the implementer's recipes when it wrote any, else approve."""
        recipe = self.recipe()
        if recipe is None or self.candidate is None or not self.validation_offered:
            return self._approve()
        return Next.VALIDATE

    def recipe(self) -> str | None:
        """The validation recipe the implementer reported, if any."""
        return None if self.response is None else self.response.validation_recipe_artifact

    def validated(self, report: LocalValidationReport | None) -> Next:
        """Fold the recipes' trusted run: a failure is feedback for another attempt."""
        if report is None or report.status != "succeeded":
            # The run could not be validated (nothing to blame on the candidate): approve it on
            # the judge's verdict, as a run that does not offer validation does.
            return self._approve()
        if report.passed:
            return self._approve()
        self.validation_failure = ValidationFailure(
            recipe=report.recipe,
            error=report.error,
            output=report.output,
            recipe_unusable=report.recipe_unusable,
        )
        label = (
            "validation recipe unusable" if report.recipe_unusable else "local validation failed"
        )
        self.ledger = rounds.note_attempt_failure(self.config.hypotheses, self.ledger, label)
        self.feedback = None
        self.conflict = None
        return Next.AGAIN

    def _approve(self) -> Next:
        """Approve the reply's claims, then settle or have the trusted gates measure the candidate."""
        reply = self.response
        if reply is None:
            return Next.SETTLE
        hypotheses = self.config.hypotheses
        ledger = reviewed.approve_candidate(hypotheses, self.ledger, reply)
        reason = rounds.official_reason(candidate_ready=reviewed.candidate_ready(reply))
        if reason is None or not self.terms.measured or self.candidate is None:
            self.ledger = ledger
            self.passed = True
            self.official_reason = None
            return Next.SETTLE
        self.ledger = reviewed.approve_performance(hypotheses, ledger, reply)
        self.official_reason = reason
        return Next.GATES

    def gated(self, result: GateResult) -> Next:
        """Fold the trusted gates: a rejection is feedback, a missing measurement is nobody's fault."""
        match result:
            case serial.Gated(readings=readings, comparison=comparison, noise=noise):
                self.readings = readings
                self.measurement_noise = noise or self.measurement_noise
                self.effect = (
                    None
                    if comparison is None
                    else effect_verdict(
                        comparison, self.plan.minimum_ratio, terms_policy(self.terms)
                    )
                )
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

    def owes_evaluated_measurement(self) -> bool:
        """Whether the attempt ended with no ready candidate and its trusted evaluations unmeasured.

        An implementer that was blocked, could not implement the idea, asked to continue, or
        never replied may still have evaluated a revision through the trusted evaluation tool.
        Nothing else measures it (a candidate the gates measured has its readings), and a failure
        of the machinery is not the idea's, so neither is a measurement that already failed.
        """
        offered = self.response is not None and reviewed.candidate_ready(self.response)
        return (
            not offered
            and bool(self.terms.measured)
            and not self.readings
            and self.evaluated is None
            and not self.unmeasured
            and self.failure is None
        )

    def evaluated_gated(self, revision: RevisionRef, result: GateResult) -> None:
        """Fold the gates' measurement of the revision the agent evaluated.

        A reading is recorded when no gate rejected it, as it is for a round that passed; a
        rejection is feedback for the designer. A measurement that could not be taken leaves the
        round as it was: nobody owes the idea a second attempt at the machinery.
        """
        if not isinstance(result, serial.Gated):
            return
        feedback = serial.failed_gate(result.readings)
        if feedback is not None:
            self.feedback = self.feedback or feedback
            return
        self.measurement_noise = result.noise or self.measurement_noise
        comparison = result.comparison
        self.evaluated = Evaluated(
            revision=revision,
            readings=result.readings,
            effect=None
            if comparison is None
            else effect_verdict(comparison, self.plan.minimum_ratio, terms_policy(self.terms)),
        )

    def _gate_failed(self, feedback: str) -> None:
        """A trusted gate rejected the candidate: another attempt may repair it."""
        commit = None if self.candidate is None else self.candidate.revision_id.root
        gate = rounds.GateFailure(commit, accuracy_passed=serial.accuracy_passed(self.readings))
        self.ledger = rounds.note_attempt_failure(
            self.config.hypotheses, self.ledger, feedback, gate
        )
        self.gate_failed = True
        self.passed = False
        self.revalidation_required = True
        self.feedback = feedback
        self.conflict = None
        self.validation_failure = None

    # -- how the attempt ends ----------------------------------------------------------------

    def finished(self) -> serial.FinishedRound:
        """The attempt as the candidate policy reads a finished one."""
        return serial.FinishedRound(
            readings=self.readings,
            candidate=self.candidate,
            review_passed=self.judge_pass,
            failure=self.failure,
            passed=self.passed,
            judge_invocation=self.judge_invocation,
            outcome=None if self.response is None else self.response.hypothesis_outcome,
            disposition=CandidateDisposition.UNASSESSED
            if self.response is None
            else self.response.candidate_disposition,
        )

    def turned_down(self) -> bool:
        """Whether the round's own checks turned its work down (`serial_search.turned_down`)."""
        return serial.turned_down(self.finished(), unsettled=UNSETTLED)

    def reported(self) -> tuple[str | None, str] | None:
        """The implementer's own account: its outcome and its summary; None when it gave none."""
        reply = self.response
        return None if reply is None else (reply.hypothesis_outcome.value, reply.summary)

    def asserted(self) -> tuple[RevisionRef | None, Assessment]:
        """What the workspace settles with.

        Multi adopts only what an independent review passed: a round whose review was deferred
        by the cadence, or lost, or never reached, produces no verdict and so can never be the
        winner. (Dynamic reads a missing review as a pass; this is the one place the two differ.)
        """
        revision, assessment = serial.settle(
            self.finished(), baseline=self.baseline, terms=self.terms, unsettled=UNSETTLED
        )
        if assessment.eligible and self.judge_pass is not True:
            assessment = assessment.model_copy(update={"eligible": False})
        return revision, assessment

    def judge_outcome(self) -> JudgeOutcome:
        """The round's review as the ledger records it."""
        if self.judge_pass is not None:
            return JudgeReviewed("pass" if self.judge_pass else "fail")
        if self.response is None:
            return JudgeSkipped(JudgeSkipReason.UNPARSEABLE_IMPLEMENTATION)
        if self.review_skipped:
            return JudgeSkipped(JudgeSkipReason.SPARSE_REVIEW_POLICY)
        return JudgeSkipped(JudgeSkipReason.NOT_REACHED)

    def close(self, settlement: Settlement | None) -> rounds.Ledger:
        """Close the round in the ledger with what core settled (None: nothing was)."""
        retained = None if settlement is None else settlement.candidate
        evaluated = self.evaluated
        benchmark, metric = serial.benchmark_outcome(
            self.readings if evaluated is None else evaluated.readings
        )
        passed = self.passed and self.failure is None
        # A measurement and the revision it measured are recorded together.
        measured = retained if evaluated is None else evaluated.revision
        commit = None if measured is None else measured.revision_id.root
        closed = reviewed.close_reviewed_round(
            self.config.hypotheses,
            self.ledger,
            reviewed.ReviewedClosing(
                round_number=self.number,
                plan=self.plan,
                reply=self.response,
                judge=self.judge_outcome(),
                passed=passed,
                feedback=None if passed else (self.feedback or self.no_valid_reply or self.failure),
                official_reason=(
                    rounds.evaluated_reason()
                    if evaluated is not None
                    else self.official_reason
                    if passed
                    else None
                ),
                benchmark=benchmark,
                benchmark_metric=metric,
                candidate_commit=commit,
                attempts=self.turns,
                accuracy_configured="accuracy" in self.terms.measured,
                benchmark_configured="benchmark" in self.terms.measured,
                implementer=AgentAttribution("core", None, None),
                revalidation_required=self.revalidation_required,
                evaluated_only=evaluated is not None,
                effect=self.effect if evaluated is None else evaluated.effect,
            ),
        )
        return serial.record_round(
            closed, serial.focus_policy(self.config.attribution), passed=passed
        )

    def benchmark_metrics(self) -> tuple[MetricRow, ...]:
        """The benchmark rows the trusted gates read, which rank the round among the kept."""
        reading = reading_of(self.readings, EvidenceKind.BENCHMARK)
        return () if reading is None else reading.metrics
