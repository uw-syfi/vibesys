"""The run's overview names every axis its candidates are ranked on, as its options declare them.

The axes are the run's metric space (which the operator may override), so they reach the
overview from the options and never from the bundle; a client plots a task with two of them.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.events import ObjectiveOverview, RunOverview
from vibesys.metrics import MetricSpace, Objective
from vibesys.orchestration.agent_options import AgentOrchestrationOptions, objective_axes_of
from vs_sim.api.testing import examples

_AXES = st.lists(
    st.builds(
        Objective,
        name=st.sampled_from(("throughput", "latency", "memory", "accuracy")),
        direction=st.sampled_from(("max", "min")),
    ),
    unique_by=lambda objective: objective.name,
    max_size=4,
)


def _options(objectives: list[Objective]) -> AgentOrchestrationOptions:
    return AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=1,
        max_retries_per_round=1,
        judge_every=1,
        metric_space=MetricSpace(objectives=tuple(objectives)),
    )


@settings(max_examples=examples(pr=100, nightly=1000))
@given(objectives=_AXES)
def test_the_overview_carries_the_options_axes_in_order_and_survives_the_wire(
    objectives: list[Objective],
) -> None:
    axes = objective_axes_of(_options(objectives))

    assert [(axis.name, axis.direction) for axis in axes] == [
        (objective.name, objective.direction) for objective in objectives
    ]
    overview = RunOverview(objective=ObjectiveOverview(text="Go faster.", objectives=axes))
    assert RunOverview.model_validate_json(overview.model_dump_json()) == overview


def test_options_that_hold_no_metric_space_declare_no_axes() -> None:
    assert objective_axes_of(ObjectiveOverview(text="x")) == ()
    assert ObjectiveOverview(text="x").objectives == ()
