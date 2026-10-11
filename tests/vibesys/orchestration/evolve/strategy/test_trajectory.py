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

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve import world
from tests.vibesys.orchestration.evolve.strategy._executors import Executors, commit_of
from tests.vibesys.orchestration.evolve.strategy._replies import mutated, profiled, reviewed
from tests.vibesys.orchestration.evolve.strategy._run import run_shell

from vibesys.metrics import MetricSpace, Objective
from vibesys.orchestration.evolve.strategy.api import EvolveStrategyState, MetricRow
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


def _commits(seed: int) -> dict[str, world.Child]:
    """The world's children by the commit the scripted workspace retains for them."""
    return {
        commit_of(f"attempt:{component(name)}:1"): world.child(seed, name) for name in _names()
    }


def scripted(seed: int, *, delays: tuple[int, ...] = (0,)) -> Executors:
    """The executors of the world with this seed; ``delays`` reorders when jobs complete."""
    children = _commits(seed)

    def replies(role: str, key: str, serial: int) -> str:
        del serial
        child = world.child(seed, key)
        if role == "mutator":
            return mutated(child.summary)
        if role == "judge":
            return reviewed(passed=child.judge_passes, feedback="" if child.judge_passes else f"fails {key}")
        return profiled(metrics={"cache_hit": child.cache_hit})

    return Executors(
        replies=replies,
        benchmark=lambda commit: children[commit].throughput,
        extra_metrics=lambda commit: (
            MetricRow(name="ttft_ms", value=children[commit].ttft_ms, direction="min"),
        ),
        polls=lambda commit: delays[int(commit, 16) % len(delays)],
    )


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


def _run(key: str, *, parallelism: int, delays: tuple[int, ...]) -> list[dict[str, object]]:
    seed_world, seed_rng, _ = _expected(key)
    trace = run_shell(
        scripted(seed_world, delays=delays),
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
    )
    return _population(trace.state)


@pytest.mark.parametrize("key", sorted(GOLDEN["runs"])[::2])
def test_the_serial_run_reproduces_the_legacy_population(key: str) -> None:
    _, _, expected = _expected(key)
    assert _run(key, parallelism=1, delays=(0,)) == expected


@pytest.mark.parametrize("key", sorted(GOLDEN["runs"])[1:4:2])
def test_concurrent_children_reproduce_the_legacy_population(key: str) -> None:
    _, _, expected = _expected(key)
    assert _run(key, parallelism=3, delays=(0, 2, 1, 3, 0)) == expected


@settings(
    max_examples=3,
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
