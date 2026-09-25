"""Profile-guided policy tests over the shared hypothesis engine."""

from vibesys.agent_run.state import (
    AgentRunState,
    ProfileBottleneck,
    ProfileGuidanceStatus,
)
from vibesys.loops.profile_single.hypothesis import (
    HypothesisEngine,
    ProfileGuidanceOutcome,
    ProfileGuidedHypothesisController,
)
from vibesys.search.hypothesis import OrchestratorPlan
from vs_loop_state.api import RoundRecord


def _ranking(*names: str) -> tuple[ProfileBottleneck, ...]:
    return tuple(
        ProfileBottleneck(name=name, cost=100 - index, share=0.5, evidence=["sample"])
        for index, name in enumerate(names)
    )


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="H-1",
        hypothesis="component-local work reduces measured cost",
        task="optimize the focused component",
        pass_criteria="configured gates pass",  # noqa: S106
        reasoning="test the profile-guided claim",
    )


def _record() -> RoundRecord:
    return RoundRecord(
        round_number=1,
        commit="1" * 40,
        perf_metric=1.0,
        perf_unit="seconds",
        passed=True,
        reviewed=True,
        hypothesis_id="H-1",
        hypothesis_outcome="proven",
        hypothesis_claim="component-local work reduces measured cost",
        hypothesis_task="optimize the focused component",
        official_evaluation=True,
    )


def test_disabled_controller_leaves_ordinary_agent_state_unchanged() -> None:
    state = AgentRunState()
    controller = ProfileGuidedHypothesisController.create(state)

    prepared = controller.prepare_round(round_number=1, attribution=_ranking("hot"))

    assert prepared.state == state
    assert prepared.guidance.active_component == ""


def test_controller_preserves_focus_until_measured_plateau() -> None:
    controller = ProfileGuidedHypothesisController.create(AgentRunState(), enabled=True)
    controller = controller.prepare_round(round_number=1, attribution=_ranking("hot", "next"))
    assert controller.guidance.active_component == "hot"

    controller = controller.advance_round(
        round_number=1, passed=False, relative_improvement=None
    ).prepare_round(round_number=2, attribution=_ranking("next", "hot"))
    assert controller.guidance.active_component == "hot"

    controller = controller.advance_round(
        round_number=2, passed=True, relative_improvement=0.01
    ).advance_round(round_number=3, passed=True, relative_improvement=0.0)
    state = controller.state.profile_guidance
    assert state is not None
    assert state.active_component is None
    assert state.components[1].status is ProfileGuidanceStatus.EXHAUSTED
    assert state.components[1].rounds_spent == 2


def test_hypothesis_engine_commits_round_and_policy_in_one_state() -> None:
    controller = ProfileGuidedHypothesisController.create(AgentRunState(), enabled=True)
    controller = controller.prepare_round(round_number=1, attribution=_ranking("hot"))
    engine = HypothesisEngine(controller).start(_plan(), started_round=1)

    completed = engine.complete_round(
        _record(),
        next_active=None,
        profile_outcome=ProfileGuidanceOutcome(
            round_number=1, passed=True, relative_improvement=0.01
        ),
    ).state

    assert [record.round_number for record in completed.rounds] == [1]
    assert completed.active_hypothesis_id is None
    assert completed.profile_guidance is not None
    assert completed.profile_guidance.components[0].rounds_spent == 1
