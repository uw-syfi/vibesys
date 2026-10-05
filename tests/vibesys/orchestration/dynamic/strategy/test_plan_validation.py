"""Plan validation names the offending field for the planner's correction turn."""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.strategy._replies import implement, plan_reply

from vibesys.orchestration.dynamic.models import PortfolioPlan
from vibesys.orchestration.dynamic.strategy.api import DynamicStrategyState, PlanCheck, validate


def _check(*entries: dict[str, object], capacity: int = 8) -> PlanCheck:
    plan = PortfolioPlan.model_validate_json(plan_reply(*entries))
    return validate(plan, DynamicStrategyState(), (), capacity=capacity, profiling=False)


def test_repeated_id_is_a_violation() -> None:
    """Ports test_plan_ids: a plan that repeats a hypothesis ID is rejected by path."""
    check = _check(implement("a"), implement("a"))
    assert not check.valid
    assert any("workstreams[1]" in item.path for item in check.violations)


def test_distinct_ids_are_accepted() -> None:
    """Ports test_plan_ids: distinct canonical IDs make a valid plan."""
    check = _check(implement("a"), implement("b"))
    assert check.valid
    assert [item.work_id for item in check.accepted] == ["a", "b"]


def test_unknown_continuation_is_a_violation() -> None:
    """Ports test_plan_ids::test_semantically_rejected_ids_name_the_exact_plan_field (unknown_continuation): continuing an unseen hypothesis is rejected."""
    check = _check(implement("a", continue_hypothesis=True))
    assert not check.valid
    assert check.violations[0].path == "workstreams[0].hypothesis_id"


@given(st.lists(st.sampled_from("abcdef"), min_size=1, max_size=6))
def test_violations_never_raise_and_partition_the_plan(identifiers: list[str]) -> None:
    """For any ID list, validation returns accepted entries without repeats."""
    check = _check(*(implement(item) for item in identifiers))
    accepted = [item.work_id for item in check.accepted]
    assert len(accepted) == len(set(accepted))
    assert set(accepted) <= set(identifiers)
    assert check.valid == (len(identifiers) == len(set(identifiers)))


def test_plan_json_helper_is_valid_json() -> None:
    """The scenario helper builds the legacy reply shape."""
    assert json.loads(plan_reply(implement("a")))["workstreams"][0]["hypothesis_id"] == "a"
