"""The search's own knobs: what it accepts, what it rejects, what the launch adds."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.vibesys.orchestration.evolve._host import config

from vibesys.evolve_core import evolve_core_registration
from vibesys.orchestration.evolve.launch import EvolveLaunch
from vibesys.orchestration.evolve.search.api import EvolveConfig, budget
from vs_core.api.search import WorkspaceMode
from vs_sim.api.testing import examples

REQUIRED = {
    "max_generations": 2,
    "children_per_generation": 3,
    "k_top_inspirations": 1,
    "k_random_inspirations": 1,
    "selection_temperature": 1.0,
    "frontier_bias": 0.7,
    "bootstrap_max_attempts": 4,
}


@settings(max_examples=examples(pr=8, nightly=100))
@given(key=st.sampled_from(["keep_deployments", "turn_drop_backoff_seconds", "interface", "x"]))
def test_a_key_the_search_does_not_own_is_rejected_by_name(key: str) -> None:
    with pytest.raises(ValidationError, match=key):
        EvolveConfig.model_validate({**REQUIRED, key: 1})


def test_a_required_knob_cannot_be_left_out() -> None:
    for name in REQUIRED:
        with pytest.raises(ValidationError, match=name):
            EvolveConfig.model_validate({k: v for k, v in REQUIRED.items() if k != name})


def test_openevolve_knobs_select_that_policy_and_clash_with_the_built_in_one() -> None:
    assert config().selector == "vibesys"
    assert config(openevolve_num_islands=3).selector == "openevolve"
    assert config(search_policy="openevolve").needs_code
    with pytest.raises(ValidationError, match="require search_policy='openevolve'"):
        config(search_policy="vibesys", openevolve_num_islands=3)


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    cold=st.integers(1, 5),
    generations=st.integers(1, 4),
    children=st.integers(1, 4),
    parallel=st.integers(1, 4),
)
def test_the_budget_follows_the_config(
    cold: int, generations: int, children: int, parallel: int
) -> None:
    cfg = config(
        bootstrap_max_attempts=cold,
        max_generations=generations,
        children_per_generation=children,
        max_parallelism=parallel,
    )
    spend = budget(cfg)
    assert cfg.total_attempts == cold + generations * children
    assert (spend.attempts, spend.refunds, spend.parallel) == (
        cfg.total_attempts,
        cfg.total_attempts,
        parallel,
    )
    assert spend.turns % cfg.total_attempts == 0


def test_the_launch_adds_the_tasks_inputs_to_the_searchs_knobs_and_leaves_profiling_to_the_host() -> (
    None
):
    launch = EvolveLaunch.model_validate({**REQUIRED, "modality": "text_generation"})
    assert launch.modality == "text_generation"
    assert launch.search_config(profiling=True).profiling
    assert not launch.search_config(profiling=False).profiling
    assert "modality" not in EvolveConfig.model_fields
    with pytest.raises(ValidationError, match="'profiling' is not a launch option"):
        EvolveLaunch.model_validate({**REQUIRED, "profiling": False})
    with pytest.raises(ValidationError, match="keep_deployments"):
        EvolveLaunch.model_validate({**REQUIRED, "keep_deployments": False})


@settings(max_examples=examples(pr=8, nightly=100))
@given(parallel=st.integers(1, 6))
def test_a_host_without_candidate_sandboxes_runs_the_children_one_at_a_time_in_its_root(
    parallel: int,
) -> None:
    launch = EvolveLaunch.model_validate({**REQUIRED, "max_parallelism": parallel})
    assert launch.search_config(profiling=False).max_parallelism == parallel
    shared = launch.search_config(profiling=False, workspace_mode=WorkspaceMode.EXCLUSIVE_ROOT)
    assert (shared.exclusive_root, shared.max_parallelism) == (True, 1)
    with pytest.raises(ValidationError, match="'exclusive_root' is not a launch option"):
        EvolveLaunch.model_validate({**REQUIRED, "exclusive_root": True})
    if parallel > 1:
        with pytest.raises(ValidationError, match="cannot run in parallel"):
            config(exclusive_root=True, max_parallelism=parallel)


@settings(max_examples=examples(pr=8, nightly=100))
@given(generations=st.integers(1, 50))
def test_the_run_reports_the_operators_generations_from_the_launch_it_parsed(
    generations: int,
) -> None:
    """The registration reads a parsed launch, whose host-decided fields it must not re-validate."""
    launch = EvolveLaunch.model_validate({**REQUIRED, "max_generations": generations})
    registration = evolve_core_registration()
    assert registration.project_max_rounds is not None
    assert registration.project_max_rounds(launch) == generations
