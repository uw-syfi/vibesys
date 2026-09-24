"""Single-agent policy: combined implementation, profiling, and review."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.loops.agent.attempt import JudgeReviewed
from vibesys.loops.agent.policy_attempts import (
    AttemptDecision,
    AttemptRequest,
    AttemptServices,
    AttemptState,
    PerformanceProjection,
)
from vibesys.loops.agent.policy_support import _profiler_summary_from_single_agent
from vibesys.schemas import ProfilerSummary, Verdict

if TYPE_CHECKING:
    from vibesys.loops.agent.policy_rounds import RoundPreparationRequest
    from vs_loop_state.api import PerfProvenance


class SingleAgentRoundPreparation:
    """Carry the previous combined agent turn into the next designer plan."""

    def profiler_summary(self, request: RoundPreparationRequest) -> ProfilerSummary | None:
        """Adapt the previous combined response, when one exists."""
        if request.previous_single_response is None:
            return None
        return _profiler_summary_from_single_agent(request.previous_single_response)


class SingleAgentAttemptPolicy:
    """One combined agent turn supplies implementation, profile, and review."""

    def __init__(self, services: AttemptServices) -> None:
        """Bind the shared run resources."""
        self.services = services

    def run_attempt(self, request: AttemptRequest, state: AttemptState) -> AttemptDecision:
        """Run one combined turn, then ask the executor for official gates if due."""
        services = self.services
        response = services.turns.combined(request, state)
        state.single_agent_response = response
        state.judge = JudgeReviewed(response.verdict)
        if response.verdict is not Verdict.PASS:
            state.feedback = response.feedback
            request.active_hypothesis.feedback = state.feedback
            services.checkpoint(request, state)
            return AttemptDecision.RETRY
        reason = services.official_reason(request, candidate_ready=True)
        if reason is None:
            services.record_official_decision(request, state, run=False, reason="cadence_not_due")
            services.effects.log(
                "[official-evaluation] deferred; candidate retained as a provisional working checkpoint"
            )
            state.passed = True
            return AttemptDecision.FINISH
        state.official_reason = reason
        return AttemptDecision.OFFICIAL

    def project_performance(
        self, _request: AttemptRequest, state: AttemptState
    ) -> PerformanceProjection:
        """Use this round's combined response, not the prior plan's profile."""
        response = state.single_agent_response
        official = state.passed and state.official_reason is not None
        provenance: PerfProvenance | None = None
        if response is not None and state.framework_perf_metric is not None and official:
            response.perf_metric = state.framework_perf_metric
            response.perf_unit = state.framework_benchmark.metric_name
            provenance = "framework"
        metric = response.perf_metric if response is not None and official else None
        unit = response.perf_unit if response is not None and official else None
        if metric is not None and provenance is None:
            provenance = "implementer"
        return PerformanceProjection(
            metric=metric,
            unit=unit,
            provenance=provenance,
            profile_skipped=response is None or response.perf_metric is None,
            accepted_metrics={},
            accepted_evaluation_artifact=None,
            next_single_response=response,
        )

    def reviewed(self, _state: AttemptState) -> bool:
        """The combined agent response always contains its own verdict."""
        return True

    def keeps_hypothesis_active(self, _state: AttemptState, _continuation_rounds: int) -> bool:
        """Combined turns do not retain the multi-agent implementer lease."""
        return False

    def terminal_success_needs_parent_choice(
        self, _state: AttemptState, _continuation_rounds: int
    ) -> bool:
        """Combined turns have no separate terminal implementer handoff."""
        return False
