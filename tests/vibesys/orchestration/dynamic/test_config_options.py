"""The dynamic search's config rejects what it cannot run, naming the key, and its operations are fixed.

The config is the one input of the search: it is checkpointed with the run and read again on
every restart, so a value outside its bounds or a key nothing reads must be refused when the run
is configured, never later. The operations are the persisted kind names the executors serve.
"""

from __future__ import annotations

import json
from typing import Any, get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.vibesys.inputs.test_plan import plans

from vibesys.inputs import PlanStep, StepEvaluation, TaskPlan
from vibesys.orchestration.dynamic.launch import DynamicLaunch
from vibesys.orchestration.dynamic.search.api import (
    INTERPRET_KIND,
    OPERATIONS,
    RENDER_KIND,
    VERIFY_KIND,
    DynamicConfig,
    Outline,
    Step,
)
from vs_sim.api.testing import examples

VALID = {"max_rounds": 3}
LAUNCH = {**VALID, "interface": "an HTTP endpoint"}
BOUNDS = {
    "max_rounds": (1, 1000),
    "max_in_flight": (1, 32),
    "judge_every": (1, 1000),
    "max_unmeasured_turns": (1, 32),
    "max_continuation_rounds": (0, 1000),
    "evaluation_slots": (1, 32),
    "max_backlog": (0, 32),
}


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    key=st.text("abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=24).filter(
        lambda name: name not in DynamicConfig.model_fields
    )
)
def test_an_unknown_option_is_rejected_by_name(key: str) -> None:
    with pytest.raises(ValidationError, match=key):
        DynamicConfig.model_validate({**VALID, key: 2})


@pytest.mark.parametrize("name", sorted(BOUNDS))
@settings(max_examples=examples(pr=8, nightly=100))
@given(data=st.data())
def test_a_value_outside_its_bounds_is_rejected_naming_the_option_and_one_inside_is_kept(
    name: str, data: st.DataObject
) -> None:
    low, high = BOUNDS[name]
    outside = data.draw(st.integers(max_value=low - 1) | st.integers(min_value=high + 1))
    with pytest.raises(ValidationError, match=name):
        DynamicConfig.model_validate({**VALID, name: outside})
    inside = data.draw(st.integers(low, high))
    assert getattr(DynamicConfig.model_validate({**VALID, name: inside}), name) == inside


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    values=st.fixed_dictionaries(
        {name: st.integers(low, high) for name, (low, high) in BOUNDS.items()}
    ),
    profiling=st.booleans(),
)
def test_a_config_round_trips_through_json_and_its_budget_is_rounds_times_width(
    values: dict[str, int], *, profiling: bool
) -> None:
    config = DynamicConfig.model_validate({**values, "profiling": profiling})
    assert DynamicConfig.model_validate_json(config.model_dump_json()) == config
    assert config.start_budget == config.max_rounds * config.max_in_flight


def test_the_search_declares_exactly_the_operations_its_executors_serve() -> None:
    kinds = {item.kind for item in OPERATIONS.registry().descriptors}
    assert kinds == {RENDER_KIND, VERIFY_KIND, INTERPRET_KIND}
    assert kinds == {
        "dynamic.render_role_artifacts",
        "dynamic.verify_parent_revision",
        "dynamic.interpret_evidence",
    }


@settings(max_examples=examples(pr=4, nightly=20))
@given(profiling=st.booleans(), allowed=st.booleans())
def test_profiling_is_never_a_launch_option_but_follows_the_plan(
    *, profiling: bool, allowed: bool
) -> None:
    with pytest.raises(ValidationError, match="profiling"):
        DynamicLaunch.model_validate({**LAUNCH, "profiling": profiling})

    launch = DynamicLaunch.model_validate(LAUNCH)

    assert launch.search_config(profiling=allowed, evaluation_capacity=1).profiling is allowed


# -- following the task's plan --------------------------------------------------------------------


def _launch_json(**options: object) -> str:
    return json.dumps({**LAUNCH, **options})


@settings(max_examples=examples(pr=8, nightly=100))
@given(plan=st.none() | plans())
def test_following_a_plan_is_refused_when_the_task_declares_none_naming_the_setting(
    plan: dict[str, Any] | None,
) -> None:
    options = {"mode": "follow_plan"} if plan is None else {"mode": "follow_plan", "plan": plan}
    if plan is None:
        # as the launcher parses a run's options (strict JSON), and as the search reads its config
        with pytest.raises(ValidationError, match=r"mode 'follow_plan'.*\[plan\]"):
            DynamicLaunch.model_validate_json(_launch_json(**options), strict=True)
        with pytest.raises(ValidationError, match=r"mode 'follow_plan'.*\[plan\]"):
            DynamicConfig.model_validate({**VALID, **options})
        return
    launch = DynamicLaunch.model_validate_json(_launch_json(**options), strict=True)
    config = launch.search_config(profiling=False, evaluation_capacity=1)
    assert config.follows_plan
    assert config.plan is not None
    assert config.plan == Outline.model_validate_json(
        TaskPlan.model_validate(plan).model_dump_json()
    )
    assert DynamicConfig.model_validate_json(config.model_dump_json()) == config


@settings(max_examples=examples(pr=8, nightly=100))
@given(plan=plans())
def test_an_exploring_run_is_not_given_the_task_plan(plan: dict[str, Any]) -> None:
    for options in ({}, {"mode": "explore"}):
        launch = DynamicLaunch.model_validate_json(_launch_json(plan=plan, **options), strict=True)
        config = launch.search_config(profiling=False, evaluation_capacity=1)
        assert config.mode == "explore"
        assert config.plan is None


def test_the_search_copy_of_a_plan_step_has_the_fields_of_the_task_table() -> None:
    assert set(Step.model_fields) == set(PlanStep.model_fields)
    assert set(Outline.model_fields) == set(TaskPlan.model_fields)
    assert set(get_args(Step.model_fields["evaluate"].annotation)) == set(StepEvaluation)
