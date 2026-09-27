"""Typed stub replies owned by the multi policy."""

from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.multi.contracts import ImplementerResponse
from vibesys.orchestration.multi.stub import scripted_response
from vibesys.orchestration.profilers import ProfilerSummary
from vs_loop_state.api import HypothesisOutcome


def test_scripted_multi_trajectory_advances_and_closes_hypotheses() -> None:
    first_plan = scripted_response(OrchestratorPlan, 1)
    third_plan = scripted_response(OrchestratorPlan, 3)
    continuing = scripted_response(ImplementerResponse, 1)
    supported = scripted_response(ImplementerResponse, 2)
    disproven = scripted_response(ImplementerResponse, 6)

    assert isinstance(first_plan, OrchestratorPlan)
    assert isinstance(third_plan, OrchestratorPlan)
    assert first_plan.hypothesis_id == "H-01"
    assert third_plan.hypothesis_id == "H-02"
    assert isinstance(continuing, ImplementerResponse)
    assert isinstance(supported, ImplementerResponse)
    assert isinstance(disproven, ImplementerResponse)
    assert continuing.hypothesis_outcome is HypothesisOutcome.CONTINUE
    assert supported.hypothesis_outcome is HypothesisOutcome.SUPPORTED
    assert disproven.hypothesis_outcome is HypothesisOutcome.DISPROVEN


def test_scripted_multi_profile_uses_the_round_metric() -> None:
    profile = scripted_response(ProfilerSummary, 3)

    assert isinstance(profile, ProfilerSummary)
    assert profile.perf_metric == 1045.0
    assert profile.perf_unit == "median_tok_per_sec"
