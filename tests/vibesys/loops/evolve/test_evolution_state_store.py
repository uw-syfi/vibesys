"""Filesystem contract tests for evolutionary population state."""

from pathlib import Path
from unittest.mock import MagicMock

from tests.support.run_execution import run_execution_record

from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.loops.evolve.population import Individual, Population
from vibesys.loops.evolve.state import EvolutionStateStore
from vibesys.run import RunState, RunStateNamespace
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord


def _store(tmp_path: Path) -> EvolutionStateStore:
    project = Project.open(tmp_path)
    project.state.create_project("test")
    run = project.state.new_run_manifest(
        "test",
        run_id="run-1",
        branch="vibesys/run-1",
        vibesys_version="test",
        trusted_input_baseline="a" * 40,
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="evolve", config_version=1, options={}),
    )
    project.state.create_run(run)
    state = RunState(
        project,
        git=MagicMock(history_root=project.root, run_id=run.run_id),
        run_id=run.run_id,
    )
    return EvolutionStateStore(state.portable(RunStateNamespace.EVOLVE))


def test_evolution_state_store_distinguishes_empty_from_persisted(tmp_path: Path) -> None:
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


def test_metric_space_defaults_to_strict_before_a_run_records_one(tmp_path: Path) -> None:
    """State written before the space was persisted has no document.

    It loads as the empty strict space, which is exactly how those runs already
    compared, so a resumed pre-change run selects the way it always did.
    """
    assert _store(tmp_path).load_metric_space() == MetricSpace()


def test_metric_space_round_trips_through_its_own_document(tmp_path: Path) -> None:
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
