"""Internal control-flow interface for built-in agent orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vibesys.loops.agent.policy_multi import MultiAgentAttemptPolicy, MultiAgentRoundPreparation
from vibesys.loops.agent.policy_profile import (
    PlainProfilePolicy,
    ProfileGuidedPolicy,
    ProfileOutcomeInput,
    ProfilePolicy,
    ProfilePreparation,
)
from vibesys.loops.agent.policy_rounds import (
    RoundPreparation,
    RoundPreparationRequest,
    RoundPreparationServices,
    validate_inner_policy,
)
from vibesys.loops.agent.policy_single import SingleAgentAttemptPolicy, SingleAgentRoundPreparation

if TYPE_CHECKING:
    from vibesys.evaluators.input_manifest import ProfileGuidedInput
    from vibesys.loops.agent.hypothesis_controller import (
        HypothesisEngine,
        ProfileGuidanceOutcome,
    )
    from vibesys.loops.agent.model import AgentRunState
    from vibesys.loops.agent.policy_attempts import (
        AttemptDecision,
        AttemptPolicy,
        AttemptRequest,
        AttemptServices,
        AttemptState,
        PerformanceProjection,
    )
    from vibesys.schemas import ProfilerSummary


class BuiltInAgentControlFlow(Protocol):
    """Internal strategy boundary called by the shared agent round executor."""

    @property
    def profile_config(self) -> ProfileGuidedInput | None:
        """Return hypothesis-controller settings, if any."""
        ...

    def prepare_profile(
        self, request: ProfilePreparation
    ) -> tuple[HypothesisEngine, AgentRunState]:
        """Prepare outer profile guidance before a new designer plan."""
        ...

    def profiler_summary(self, request: RoundPreparationRequest) -> ProfilerSummary | None:
        """Provide fresh or carried profile evidence to the designer."""
        ...

    def official_reason(self, reason: str | None, engine: HypothesisEngine) -> str | None:
        """Adjust the planned official-evaluation reason."""
        ...

    def run_attempt(self, request: AttemptRequest, state: AttemptState) -> AttemptDecision:
        """Run the policy's agent turns for one durable attempt."""
        ...

    def project_performance(
        self, request: AttemptRequest, state: AttemptState
    ) -> PerformanceProjection:
        """Project headline evidence from the final attempt."""
        ...

    def reviewed(self, state: AttemptState) -> bool:
        """Report whether the final attempt received a review."""
        ...

    def keeps_hypothesis_active(self, state: AttemptState, continuation_rounds: int) -> bool:
        """Report whether this policy retains an implementer lease."""
        ...

    def terminal_success_needs_parent_choice(
        self, state: AttemptState, continuation_rounds: int
    ) -> bool:
        """Report whether terminal edits need a parent-state decision."""
        ...

    def profile_outcome(self, request: ProfileOutcomeInput) -> ProfileGuidanceOutcome | None:
        """Return outer profile-controller feedback for the completed round."""
        ...


class _InnerAgentFlow:
    """Shared delegation for the two built-in inner strategies."""

    def __init__(self, preparation: RoundPreparation, attempts: AttemptPolicy) -> None:
        self.preparation = preparation
        self.attempts = attempts
        self.profile = PlainProfilePolicy()

    @property
    def profile_config(self) -> None:
        """Plain inner policies have no outer profile controller."""
        return self.profile.config

    def prepare_profile(
        self, request: ProfilePreparation
    ) -> tuple[HypothesisEngine, AgentRunState]:
        """Use plain outer behavior unless a wrapper is composed."""
        return self.profile.prepare(request)

    def profiler_summary(self, request: RoundPreparationRequest) -> ProfilerSummary | None:
        """Call the inner policy's pre-plan turns."""
        return self.preparation.profiler_summary(request)

    def official_reason(self, reason: str | None, engine: HypothesisEngine) -> str | None:
        """Use ordinary cadence unless wrapped by profile guidance."""
        return self.profile.official_reason(reason, engine)

    def run_attempt(self, request: AttemptRequest, state: AttemptState) -> AttemptDecision:
        """Run the selected inner policy's agent turns."""
        return self.attempts.run_attempt(request, state)

    def project_performance(
        self, request: AttemptRequest, state: AttemptState
    ) -> PerformanceProjection:
        """Project evidence according to the selected inner policy."""
        return self.attempts.project_performance(request, state)

    def reviewed(self, state: AttemptState) -> bool:
        """Use the inner policy's review semantics."""
        return self.attempts.reviewed(state)

    def keeps_hypothesis_active(self, state: AttemptState, continuation_rounds: int) -> bool:
        """Use the inner policy's bounded implementer lease."""
        return self.attempts.keeps_hypothesis_active(state, continuation_rounds)

    def terminal_success_needs_parent_choice(
        self, state: AttemptState, continuation_rounds: int
    ) -> bool:
        """Use the inner policy's terminal handoff semantics."""
        return self.attempts.terminal_success_needs_parent_choice(state, continuation_rounds)

    def profile_outcome(self, request: ProfileOutcomeInput) -> None:
        """Plain agent rounds do not update a profile controller."""
        return self.profile.outcome(request)


