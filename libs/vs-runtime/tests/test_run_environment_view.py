"""A run-environment view's parallel-candidate support follows from its structure.

Regression for #1549: the Docker environment declared that it could not open a
session per candidate although each candidate gets its own worktree and
container. The fact is no longer a field an environment sets, so a view cannot
contradict the structure it describes.
"""

from __future__ import annotations

from dataclasses import fields

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api.infrastructure import AgentPaths, RunEnvironmentView

_views = st.builds(
    RunEnvironmentView,
    paths=st.just(AgentPaths()),
    isolated=st.booleans(),
    cli_sandboxed=st.booleans(),
    share_agent_session=st.booleans(),
    parallel_candidate_blocker=st.none() | st.text(min_size=1, max_size=12),
)


@given(_views)
def test_support_is_exactly_the_absence_of_an_obstacle(view: RunEnvironmentView) -> None:
    assert view.supports_parallel_candidate_evaluation == (view.parallel_candidate_obstacle is None)


@given(_views)
def test_support_follows_from_the_container_the_agent_runs_in(view: RunEnvironmentView) -> None:
    expected = (
        view.cli_sandboxed
        and not view.share_agent_session
        and view.parallel_candidate_blocker is None
    )

    assert view.supports_parallel_candidate_evaluation is expected


@given(_views)
def test_a_refusal_always_carries_its_reason(view: RunEnvironmentView) -> None:
    obstacle = view.parallel_candidate_obstacle

    assert obstacle is None or obstacle.strip()


def test_support_is_not_a_field_an_environment_can_set() -> None:
    declared = {field.name for field in fields(RunEnvironmentView)}

    assert "supports_parallel_candidate_evaluation" not in declared


@given(_views)
def test_a_built_view_cannot_be_overwritten_with_a_claim(view: RunEnvironmentView) -> None:
    with pytest.raises(AttributeError):
        view.supports_parallel_candidate_evaluation = True  # ty: ignore[invalid-assignment]
