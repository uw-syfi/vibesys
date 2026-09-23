"""Filesystem contract tests for evolutionary population state."""

from unittest.mock import MagicMock

from vibesys.loops.evolve.population import Individual, Population
from vibesys.loops.evolve.state import EvolutionStateStore
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.run import RunState, RunStateNamespace
from vs_project.api import EvolveRunConfiguration, Project, RunEnvironmentRecord


def _store(tmp_path) -> EvolutionStateStore:  # noqa: ANN001
    project = Project.open(tmp_path)
    project.state.create_project("test")
    run = project.state.new_run_manifest(
        "test",
        run_id="run-1",
        branch="vibesys/run-1",
        vibesys_version="test",
        trusted_input_baseline="a" * 40,
        configuration=EvolveRunConfiguration(
            outer_loop="evolve",
            run_environment=RunEnvironmentRecord(name="local"),
            agent_backend="stub",
            compute_backend="cpu",
            max_generations=1,
            children_per_generation=1,
            k_top_inspirations=1,
            k_random_inspirations=1,
            selection_temperature=0.5,
            frontier_bias=0.7,
            bootstrap_max_attempts=1,
            keep_deployments=False,
            max_parallelism=1,
        ),
    )
    project.state.create_run(run)
    state = RunState(
        project,
        git=MagicMock(history_root=project.root, run_id=run.run_id),
        run_id=run.run_id,
    )
    return EvolutionStateStore(state.portable(RunStateNamespace.EVOLVE))


def test_evolution_state_store_distinguishes_empty_from_persisted(tmp_path) -> None:  # noqa: ANN001
    store = _store(tmp_path)
    assert store.load_population().all == []

    population = Population(
        [
            Individual(
                id=1,
                generation=0,
                parent_id=None,
                commit="a" * 40,
                perf_metric=10.0,
                perf_unit="ops/s",
                passed=True,
            )
        ]
    )
    store.save_population(population)

    assert store.load_population().all == population.all


def test_metric_space_defaults_to_strict_before_a_run_records_one(tmp_path) -> None:  # noqa: ANN001
    """State written before the space was persisted has no document.

    It loads as the empty strict space, which is exactly how those runs already
    compared, so a resumed pre-change run selects the way it always did.
    """
    assert _store(tmp_path).load_metric_space() == MetricSpace()


def test_metric_space_round_trips_through_its_own_document(tmp_path) -> None:  # noqa: ANN001
    store = _store(tmp_path)
    space = MetricSpace(
        objectives=(
            Objective(name="tput", direction="max"),
            Objective(name="lat_ms", direction="min"),
        ),
        relative_noise=0.05,
    )

    store.save_metric_space(space)

    assert store.load_metric_space() == space
    # The population is a separate document, so recording how a run compares
    # does not rewrite what it has measured.
    assert store.load_population().all == []
