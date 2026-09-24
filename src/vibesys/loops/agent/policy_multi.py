"""Multi-agent policy: pre-plan profiling, implementation, and review."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vibesys.loops.agent.attempt import (
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    attempt_was_reviewed,
)
from vibesys.loops.agent.policy_attempts import (
    AttemptDecision,
    AttemptRequest,
    AttemptServices,
    AttemptState,
    PerformanceProjection,
)
from vibesys.loops.agent.policy_support import (
    _candidate_evidence_is_fresh,
    _implementation_keeps_hypothesis_active,
    _implementation_requests_continuation,
    _is_fresh_cold_start,
    _pareto_archive_conflict,
    _review_due,
)
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    ImplementerResponse,
    ProfilerSummary,
    Verdict,
)

if TYPE_CHECKING:
    from vibesys.loops.agent.policy_rounds import RoundPreparationRequest, RoundPreparationServices


class MultiAgentRoundPreparation:
    """Let the orchestrator request a separate profiler turn."""

    def __init__(self, services: RoundPreparationServices) -> None:
        """Bind the shared context and specialist role handles."""
        self.services = services

    def profiler_summary(self, request: RoundPreparationRequest) -> ProfilerSummary | None:
        """Run the pre-round decision and its optional profiler turn."""
        services = self.services
        decision = services.turns.pre_round_decision(
            request, has_history=not _is_fresh_cold_start(request.round_number, request.records)
        )
        if not decision.need_profile or not services.profiler_enabled:
            return None
        return services.turns.profile(
            request, decision.profile_focus or "general steady-state benchmark hotspots"
        )


class MultiAgentAttemptPolicy:
    """Implementer, optional reviewer, and local validation in that order."""

    def __init__(self, services: AttemptServices) -> None:
        """Bind the run's role handles and framework services."""
        self.services = services

    def _implement(self, request: AttemptRequest, state: AttemptState) -> bool:
        services = self.services
        attempt = services.turns.implement(request, state)
        state.implementation = attempt.response
        if not attempt.synthesized:
            return True
        state.judge = JudgeSkipped(JudgeSkipReason.UNPARSEABLE_IMPLEMENTATION)
        services.effects.log(
            f"[implementer] attempt {state.retry} returned no parseable structured response; "
            "the framework synthesized a fail-closed one. "
            + (
                "Retrying within the same round."
                if state.retry < services.max_retries_per_round
                else "Retries are exhausted; completing the round with it."
            )
        )
        return False

    def _needs_review(self, request: AttemptRequest, state: AttemptState) -> bool:
        services = self.services
        implementation = cast("ImplementerResponse", state.implementation)
        due = _review_due(
            round_number=request.round_number,
            max_rounds=services.max_rounds,
            judge_every=services.judge_every,
            outcome=implementation.hypothesis_outcome,
            candidate_evidence_fresh=_candidate_evidence_is_fresh(implementation, request.records),
        )
        if state.review_started and not _implementation_requests_continuation(implementation):
            due = True
        if (
            state.review_started
            and request.round_number != services.max_rounds
            and _implementation_requests_continuation(implementation)
            and implementation.candidate_disposition is not CandidateDisposition.PARETO_FRONTIER
            and not state.revalidation_required
        ):
            due = False
        if due:
            return True
        state.judge = JudgeSkipped(JudgeSkipReason.SPARSE_REVIEW_POLICY)
        services.effects.record_judge_skipped(request, implementation.hypothesis_outcome.value)
        services.effects.log(
            "[judge] deferred by sparse-review policy; official gates were not run"
        )
        return False

    def _judge(self, request: AttemptRequest, state: AttemptState) -> Verdict:
        services = self.services
        implementation = cast("ImplementerResponse", state.implementation)
        state.review_started = True
        state.revalidation_required = False
        conflict = _pareto_archive_conflict(
            candidate_disposition=implementation.candidate_disposition,
            candidate_metrics=dict(implementation.candidate_metrics),
            records=request.records,
            space=state.agent_run_state.metrics,
        )
        verdict = services.turns.judge(request, state, conflict)
        state.judge = JudgeReviewed(verdict.verdict)
        if verdict.verdict is not Verdict.PASS:
            state.feedback = verdict.feedback
            request.active_hypothesis.feedback = state.feedback
            services.checkpoint(request, state)
        return verdict.verdict

    def _approved_candidate(self, request: AttemptRequest, state: AttemptState) -> None:
        implementation = cast("ImplementerResponse", state.implementation)
        if implementation.candidate_disposition is not CandidateDisposition.PARETO_FRONTIER:
            return
        hypothesis = request.active_hypothesis
        hypothesis.gate_approved_candidate_disposition = implementation.candidate_disposition.value
        hypothesis.gate_approved_candidate_metrics = dict(implementation.candidate_metrics)
        hypothesis.gate_approved_candidate_evaluation_artifact = (
            implementation.candidate_evaluation_artifact
        )
        hypothesis.gate_approved_candidate_operating_point = (
            implementation.candidate_operating_point
        )
        hypothesis.gate_approved_candidate_retention_reason = (
            implementation.candidate_retention_reason
        )
        self.services.checkpoint(request, state)

    def _approved_perf(self, request: AttemptRequest, state: AttemptState) -> None:
        implementation = cast("ImplementerResponse", state.implementation)
        if implementation.perf_metric is None:
            return
        hypothesis = request.active_hypothesis
        hypothesis.gate_approved_perf_metric = implementation.perf_metric
        hypothesis.gate_approved_perf_unit = implementation.perf_unit
        hypothesis.gate_approved_metrics = dict(implementation.metrics)
        hypothesis.gate_approved_evaluation_artifact = implementation.evaluation_artifact
        self.services.checkpoint(request, state)

    def _passed_review(self, request: AttemptRequest, state: AttemptState) -> AttemptDecision:
        services = self.services
        implementation = cast("ImplementerResponse", state.implementation)
        validation_feedback = services.effects.validate(
            request, state, implementation.validation_recipe_artifact
        )
        if validation_feedback is not None:
            state.feedback = validation_feedback
            request.active_hypothesis.feedback = state.feedback
            services.checkpoint(request, state)
            return AttemptDecision.RETRY
        self._approved_candidate(request, state)
        candidate_ready = (
            implementation.hypothesis_outcome
            in {
                HypothesisOutcome.SUPPORTED,
                HypothesisOutcome.NOMINATED,
            }
            or implementation.candidate_disposition is CandidateDisposition.PARETO_FRONTIER
        )
        reason = services.official_reason(request, candidate_ready=candidate_ready)
        if reason is None:
            if candidate_ready:
                services.record_official_decision(
                    request, state, run=False, reason="cadence_not_due"
                )
                services.effects.log(
                    "[official-evaluation] deferred; candidate retained as a provisional working checkpoint"
                )
            state.passed = True
            return AttemptDecision.FINISH
        self._approved_perf(request, state)
        state.official_reason = reason
        return AttemptDecision.OFFICIAL

    def run_attempt(self, request: AttemptRequest, state: AttemptState) -> AttemptDecision:
        """Run implementer, cadence review, judge, then validation."""
        if not self._implement(request, state):
            return AttemptDecision.RETRY
        if not self._needs_review(request, state):
            return AttemptDecision.FINISH
        if self._judge(request, state) is Verdict.PASS:
            return self._passed_review(request, state)
        return AttemptDecision.RETRY

    def project_performance(
        self, request: AttemptRequest, state: AttemptState
    ) -> PerformanceProjection:
        """Use the framework metric or reviewed implementer evidence."""
        implementation = state.implementation
        official = state.passed and state.official_reason is not None
        implementation_metric = (
            implementation.perf_metric if implementation is not None and official else None
        )
        if (
            implementation_metric is None
            and official
            and implementation is not None
            and implementation.hypothesis_outcome
            in {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED}
            and request.active_hypothesis.gate_revalidation_pending
        ):
            implementation_metric = request.active_hypothesis.gate_approved_perf_metric
        profile_skipped = state.framework_perf_metric is None and implementation_metric is None
        if state.framework_perf_metric is not None and official:
            return PerformanceProjection(
                metric=state.framework_perf_metric,
                unit=state.framework_benchmark.metric_name,
                provenance="framework",
                profile_skipped=profile_skipped,
                accepted_metrics={},
                accepted_evaluation_artifact=None,
                next_single_response=None,
            )
        if implementation_metric is None:
            return PerformanceProjection(None, None, None, profile_skipped, {}, None, None)
        if implementation is not None and implementation.perf_metric is not None:
            return PerformanceProjection(
                implementation_metric,
                implementation.perf_unit,
                "implementer",
                profile_skipped,
                dict(implementation.metrics),
                implementation.evaluation_artifact,
                None,
            )
        hypothesis = request.active_hypothesis
        return PerformanceProjection(
            implementation_metric,
            hypothesis.gate_approved_perf_unit,
            "implementer",
            profile_skipped,
            dict(hypothesis.gate_approved_metrics),
            hypothesis.gate_approved_evaluation_artifact,
            None,
        )

    def reviewed(self, state: AttemptState) -> bool:
        """Only the final attempt's judge verdict counts as a review."""
        return attempt_was_reviewed(state.judge)

    def keeps_hypothesis_active(self, state: AttemptState, continuation_rounds: int) -> bool:
        """A reviewed or provisional implementation may retain its lease."""
        return _implementation_keeps_hypothesis_active(
            state.implementation, continuation_rounds=continuation_rounds
        )

    def terminal_success_needs_parent_choice(
        self, state: AttemptState, continuation_rounds: int
    ) -> bool:
        """Terminal reviewed edits need the next designer to choose a parent."""
        implementation = state.implementation
        return (
            implementation is not None
            and not self.keeps_hypothesis_active(state, continuation_rounds)
            and implementation.hypothesis_outcome is not HypothesisOutcome.NOMINATED
        )
