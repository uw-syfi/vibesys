"""The evolve record: which round comes next, what each finished child does to it, who leads."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve._host import TERMS, config
from tests.vibesys.orchestration.evolve._values import (
    BASE,
    failing,
    finished_children,
    passing,
    revision,
)

from vibesys.orchestration.evolve.search.api import ChildDone, EvolveRecord, Stage
from vs_core.api import Kept
from vs_core.api.search import Input
from vs_sim.api.testing import examples


def record(**settings: object) -> EvolveRecord:
    return EvolveRecord.begin(config(**settings), BASE, TERMS)


def test_a_new_run_starts_with_a_cold_start_building_on_the_input() -> None:
    run = record()
    round_ = run.open_round(stopping=False)
    assert round_ is not None
    (first,) = round_.starts
    assert (first.key, first.cold_start, first.base) == ("bootstrap.1", True, BASE)
    assert round_.width == 1
    assert not first.prompt.repair_seed


def test_nothing_starts_once_the_operator_has_stopped() -> None:
    assert record().open_round(stopping=True) is None


def test_cold_starts_are_spent_one_by_one_until_the_budget_is_gone() -> None:
    run = record(bootstrap_max_attempts=3)
    for number in (1, 2, 3):
        round_ = run.open_round(stopping=False)
        assert round_ is not None
        assert round_.starts[0].key == f"bootstrap.{number}"
        run.admit(failing(f"bootstrap.{number}"))
    assert run.open_round(stopping=False) is None
    ending = run.ending()
    assert ending is not None
    assert "3 attempts" in ending.reason


def test_a_failed_cold_starts_work_in_progress_seeds_the_next_one() -> None:
    run = record()
    run.admit(failing("bootstrap.1", commit="c1"))
    round_ = run.open_round(stopping=False)
    assert round_ is not None
    (second,) = round_.starts
    assert (second.base, second.prompt.repair_seed) == (revision("c1"), True)
    assert second.prompt.failed_lessons == ("needs work",)


def test_a_passing_cold_start_opens_the_generations_and_each_names_a_parent() -> None:
    run = record(max_generations=2, children_per_generation=2, max_parallelism=2)
    run.admit(passing("bootstrap.1", "c1", 100.0))
    assert run.stage is Stage.GENERATION
    generations = []
    for _ in range(2):
        round_ = run.open_round(stopping=False)
        assert round_ is not None
        assert round_.width == 2
        assert [s.key.split(".")[1] for s in round_.starts] == ["c1", "c2"]
        assert all(s.base == revision("c1") and s.prompt.parent is not None for s in round_.starts)
        generations.append(round_.starts[0].generation)
        for number, start in enumerate(round_.starts):
            run.admit(passing(start.key, f"g{start.generation}{number}", 90.0, cold=False))
    assert generations == [1, 2]
    assert run.open_round(stopping=False) is None


def test_the_leader_is_the_best_passing_individual_and_the_input_before_any() -> None:
    run = record()
    assert isinstance(run.leader(), Input)
    run.admit(passing("bootstrap.1", "c1", 100.0))
    run.admit(passing("g1.c1", "c2", 150.0, cold=False))
    run.admit(passing("g1.c2", "c3", 120.0, cold=False))
    leader = run.leader()
    assert isinstance(leader, Kept)
    assert leader.revision == revision("c2")
    assert run.ending() is None


@settings(max_examples=examples(pr=8, nightly=100))
@given(children=st.lists(finished_children(), max_size=8))
def test_the_record_keeps_its_invariants_whatever_children_end_with(
    children: list[ChildDone],
) -> None:
    run = record()
    cold = 0
    for done in children:
        run.admit(done)
        cold += done.start.cold_start
    ids = [item.id for item in run.population.individuals]
    assert ids == list(range(1, len(ids) + 1))
    assert run.bootstrap_spent == cold
    if run.stage is Stage.GENERATION:
        assert any(i.passed for i in run.population.individuals)
    assert set(run.kept) <= {item.id for item in run.population.individuals if item.passed}
    for member in run.population.individuals:
        if member.passed:
            assert run.revision_of(member) is not None
    assert (run.ending() is None) == (not isinstance(run.leader(), Input))
