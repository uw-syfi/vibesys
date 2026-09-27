"""Typed stub replies owned by the single policy."""

from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.review import Verdict
from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vibesys.orchestration.single.stub import scripted_response


def test_scripted_single_trajectory_advances_every_two_rounds() -> None:
    first = scripted_response(OrchestratorPlan, 1)
    second = scripted_response(OrchestratorPlan, 2)
    third = scripted_response(OrchestratorPlan, 3)

    assert isinstance(first, OrchestratorPlan)
    assert isinstance(second, OrchestratorPlan)
    assert isinstance(third, OrchestratorPlan)
    assert first.hypothesis_id == second.hypothesis_id == "H-01"
    assert third.hypothesis_id == "H-02"


def test_scripted_single_round_is_a_typed_pass() -> None:
    response = scripted_response(SingleAgentRoundResponse, 1)

    assert isinstance(response, SingleAgentRoundResponse)
    assert response.verdict is Verdict.PASS
    assert response.perf_metric == 1000.0
