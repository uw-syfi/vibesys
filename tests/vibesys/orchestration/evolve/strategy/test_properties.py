"""Properties of the evolve strategy beyond the shared conformance suite (design note 9.6)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve.strategy.world import (
    EvolveWorld,
    evolve_config,
    mutated,
    profiled,
    reviewed,
    run_to_end,
)

from vibesys.orchestration.evolve.strategy.api import EvolveStrategyState
from vs_core.api import (
    DispatchTurn,
    Measure,
    ProposeWinner,
    RequestTurn,
    RevisionRef,
    Settle,
    StartAttempt,
    Stop,
    Withdraw,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_dst.api import SimulatedRun

RUNS = settings(max_examples=3, deadline=None, derandomize=True, database=None)


def _state(trace: SimulatedRun) -> EvolveStrategyState:
    assert isinstance(trace.state, EvolveStrategyState)
    return trace.state


# Candidates whose commits rank differently, so selection has something to choose between.
_THROUGHPUTS = tuple(float(10 + 5 * n) for n in range(17))
_PROFILER: dict[str, Any] = {
    "profiler_id": "linux_cpu",
    "profiler_support_name": "linux_cpu_profiler",
    "profiler_mcp_name": "vibesys-linux-cpu-profiler",
}


def _passing_world(settings: Mapping[str, Any], passes: tuple[bool, ...] = (True,)) -> EvolveWorld:
    return EvolveWorld(throughputs=_THROUGHPUTS, passes=passes, **settings)


@RUNS
@given(
    passes=st.lists(st.booleans(), min_size=1, max_size=4).map(tuple),
    parallelism=st.integers(1, 3),
    children=st.integers(1, 3),
)
def test_e4_individual_ids_are_unique_and_dense(
    passes: tuple[bool, ...], parallelism: int, children: int
) -> None:
    run = run_to_end(
        _passing_world(
            {
                "max_parallelism": parallelism,
                "children_per_generation": children,
                "bootstrap_max_attempts": 3,
            },
            passes=(True, *passes),
        )
    )
    population = _state(run).population
    assert population is not None
    assert [item.id for item in population.individuals] == list(
        range(1, len(population.individuals) + 1)
    )
    assert population.next_id == len(population.individuals) + 1


def test_e6_a_cold_start_builds_on_the_previous_attempts_work_in_progress() -> None:
    world = EvolveWorld(
        mutations=[mutated(f"seed {n}") for n in range(3)],
        # The first two cold starts are rejected, the third passes.
        reviews=[reviewed(passed=False), reviewed(passed=False), reviewed(passed=True)]
        + [reviewed()] * 8,
        bootstrap_max_attempts=3,
        max_generations=1,
        children_per_generation=1,
    )
    run = run_to_end(world)
    starts = [item for item in run.decisions if isinstance(item, StartAttempt)][:3]
    settles = [
        item.disposition
        for item in run.decisions
        if isinstance(item, Withdraw) and isinstance(item.disposition, Settle)
    ][:2]
    assert [item.retention for item in settles] == ["wip", "wip"]
    assert starts[1].workspace.base == settles[0].candidate
    assert starts[2].workspace.base == settles[1].candidate


def test_e6_a_cold_start_that_never_passes_ends_without_a_selection() -> None:
    world = EvolveWorld(
        mutations=[mutated() for _ in range(2)],
        reviews=[reviewed(passed=False) for _ in range(2)],
        bootstrap_max_attempts=2,
    )
    run = run_to_end(world)
    final = run.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "failure"
    assert not any(isinstance(item, ProposeWinner) for item in run.decisions)
    assert sum(isinstance(item, StartAttempt) for item in run.decisions) == 2


def test_a_run_without_a_profiler_asks_no_profiler_turn() -> None:
    run = run_to_end(_passing_world({"max_generations": 1}))
    roles = {
        item.turn.session.role_id.root for item in run.decisions if isinstance(item, RequestTurn)
    }
    assert roles == {"evolve-mutator", "evolve-judge"}


def test_a_profiler_turn_is_asked_once_per_passing_child_when_the_run_has_one() -> None:
    run = run_to_end(
        _passing_world({"max_generations": 1, "children_per_generation": 2, **_PROFILER})
    )
    profiles = [
        item
        for item in run.decisions
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root == "evolve-profiler"
    ]
    assert len(profiles) == 3  # the cold start and two children


def test_m2_a_failing_profiler_never_fails_the_child() -> None:
    world = EvolveWorld(
        mutations=[mutated() for _ in range(2)],
        reviews=[reviewed() for _ in range(2)],
        # An unparsable reply and its unparsable correction, for each of the two children.
        profiles=['{"analysis": 1}'] * 4,
        max_generations=1,
        children_per_generation=1,
        **_PROFILER,
    )
    run = run_to_end(world)
    state = _state(run)
    assert state.population is not None
    assert all(item.passed for item in state.population.individuals)
    assert any(note.startswith("profiler:") for note in state.notes)
    final = run.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "success"


def test_a_failed_review_skips_the_trusted_gates_and_keeps_the_feedback() -> None:
    world = EvolveWorld(
        mutations=[mutated() for _ in range(3)],
        reviews=[reviewed(), reviewed(passed=False, feedback="tests fail"), reviewed()],
        max_generations=1,
        children_per_generation=2,
    )
    run = run_to_end(world)
    state = _state(run)
    assert state.population is not None
    failed = [item for item in state.population.individuals if not item.passed]
    assert [item.feedback for item in failed] == ["tests fail"]
    measured = {
        item.plan.candidate.revision_id.root
        for item in run.decisions
        if isinstance(item, Measure) and isinstance(item.plan.candidate, RevisionRef)
    }
    assert len(measured) == 2  # the failed child was never measured


def test_a_candidate_the_machinery_could_not_measure_earns_no_individual() -> None:
    # Every official measurement ends without evidence, so no cold start can be admitted.
    world = EvolveWorld(
        mutations=[mutated() for _ in range(4)],
        reviews=[reviewed() for _ in range(4)],
        unmeasurable=True,
        bootstrap_max_attempts=2,
        max_generations=1,
    )
    run = run_to_end(world)
    state = _state(run)
    assert state.population is not None
    assert state.population.individuals == ()
    assert state.bootstrap_spent == 2
    final = run.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "failure"


def test_the_state_grows_linearly_in_individuals() -> None:
    sizes = []
    for generations in (1, 2, 3):
        run = run_to_end(
            _passing_world({"max_generations": generations, "children_per_generation": 2})
        )
        population = _state(run).population
        assert population is not None
        sizes.append(
            (len(population.individuals), len(json.dumps(population.model_dump(mode="json"))))
        )
    (n1, s1), (n2, s2), (n3, s3) = sizes
    per_individual = (s2 - s1) / (n2 - n1)
    assert abs((s3 - s2) / (n3 - n2) - per_individual) < 0.2 * per_individual


def test_a_dispatch_is_never_repeated_for_one_invocation() -> None:
    run = run_to_end(_passing_world({"max_generations": 2}))
    dispatched = [
        item.turn.invocation_id.root for item in run.decisions if isinstance(item, RequestTurn)
    ]
    assert len(dispatched) == len(set(dispatched))
    assert DispatchTurn is not None
    assert evolve_config().total_attempts >= 1


@RUNS
@given(
    unit=st.none() | st.sampled_from(("tokens/s", "calls/s")),
    profiler_replies=st.sampled_from(("unparsable", "no_metric")),
)
def test_every_trusted_individual_records_a_unit_label_whatever_the_profiler_says(
    unit: str | None, profiler_replies: str
) -> None:
    """A benchmark headline without a declared unit is labeled by its metric name.

    The profiler's own self-report cannot supply the label: it fails, or reports no number.
    """
    world = EvolveWorld(
        mutations=[mutated() for _ in range(2)],
        reviews=[reviewed() for _ in range(2)],
        profiles=['{"analysis": 1}'] * 4 if profiler_replies == "unparsable" else [profiled()] * 4,
        unit=unit,
        max_generations=1,
        children_per_generation=1,
        **_PROFILER,
    )
    state = _state(run_to_end(world))
    assert state.population is not None
    measured = [item for item in state.population.individuals if item.perf_metric is not None]
    assert measured
    assert all(item.perf_unit == (unit or "throughput") for item in measured)
