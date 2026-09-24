"""Outer profile guidance policies composed with either built-in agent strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys.loops.agent.hypothesis_controller import (
    HypothesisEngine,
    ProfileGuidanceOutcome,
)

if TYPE_CHECKING:
    from vibesys.evaluators.input_manifest import ProfileGuidedInput
    from vibesys.loops.agent.policy_ports import ProfileEffect
    from vibesys.loops.agent.state import AgentRunState


@dataclass(frozen=True)
class ProfilePreparation:
    """Framework context for one outer-policy pre-plan step."""

    effects: ProfileEffect
    engine: HypothesisEngine
    state: AgentRunState
    round_number: int


@dataclass(frozen=True)
class ProfileOutcomeInput:
    """Completed round facts consumed by the outer profile controller."""

    round_number: int
    passed: bool
    official: bool
    delta_pct: float | None


class ProfilePolicy(Protocol):
    """Outer round behavior that can wrap either inner attempt policy."""

    @property
    def config(self) -> ProfileGuidedInput | None:
        """Return the profile controller configuration, if active."""
        ...

    def prepare(self, request: ProfilePreparation, /) -> tuple[HypothesisEngine, AgentRunState]:
        """Prepare profile guidance before a new designer plan."""
        ...

    def official_reason(self, reason: str | None, engine: HypothesisEngine, /) -> str | None:
        """Possibly require an official component measurement."""
        ...

    def outcome(self, request: ProfileOutcomeInput, /) -> ProfileGuidanceOutcome | None:
        """Return profile controller evidence for the completed round."""
        ...


class PlainProfilePolicy:
    """Ordinary agent loop with no outer profile controller."""

    @property
    def config(self) -> None:
        """Keep the hypothesis controller in its plain mode."""
        return None

    def prepare(self, request: ProfilePreparation) -> tuple[HypothesisEngine, AgentRunState]:
        """Leave the round state unchanged."""
        return request.engine, request.state

    def official_reason(self, reason: str | None, _engine: HypothesisEngine) -> str | None:
        """Use the ordinary official evaluation cadence."""
        return reason

    def outcome(self, _request: ProfileOutcomeInput) -> None:
        """Do not write profile controller feedback."""


class ProfileGuidedPolicy:
    """Profile a component before design and measure its completed outcome."""

    def __init__(self, settings: ProfileGuidedInput) -> None:
        """Bind validated profile-guided input settings."""
        self.settings = settings

    @property
    def config(self) -> ProfileGuidedInput:
        """Configure the hypothesis controller for profile guidance."""
        return self.settings

    def prepare(self, request: ProfilePreparation) -> tuple[HypothesisEngine, AgentRunState]:
        """Profile the selected component and durably record its guidance."""
        return request.effects.prepare_profile(
            request.engine, request.state, self.settings, request.round_number
        )

    def official_reason(self, reason: str | None, engine: HypothesisEngine) -> str | None:
        """Measure an active component even when ordinary cadence defers."""
        if engine.controller.guidance.active_component:
            return reason or "profile-guided component measurement"
        return reason

    def outcome(self, request: ProfileOutcomeInput) -> ProfileGuidanceOutcome:
        """Feed completed measurement back into the profile controller."""
        return ProfileGuidanceOutcome.from_round(
            request.round_number, request.passed, request.official, request.delta_pct
        )