class MultiAgentFlow(_InnerAgentFlow):
    """Orchestrator prepass, optional profiler, implementer, then judge."""

    def __init__(
        self, round_services: RoundPreparationServices, attempt_services: AttemptServices
    ) -> None:
        """Bind specialist role turns and their review policy."""
        super().__init__(
            MultiAgentRoundPreparation(round_services), MultiAgentAttemptPolicy(attempt_services)
        )


class SingleAgentFlow(_InnerAgentFlow):
    """Designer plus one combined implementation, profile, and review turn."""

    def __init__(self, attempt_services: AttemptServices) -> None:
        """Bind the combined agent turn and its evidence projection."""
        super().__init__(SingleAgentRoundPreparation(), SingleAgentAttemptPolicy(attempt_services))


class ProfileGuidedFlow:
    """Outer profile-guided control flow composed with either inner strategy."""

    def __init__(self, inner: BuiltInAgentControlFlow, settings: ProfileGuidedInput) -> None:
        """Wrap an already selected inner agent strategy."""
        self.inner = inner
        self.profile: ProfilePolicy = ProfileGuidedPolicy(settings)

    @property
    def profile_config(self) -> ProfileGuidedInput | None:
        """Configure the persistent hypothesis profile controller."""
        return self.profile.config

    def prepare_profile(
        self, request: ProfilePreparation
    ) -> tuple[HypothesisEngine, AgentRunState]:
        """Run the outer component profile before inner planning."""
        return self.profile.prepare(request)

    def profiler_summary(self, request: RoundPreparationRequest) -> ProfilerSummary | None:
        """Preserve the selected inner policy's profiler communication."""
        return self.inner.profiler_summary(request)

    def official_reason(self, reason: str | None, engine: HypothesisEngine) -> str | None:
        """Require component measurement when profile guidance is active."""
        return self.profile.official_reason(reason, engine)

    def run_attempt(self, request: AttemptRequest, state: AttemptState) -> AttemptDecision:
        """Preserve the selected inner policy's agent turn order."""
        return self.inner.run_attempt(request, state)

    def project_performance(
        self, request: AttemptRequest, state: AttemptState
    ) -> PerformanceProjection:
        """Preserve the selected inner policy's evidence rules."""
        return self.inner.project_performance(request, state)

    def reviewed(self, state: AttemptState) -> bool:
        """Preserve the selected inner policy's review rules."""
        return self.inner.reviewed(state)

    def keeps_hypothesis_active(self, state: AttemptState, continuation_rounds: int) -> bool:
        """Preserve the selected inner policy's bounded lease."""
        return self.inner.keeps_hypothesis_active(state, continuation_rounds)

    def terminal_success_needs_parent_choice(
        self, state: AttemptState, continuation_rounds: int
    ) -> bool:
        """Preserve the selected inner policy's terminal handoff."""
        return self.inner.terminal_success_needs_parent_choice(state, continuation_rounds)

    def profile_outcome(self, request: ProfileOutcomeInput) -> ProfileGuidanceOutcome | None:
        """Feed the official result back to the outer profile controller."""
        return self.profile.outcome(request)


def validate_control_flow(
    inner_loop: str, outer_loop: str, settings: ProfileGuidedInput | None
) -> None:
    """Reject invalid built-in policy selections before creating run resources."""
    validate_inner_policy(inner_loop)
    if outer_loop == "profile-guided" and settings is None:
        message = "profile-guided runs require profile guidance settings"
        raise ValueError(message)


def control_flow_for(
    inner_loop: str,
    outer_loop: str,
    settings: ProfileGuidedInput | None,
    round_services: RoundPreparationServices,
    attempt_services: AttemptServices,
) -> BuiltInAgentControlFlow:
    """Compose one internal control flow before the shared executor starts."""
    inner_factories = {
        "multi-agent": lambda: MultiAgentFlow(round_services, attempt_services),
        "single-agent": lambda: SingleAgentFlow(attempt_services),
    }
    inner = inner_factories[inner_loop]()
    if outer_loop == "profile-guided":
        if settings is None:
            message = "profile-guided runs require profile guidance settings"
            raise ValueError(message)
        return ProfileGuidedFlow(inner, settings)
    return inner
