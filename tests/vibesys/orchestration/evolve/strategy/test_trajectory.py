"""The population a given ``--seed`` produces equals the legacy loop's, whatever the completion order.

The fixture holds the populations the legacy serial loop built for a scripted world (see
``tests/vibesys/orchestration/evolve/world.py``), recorded before that loop was replaced. The same
world drives the strategy on the production shell, serially and with concurrent children whose
trusted evaluations complete in drawn orders; admission is in slot order, so the population is the
same in every one of them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve import world
from tests.vibesys.orchestration.evolve.strategy.world import EvolveWorld, commit_of, run_to_end

from vibesys.metrics import MetricSpace, Objective
from vibesys.orchestration.evolve.population import (
    CandidateOutcome,
    PopulationConfig,
    PopulationSearch,
    PopulationState,
    Proposal,
)
from vibesys.orchestration.evolve.strategy.api import EvolveStrategyState
from vs_core.api import component

GOLDEN = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "legacy_trajectories.json").read_text()
)
GENERATIONS, CHILDREN, ATTEMPTS = GOLDEN["generations"], GOLDEN["children"], GOLDEN["attempts"]


def _names() -> list[str]:
    return [
        *(world.bootstrap_name(number) for number in range(1, ATTEMPTS + 1)),
        *world.generation_names(GENERATIONS, CHILDREN),
    ]


def _population(state: object) -> list[dict[str, object]]:
    assert isinstance(state, EvolveStrategyState)
    assert state.population is not None
    rows: list[dict[str, object]] = []
    for item in state.population.individuals:
        row = item.model_dump(mode="json")
        row["metrics"] = dict(item.metrics)
        row.pop("commit")
        row["has_commit"] = item.commit is not None
        rows.append(row)
    return rows


def _expected(key: str) -> tuple[int, int, list[dict[str, object]]]:
    run = GOLDEN["runs"][key]
    rows = []
    for item in run["individuals"]:
        row = dict(item)
        row["inspiration_ids"] = list(row["inspiration_ids"])
        rows.append(row)
    seed_world, seed_rng = (int(part.split("=")[1]) for part in key.split(",")[:2])
    return seed_world, seed_rng, rows


def _space(key: str) -> MetricSpace:
    objectives = (Objective("throughput", "max"),)
    if key.endswith("two"):
        objectives = (*objectives, Objective("ttft_ms", "min"))
    return MetricSpace(objectives=objectives)


def _scripted(
    key: str,
    *,
    parallelism: int,
    delays: tuple[int, ...],
    selector: Literal["vibesys", "openevolve"] = "vibesys",
) -> EvolveWorld:
    """The world of the golden run ``key``; ``delays`` reorders when its jobs complete."""
    seed_world, seed_rng, _ = _expected(key)
    return EvolveWorld(
        population_seed=seed_world,
        polls=delays,
        max_generations=GENERATIONS,
        children_per_generation=CHILDREN,
        bootstrap_max_attempts=ATTEMPTS,
        k_top_inspirations=1,
        k_random_inspirations=1,
        seed=seed_rng,
        metric_space=_space(key),
        max_parallelism=parallelism,
        profiler_id="linux_cpu",
        profiler_support_name="linux_cpu_profiler",
        profiler_mcp_name="linux-cpu-profiler",
        selector=selector,
    )


def _run(key: str, *, parallelism: int, delays: tuple[int, ...]) -> list[dict[str, object]]:
    run = run_to_end(_scripted(key, parallelism=parallelism, delays=delays))
    return _population(run.state)


@pytest.mark.parametrize("key", sorted(GOLDEN["runs"])[::2])
def test_the_serial_run_reproduces_the_legacy_population(key: str) -> None:
    _, _, expected = _expected(key)
    assert _run(key, parallelism=1, delays=(0,)) == expected


@pytest.mark.parametrize("key", sorted(GOLDEN["runs"])[1:4:2])
def test_concurrent_children_reproduce_the_legacy_population(key: str) -> None:
    _, _, expected = _expected(key)
    assert _run(key, parallelism=3, delays=(0, 2, 1, 3, 0)) == expected


@settings(
    max_examples=2,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    key=st.sampled_from(sorted(GOLDEN["runs"])),
    parallelism=st.integers(min_value=1, max_value=4),
    delays=st.lists(st.integers(min_value=0, max_value=4), min_size=1, max_size=6).map(tuple),
)
def test_the_population_does_not_depend_on_completion_order(
    key: str, parallelism: int, delays: tuple[int, ...]
) -> None:
    _, _, expected = _expected(key)
    assert _run(key, parallelism=parallelism, delays=delays) == expected


def _reference_population(
    seed_world: int, seed_rng: int, space: MetricSpace, selector: Literal["vibesys", "openevolve"]
) -> PopulationState:
    """The legacy loop's use of the population policy, replayed serially without any agent."""
    config = PopulationConfig(
        space=space,
        selection_temperature=1.0,
        frontier_bias=0.7,
        k_top_inspirations=1,
        k_random_inspirations=1,
        selector=selector,
        seed=seed_rng,
    )
    search = PopulationSearch(config)
    state = search.initial()
    commits = {name: commit_of(f"attempt:{component(name)}:1") for name in _names()}

    def outcome(name: str, proposal: Proposal | None) -> CandidateOutcome:
        child = world.child(seed_world, name)
        if not child.judge_passes:
            return CandidateOutcome(
                passed=False,
                parent_id=None if proposal is None else proposal.parent.id,
                inspiration_ids=()
                if proposal is None
                else tuple(item.id for item in proposal.inspirations),
                summary=child.summary,
                feedback=f"fails {name}",
                commit=commits[name] if proposal is None else None,
                policy_parent_id=None if proposal is None else proposal.policy_parent_id,
                target_island=None if proposal is None else proposal.target_island,
            )
        return CandidateOutcome(
            passed=True,
            parent_id=None if proposal is None else proposal.parent.id,
            inspiration_ids=()
            if proposal is None
            else tuple(item.id for item in proposal.inspirations),
            summary=child.summary,
            commit=commits[name],
            perf_metric=child.throughput,
            perf_unit="tokens/s",
            metrics=(
                ("cache_hit", child.cache_hit),
                ("throughput", child.throughput),
                ("ttft_ms", child.ttft_ms),
            ),
            policy_parent_id=None if proposal is None else proposal.policy_parent_id,
            target_island=None if proposal is None else proposal.target_island,
            code=f"patch for {commits[name]}",
        )

    for name in world.bootstrap_names(seed_world, ATTEMPTS):
        _, state = search.admit(state, outcome(name, None))
    for generation in range(1, GENERATIONS + 1):
        state = search.end_generation(state)
        start = state
        proposals = []
        walk = start
        for _ in range(CHILDREN):
            proposal, walk = search.propose(walk)
            proposals.append(proposal)
        for slot, proposal in enumerate(proposals, start=1):
            if proposal is not None:
                _, state = search.admit(
                    state, outcome(world.child_name(generation, slot), proposal)
                )
    return state


@pytest.mark.parametrize("selector", ["vibesys", "openevolve"])
def test_e3_both_selectors_propose_what_the_legacy_use_of_the_policy_proposes(
    selector: Literal["vibesys", "openevolve"],
) -> None:
    seed_world, seed_rng = 2, 7
    key = "world=2,seed=7,objectives=one"
    expected = _reference_population(seed_world, seed_rng, _space(key), selector)
    run = run_to_end(_scripted(key, parallelism=2, delays=(0, 3, 1), selector=selector))
    assert isinstance(run.state, EvolveStrategyState)
    assert run.state.population == expected
