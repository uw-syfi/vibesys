"""Outer hypothesis decisions without agents, workspace, or run context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibesys.loops.agent.hypothesis_controller import HypothesisEngine
from vibesys.loops.agent.model import AgentRunState
from vibesys.loops.agent.policy_attempts import AttemptState
from vibesys.loops.agent.policy_scheduler import (
    PlanRequest,
    RoundSelection,
    RoundSelectionRequest,
    TerminalRequest,
    apply_requested_rollback,
    select_round,
    transition_round,
)
from vibesys.loops.agent.policy_support import _CarryOver
from vibesys.schemas import ImplementerResponse, OrchestratorPlan
from vs_loop_state.api import RoundHistory, RoundRecord

if TYPE_CHECKING:
    from vibesys.evaluators.input_manifest import ProfileGuidedInput
    from vibesys.loops.agent.hypothesis_controller import ProfileGuidanceOutcome
    from vibesys.loops.agent.model import Hypothesis
    from vibesys.loops.agent.policy_attempts import (
        AttemptDecision,
        AttemptRequest,
        PerformanceProjection,
    )
    from vibesys.loops.agent.policy_ports import RoundPreparationRequest
    from vibesys.loops.agent.policy_profile import ProfileOutcomeInput, ProfilePreparation
    from vibesys.schemas import ProfilerSummary


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="h1",
        hypothesis="cache decode",
        task="implement cache",
        pass_criteria="tests pass",  # noqa: S106
        reasoning="measure decode",
    )


@dataclass
class _Effects:
    calls: list[str] = field(default_factory=list)
    selected_plan: OrchestratorPlan = field(default_factory=_plan)
    rollback_success: bool = True

    def prepare_profile(
        self,
        engine: HypothesisEngine,
        state: AgentRunState,
        settings: ProfileGuidedInput,
        round_number: int,
    ) -> tuple[HypothesisEngine, AgentRunState]:
        del settings, round_number
        self.calls.append("profile-effect")
        return engine, state

    def plan(self, request: PlanRequest) -> OrchestratorPlan:
        self.calls.append("designer")
        assert request.round_number == 1
        assert request.provisional_candidates == 0
        return self.selected_plan

    def current_commit(self) -> str:
        self.calls.append("parent-commit")
        return "a" * 40

    def persist_started(self, _state: AgentRunState, _plan: OrchestratorPlan) -> None:
        self.calls.append("persist-start")

    def record_continuation(self, round_number: int, _hypothesis: Hypothesis) -> None:
        self.calls.append(f"continuation:{round_number}")

    def checkout_rollback(
        self, commit: str, parent_round: int, failed_child_round: int | None
    ) -> bool:
        self.calls.append(f"checkout:{commit[:8]}:{parent_round}:{failed_child_round}")
        return self.rollback_success

    def persist_rollback(self, state: AgentRunState, _hypothesis: Hypothesis) -> AgentRunState:
        self.calls.append("persist-rollback")
        return state

    def warn(self, message: str) -> None:
        self.calls.append(f"warn:{message}")


@dataclass
class _Flow:
    calls: list[str]

    @property
    def config(self) -> None:
        return None

    def prepare(self, request: ProfilePreparation) -> tuple[HypothesisEngine, AgentRunState]:
        self.calls.append("prepare-profile")
        return request.engine, request.state

    def profiler_summary(self, request: RoundPreparationRequest) -> ProfilerSummary | None:
        del request
        self.calls.append("prepass")
        return None

    def run_attempt(self, _request: AttemptRequest, _state: AttemptState) -> AttemptDecision:
        raise AssertionError

    def project_performance(
        self, _request: AttemptRequest, _state: AttemptState
    ) -> PerformanceProjection:
        raise AssertionError

    def reviewed(self, _state: AttemptState) -> bool:
        raise AssertionError

    def official_reason(self, reason: str | None, _engine: HypothesisEngine) -> str | None:
        self.calls.append("official-reason")
        return reason

    def keeps_hypothesis_active(self, _state: AttemptState, _continuation_rounds: int) -> bool:
        return False

    def terminal_success_needs_parent_choice(
        self, _state: AttemptState, _continuation_rounds: int
    ) -> bool:
        return False

    def outcome(self, _request: ProfileOutcomeInput) -> ProfileGuidanceOutcome | None:
        return None


def test_new_hypothesis_runs_designer_and_continuation_reuses_plan() -> None:
    effects = _Effects()
    flow = _Flow(effects.calls)
    engine = HypothesisEngine.create(AgentRunState(), config=None)
    first = select_round(
        flow,
        flow,
        effects,
        RoundSelectionRequest(engine, engine.state, [], _CarryOver(), 1, 3, 2, None),
    )

    assert first.hypothesis.parent_commit == "a" * 40
    assert first.planned_official_reason is None
    assert effects.calls == [
        "prepare-profile",
        "prepass",
        "designer",
        "parent-commit",
        "persist-start",
        "official-reason",
    ]

    effects.calls.clear()
    second = select_round(
        flow,
        flow,
        effects,
        RoundSelectionRequest(first.engine, first.state, [], _CarryOver(), 2, 3, 2, None),
    )
    assert second.hypothesis.hypothesis_id == first.hypothesis.hypothesis_id
    assert second.plan == first.plan
    assert second.planned_official_reason is None
    assert effects.calls == ["continuation:2", "official-reason"]


def test_review_failure_retains_bounded_claim_and_sets_exhaustion_carry() -> None:
    engine = HypothesisEngine.create(AgentRunState(), config=None).start(_plan(), started_round=1)
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    attempt = AttemptState(
        agent_run_state=engine.state,
        feedback="fix validation",
        implementation=ImplementerResponse(
            summary="changed", expected_behavior="faster", next_step="repair parser"
        ),
    )
    record = RoundRecord(
        round_number=1,
        commit=None,
        perf_metric=None,
        perf_unit=None,
        hypothesis_id="h1",
        passed=False,
        judge_verdict="fail",
        hypothesis_outcome="rejected",
    )

    terminal = transition_round(
        _Flow([]),
        _Flow([]),
        TerminalRequest(
            engine=engine,
            state=engine.state,
            hypothesis=hypothesis,
            attempt=attempt,
            record=record,
            records=[],
            carry=_CarryOver(),
            reviewed=True,
            max_retries_per_round=3,
        ),
    )

    active = terminal.state.active_hypothesis
    assert active is not None
    assert active.feedback == "fix validation"
    assert active.continuation_rounds == 1
    assert terminal.exhaustion_feedback == "fix validation"
    assert "fix validation" in (terminal.carry.exhaustion_info or "")


def test_terminal_success_releases_claim_and_reports_discarded_official_candidate() -> None:
    engine = HypothesisEngine.create(AgentRunState(), config=None).start(_plan(), started_round=1)
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    attempt = AttemptState(agent_run_state=engine.state, feedback=None, passed=True)
    record = RoundRecord(
        round_number=1,
        commit=None,
        hypothesis_id="h1",
        passed=True,
        judge_verdict="pass",
        official_evaluation=True,
        candidate_retained=False,
        perf_metric=12.0,
        perf_unit="ops",
    )

    terminal = transition_round(
        _Flow([]),
        _Flow([]),
        TerminalRequest(
            engine=engine,
            state=engine.state,
            hypothesis=hypothesis,
            attempt=attempt,
            record=record,
            records=[],
            carry=_CarryOver(),
            reviewed=True,
            max_retries_per_round=3,
        ),
    )

    assert terminal.state.active_hypothesis is None
    assert terminal.exhaustion_feedback is None
    assert "not retained: 12.0 ops" in (terminal.carry.regression_info or "")


def test_rollback_uses_failed_child_base_and_persists_only_after_checkout() -> None:
    plan = _plan().model_copy(update={"revert_to_round": 1})
    engine = HypothesisEngine.create(AgentRunState(), config=None).start(plan, started_round=3)
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    selection = RoundSelection(engine, engine.state, hypothesis, plan, None)
    target = RoundRecord(
        round_number=1, commit="a" * 40, perf_metric=None, perf_unit=None, passed=True
    )
    failed_child = RoundRecord(
        round_number=2,
        commit="b" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=False,
        hypothesis_parent_round=1,
        hypothesis_parent_commit="c" * 40,
        hypothesis_outcome="rejected",
    )
    records = [target, failed_child]
    effects = _Effects(rollback_success=False)

    state = apply_requested_rollback(effects, selection, RoundHistory(records), records)
    assert state is selection.state
    assert hypothesis.revert_applied is False
    assert effects.calls == [
        "checkout:cccccccc:1:2",
        "warn:rollback was not applied; will retry round 1 on the next continuation",
    ]

    effects.calls.clear()
    effects.rollback_success = True
    state = apply_requested_rollback(effects, selection, RoundHistory(records), records)
    assert state is selection.state
    assert hypothesis.revert_applied is True
    assert hypothesis.parent_commit == "c" * 40
    assert effects.calls == ["checkout:cccccccc:1:2", "persist-rollback"]
