"""Whatever core answers and whenever the operator stops, the evolve root keeps its promises.

A generated run is a list of steps (which open wait core answers, and how) and, maybe, a stop
before one of them. The fake core never lies; the flow must never exceed its parallelism or its
attempt budget, never admit out of order, never start work after the stop, and end in exactly one
`Finish` with every attempt settled and the adopted candidate the best passing individual.
A failing example prints the run as a timeline.
"""

from __future__ import annotations

from hypothesis import given, note, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve._host import Host, begin, config
from tests.vibesys.search_faults import Step, Stop, play, steps, stops
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.evolve.core_policy.api import project_snapshot
from vibesys.run.contracts import IterationTree
from vs_core.api import Finish
from vs_sim.api.testing import examples

MODES = across_restarts


def _check_always(host: Host) -> None:
    """What holds after every step."""
    rules, record = host.config, host.record
    where = host.run.timeline()
    assert len(host.unsettled) <= rules.max_parallelism, f"too many attempts open: {where}"
    assert len(host.opened) <= rules.total_attempts, f"more starts than the budget: {where}"
    if record is not None:
        ids = [item.id for item in record.population.individuals]
        assert ids == list(range(1, len(ids) + 1)), f"individuals out of order: {where}"
        assert record.population.generation <= rules.max_generations, (
            f"too many generations: {where}"
        )
        # Every published record projects to a sound iteration tree: the run projects each one.
        tree = project_snapshot(record, experiment_revision=0).iterations
        assert isinstance(tree, IterationTree), f"unprojectable record: {where}"


def _check_at_the_end(host: Host) -> None:
    """What holds of a finished run."""
    where = host.run.timeline()
    record = host.record
    assert record is not None
    finish = host.run.finished
    assert isinstance(finish, Finish)
    chosen = finish.result.selection
    passing = [item for item in record.population.individuals if item.passed]
    assert (chosen is not None) == bool(passing), f"a passing individual and no winner: {where}"
    if chosen is None:
        return
    adopted = [item for item in passing if item.commit == chosen.revision.git_commit]
    assert len(adopted) == 1, f"adopted a candidate that is no passing individual: {where}"
    best = max(item.perf_metric or 0.0 for item in passing)
    assert (adopted[0].perf_metric or 0.0) == best, (
        f"adopted a candidate that is not the best: {where}"
    )


@MODES
@settings(max_examples=examples(pr=10, nightly=60), deadline=None, derandomize=True, database=None)
@given(
    story=steps(),
    stop=stops(),
    shape=st.fixed_dictionaries(
        {
            "max_generations": st.integers(1, 2),
            "children_per_generation": st.integers(1, 3),
            "max_parallelism": st.integers(1, 3),
            "search_policy": st.sampled_from(["vibesys", "openevolve"]),
            "profiling": st.booleans(),
        }
    ),
    throughputs=st.dictionaries(
        st.sampled_from([f"c{n}" for n in range(1, 9)]), st.sampled_from([30.0, 80.0, 120.0, 160.0])
    ),
)
def test_the_root_keeps_its_promises_whatever_core_answers_and_whenever_the_operator_stops(
    *,
    restarting: bool,
    story: list[Step],
    stop: Stop | None,
    shape: dict[str, object],
    throughputs: dict[str, float],
) -> None:
    host = begin(config(**shape), restarting=restarting, throughputs=throughputs)
    try:
        play(host, story, stop, check=lambda: _check_always(host))
        _check_at_the_end(host)
    finally:
        note(host.run.timeline())
