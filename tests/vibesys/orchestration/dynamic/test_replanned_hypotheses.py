"""Whatever IDs the planner reuses, every record the search publishes projects to a sound tree.

The planner chooses hypothesis IDs. It may go on with one it already worked on, re-plan one as
new, name one that is running, or name one it abandoned; the plan check accepts the first and
asks for a correction of the rest. Whatever it does, each record the search publishes must
project to an iteration tree (one node per scheduled workstream, in schedule order) instead of
raising, because the run projects every record it commits.
"""

from __future__ import annotations

from hypothesis import given, note, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import Host, begin, config
from tests.vibesys.search_faults import MAX_STEPS, Step, Stop, play, steps, stops
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.core_policy.api import project_run_snapshot
from vibesys.run.contracts import IterationKind, IterationTree
from vs_sim.api.testing import examples

_IMPLEMENTATION_FAILED = 2
"""The implementer variant that ends a round as `implementation_failed`, which may go on."""

_GOING_ON = (0, 1, 20, 26, 10, 5)
"""Host variants (one number answers any role) that leave rounds a hypothesis may go on from:
0 plans a portfolio, implements to `supported` and passes review; 1 plans one entry, blocks and
fails review; 20 and 26 end the implementation as `implementation_failed`; 10 times the
implementer out; 5 declines the implementer's checkpoint."""


def _stories() -> st.SearchStrategy[list[Step]]:
    """The generic fault stories, or ordinary stories biased toward rounds that may go on."""
    going_on = st.lists(
        st.builds(Step, st.integers(0, 7), st.sampled_from(_GOING_ON), st.none()),
        min_size=20,
        max_size=MAX_STEPS,
    )
    return st.one_of(steps(), going_on)


def _check_tree(host: Host) -> None:
    """The record projects, with one workstream node per scheduled workstream, in order."""
    record = host.record
    if record is None:
        return
    tree = project_run_snapshot(record, experiment_revision=0).iterations
    assert isinstance(tree, IterationTree)
    roots = [node for node in tree.nodes if node.parent_id is None]
    scheduled = sorted(item.sequence for item in (*record.finished, *record.running))
    assert [node.ordinal for node in roots] == scheduled, host.run.timeline()
    assert all(node.kind is IterationKind.WORKSTREAM for node in roots)


def test_a_hypothesis_that_goes_on_runs_as_a_second_workstream_of_the_tree() -> None:
    """Plan ``h1``, fail its implementation, go on with ``h1``: the tree shows both workstreams."""
    host = begin(config(max_rounds=2), restarting=False, replans=[0, 1])
    for _ in range(200):
        answerable = [item for item in host.run.open if item.until is not None]
        if not answerable:
            break
        asked = answerable[0]
        host.answer(asked, _IMPLEMENTATION_FAILED if asked.label == "implementer" else 0)
        _check_tree(host)
    record = host.record
    assert record is not None
    assert [item.plan.work_id for item in record.finished] == ["h1", "h1"], host.run.timeline()
    assert [item.plan.continue_hypothesis for item in record.finished] == [False, True]
    tree = project_run_snapshot(record, experiment_revision=0).iterations
    assert isinstance(tree, IterationTree)
    roots = [node for node in tree.nodes if node.parent_id is None]
    assert len({node.id for node in roots}) == len(roots) == len(record.finished)


@across_restarts
@settings(max_examples=examples(pr=10, nightly=80), deadline=None, derandomize=True, database=None)
@given(
    story=_stories(),
    stop=stops(),
    planner=st.tuples(
        st.lists(st.integers(0, 7), min_size=4, max_size=12),
        st.lists(st.integers(0, 4), max_size=6),
    ),
    width=st.tuples(st.integers(2, 5), st.integers(1, 2)),
)
def test_any_reuse_of_hypothesis_ids_keeps_every_published_record_projectable(
    *,
    restarting: bool,
    story: list[Step],
    stop: Stop | None,
    planner: tuple[list[int], list[int]],
    width: tuple[int, int],
) -> None:
    rounds, in_flight = width
    replans, abandons = planner
    host = begin(
        config(max_rounds=rounds, max_in_flight=in_flight),
        restarting=restarting,
        replans=replans,
        abandons=abandons,
    )
    try:
        play(host, story, stop, check=lambda: _check_tree(host))
    finally:
        note(host.run.timeline())
        note(f"planner calls: {host.planner_calls}, named: {host.named}")
