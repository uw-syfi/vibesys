"""Effect boundaries for the built-in agent control flow.

The local adapter owns clients, workspace paths, the issue board, and durable
state. Policies consume only typed turn results and explicit effects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vibesys.evaluators.gates import FrameworkBenchmarkOutcome
    from vibesys.evaluators.input_manifest import ProfileGuidedInput
    from vibesys.loops.agent.hypothesis_controller import HypothesisEngine
    from vibesys.loops.agent.model import AgentRunState
    from vibesys.loops.agent.policy_attempts import AttemptRequest, AttemptState
    from vibesys.loops.agent.policy_rounds import RoundPreparationRequest
    from vibesys.loops.agent.policy_support import _ImplementerAttempt
    from vibesys.schemas import (
        JudgeResponse,
        PreRoundDecision,
        ProfilerSummary,
        SingleAgentRoundResponse,
    )


class AgentTurns(Protocol):
    """Run named agent turns and return their parsed evidence."""

    def pre_round_decision(
        self, request: RoundPreparationRequest, /, *, has_history: bool
    ) -> PreRoundDecision:
        """Run the orchestrator's pre-plan profile decision."""
        ...

    def profile(self, request: RoundPreparationRequest, focus: str, /) -> ProfilerSummary | None:
        """Run a requested specialist profile turn."""
        ...

    def implement(self, request: AttemptRequest, state: AttemptState, /) -> _ImplementerAttempt:
        """Run one paid implementer turn and return parsed evidence."""
        ...

    def judge(
        self, request: AttemptRequest, state: AttemptState, conflict: str | None, /
    ) -> JudgeResponse:
        """Run the reviewer against the latest implementation."""
        ...

    def combined(self, request: AttemptRequest, state: AttemptState, /) -> SingleAgentRoundResponse:
        """Run one combined implementation and review turn."""
        ...


class RoundEffects(Protocol):
    """Durable attempt markers, gates, and diagnostics used by policies."""

    def checkpoint(self, request: AttemptRequest, state: AttemptState, /) -> None:
        """Persist feedback and gate state before another paid turn."""
        ...

    def record_official_decision(
        self,
        request: AttemptRequest,
        state: AttemptState,
        /,
        *,
        run: bool,
        reason: str,
        provisional_candidates: int,
    ) -> None:
        """Journal an official evaluation cadence decision."""
        ...

    def record_judge_skipped(self, request: AttemptRequest, outcome: str, /) -> None:
        """Journal the sparse-review decision."""
        ...

    def validate(
        self, request: AttemptRequest, state: AttemptState, recipe: str | None, /
    ) -> str | None:
        """Run a judge-approved local validation recipe."""
        ...

    def current_commit(self) -> str | None:
        """Return the current workspace revision for gate reuse checks."""
        ...

    def official_gates(
        self,
        request: AttemptRequest,
        state: AttemptState,
        /,
        *,
        reuse_accuracy_pass: bool,
        candidate_commit: str | None,
    ) -> tuple[str | None, FrameworkBenchmarkOutcome, bool]:
        """Run official gates and return feedback, benchmark, accuracy status."""
        ...

    def next_attempt(self, round_number: int) -> int:
        """Return the next unpaid attempt from the durable issue board."""
        ...

    def log(self, message: str, /) -> None:
        """Record a framework diagnostic."""
        ...


class ProfileEffect(Protocol):
    """Perform and persist one profile-guidance preparation."""

    def prepare_profile(
        self,
        engine: HypothesisEngine,
        state: AgentRunState,
        settings: ProfileGuidedInput,
        round_number: int,
    ) -> tuple[HypothesisEngine, AgentRunState]:
        """Prepare profile guidance and commit its new state."""
        ...
