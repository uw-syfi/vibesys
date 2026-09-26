"""Persistence contract tests for the evolve loop's durable state."""

from unittest.mock import MagicMock  # test-isolation: git tracker stub below

from tests.support.run_execution import run_execution_record

from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.loops.evolve.state import EvolutionStateStore, EvolveState
from vibesys.run import RunState, RunStateNamespace
from vibesys.search.population.models import CandidateOutcome, PopulationConfig
from vibesys.search.population.search import PopulationSearch
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord


def _store(tmp_path) -> EvolutionStateStore:  # noqa: ANN001  # LW-040156 [ANN001]; this scripted double mirrors a production signature whose parameters are not annotated here.
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
        # test-isolation: the git tracker is stubbed with the two attributes RunState reads
        git=MagicMock(history_root=project.root, run_id=run.run_id),
        run_id=run.run_id,
    )
    return EvolutionStateStore(state.portable(RunStateNamespace.EVOLVE))


def test_load_distinguishes_empty_from_persisted(tmp_path) -> None:  # noqa: ANN001  # LW-040157 [ANN001]; this scripted double mirrors a production signature whose parameters are not annotated here.
    store = _store(tmp_path)
    assert store.load() is None

    search = PopulationSearch(PopulationConfig(seed=1))
    population = search.initial()
    _individual, population = search.admit(
        population, CandidateOutcome(passed=True, parent_id=None, commit="a" * 40, perf_metric=10.0)
    )
    state = EvolveState(population=population)
    store.save(state)

    assert store.load() == state


def test_metric_space_defaults_to_strict_before_a_run_records_one(tmp_path) -> None:  # noqa: ANN001  # LW-040158 [ANN001]; this scripted double mirrors a production signature whose parameters are not annotated here.
    """State written before the space was persisted has no document.

    It loads as the empty strict space, which is exactly how those runs
    already compared, so a resumed pre-change run selects the way it always
    did.
    """
    assert _store(tmp_path).load_metric_space() == MetricSpace()


def test_metric_space_round_trips_through_its_own_document(tmp_path) -> None:  # noqa: ANN001  # LW-040159 [ANN001]; this scripted double mirrors a production signature whose parameters are not annotated here.
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
    # The metric space is a separate document, so recording how a run
    # compares does not rewrite what it has measured.
    assert store.load() is None


def test_projection_exposes_generation_and_metric_space(tmp_path) -> None:  # noqa: ANN001  # LW-040160 [ANN001]; this scripted double mirrors a production signature whose parameters are not annotated here.
    store = _store(tmp_path)
    space = MetricSpace(objectives=(Objective(name="tput", direction="max"),))
    store.save_metric_space(space)
    search = PopulationSearch(PopulationConfig(seed=1, space=space))
    population = search.end_generation(search.initial())
    state = EvolveState(population=population)

    projection = store.projection(state)

    assert projection.generation == 1
    assert projection.metric_space == space
    assert projection.population == population
