"""How a finished child becomes an individual, and how a generation's parents are chosen.

The population policy itself (`PopulationSearch`) has its own tests under `population/`; these
cover the search's binding of it: what is recorded for each way a child can end.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve._values import (
    CONFIG,
    finished_children,
    passing,
    reading,
    revision,
    settlement,
    start,
)

from vibesys.metrics import MetricRow, MetricSpace
from vibesys.orchestration.evolve.population import CandidateOutcome
from vibesys.orchestration.evolve.search.api import (
    STOPPED,
    ChildDone,
    Closing,
    FailureKind,
    ProfileReading,
    earns_individual,
    fitness,
    keeps_commit,
    outcome_of,
    proposals,
    search_of,
)
from vs_core.api import EvidenceKind
from vs_sim.api.testing import examples

SPACE = MetricSpace()


@settings(max_examples=examples(pr=8, nightly=100))
@given(done=finished_children())
def test_a_child_earns_an_individual_exactly_when_it_was_measured_and_not_stopped(
    done: ChildDone,
) -> None:
    cause = done.closing.cause
    expected = (
        done.refused is None
        and done.settlement is not None
        and (cause is None or cause.kind not in (FailureKind.STOPPED, FailureKind.UNMEASURED))
    )
    assert earns_individual(done) is expected
    assert (outcome_of(done) is not None) is expected


@settings(max_examples=examples(pr=8, nightly=100))
@given(done=finished_children())
def test_an_individual_records_its_revision_only_when_it_passed_or_is_a_cold_starts_work(
    done: ChildDone,
) -> None:
    outcome = outcome_of(done)
    if outcome is None:
        return
    retained = None if done.settlement is None else done.settlement.candidate
    wants = retained is not None and keeps_commit(done)
    assert (outcome.commit is not None) is wants
    assert keeps_commit(done) is (done.passed or done.start.cold_start)
    if outcome.commit is not None:
        assert retained is not None
        assert outcome.commit == retained.revision_id.root
    assert outcome.passed is done.passed
    if not done.passed:
        assert (outcome.perf_metric, outcome.metrics, outcome.code) == (None, (), None)


@settings(max_examples=examples(pr=8, nightly=100))
@given(done=finished_children())
def test_a_passed_individual_needs_the_candidate_to_pass_everything_and_core_to_accept_it(
    done: ChildDone,
) -> None:
    settled = done.settlement
    assert done.passed is (
        done.closing.passed
        and settled is not None
        and settled.eligible
        and done.closing.cause is None
    )


def test_a_passing_child_records_the_trusted_headline_its_parent_and_its_patch() -> None:
    done = passing("g1.c1", "c2", 150.0, cold=False, parent=3)
    done = ChildDone(
        done.start,
        done.closing.model_copy(update={"patch": "diff", "summary": "tweak"}),
        done.settlement,
    )
    outcome = outcome_of(done)
    assert outcome == CandidateOutcome(
        passed=True,
        parent_id=3,
        summary="tweak",
        feedback="",
        commit="c2",
        perf_metric=150.0,
        perf_unit="tokens/s",
        metrics=(("throughput", 150.0),),
        code="diff",
    )


def test_a_stopped_child_earns_nothing() -> None:
    done = ChildDone(start(), Closing().ended_by(STOPPED), settlement(None, eligible=False))
    assert outcome_of(done) is None


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    benchmark=st.floats(1, 1e6),
    perf=st.floats(1, 1e6),
    extra=st.dictionaries(st.sampled_from(["throughput", "cache_hit", "ttft"]), st.floats(0, 100)),
)
def test_fitness_is_the_trusted_row_merged_over_the_profilers(
    benchmark: float, perf: float, extra: dict[str, float]
) -> None:
    profile = ProfileReading(perf_metric=perf, perf_unit="x", metrics=tuple(extra.items()))
    trusted = reading(EvidenceKind.BENCHMARK, value=benchmark)
    metric, unit, metrics = fitness(profile, trusted)
    assert (metric, unit) == (benchmark, "tokens/s")
    assert dict(metrics) == {**extra, "throughput": benchmark}
    assert fitness(profile, None) == (perf, "x", tuple(extra.items()))
    assert fitness(None, trusted)[0] == benchmark
    assert fitness(None, None) == (None, None, ())


def test_a_benchmark_without_a_headline_leaves_the_profilers_self_report() -> None:
    profile = ProfileReading(perf_metric=3.0, perf_unit="x")
    assert fitness(profile, reading(EvidenceKind.BENCHMARK)) == (3.0, "x", ())


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    values=st.lists(st.floats(1, 1000), min_size=1, max_size=4),
    children=st.integers(1, 5),
)
def test_a_generations_proposals_are_a_pure_function_of_the_population_it_began_with(
    values: list[float], children: int
) -> None:
    search = search_of(CONFIG, SPACE)
    state = search.initial()
    for index, value in enumerate(values):
        outcome = outcome_of(passing(f"bootstrap.{index + 1}", f"c{index}", value))
        assert outcome is not None
        _, state = search.admit(state, outcome)
    first = proposals(search, state, children)
    assert first == proposals(search_of(CONFIG, SPACE), state, children)
    assert len(first) == children
    known = {item.id for item in state.individuals}
    assert all(item is not None and item.parent_id in known for item in first)


def test_a_population_without_a_passing_parent_proposes_nothing() -> None:
    search = search_of(CONFIG, SPACE)
    assert proposals(search, search.initial(), 3) == (None, None, None)
    assert revision("a") != revision("b")


@settings(max_examples=examples(pr=8, nightly=100))
@given(unit=st.none() | st.sampled_from(("tokens/s", "calls/s")), value=st.floats(1, 1e6))
def test_a_trusted_headline_is_labeled_by_its_unit_or_else_by_its_metric_name(
    unit: str | None, value: float
) -> None:
    """The profiler's own unit describes the profiler's number, which the headline replaces."""
    benchmark = reading(EvidenceKind.BENCHMARK, value=value).model_copy(
        update={"metrics": (MetricRow(name="throughput", value=value, direction="max", unit=unit),)}
    )
    profile = ProfileReading(perf_metric=1.0, perf_unit="profiler-unit")

    assert fitness(profile, benchmark)[1] == (unit or "throughput")
    assert fitness(None, benchmark)[1] == (unit or "throughput")
