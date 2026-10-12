"""A host that dies at any durable boundary is replaced by one that finishes the same run.

The reference run commits some number of times. For a death just before or just after any
commit (just before one is an effect that ran whose observation was not committed: the board
write that landed whose acknowledgement was lost, among them), a new host over the same store
reaches the same result and the same board, with no request executed twice. A second death
during that recovery is recovered too.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.issue_queue.strategy.world import (
    IssueQueueWorld,
    evaluated,
    implemented,
    judged,
)

from vs_dst.api import SimulatedRun, simulate


def _world() -> IssueQueueWorld:
    """Two issues: the bootstrap one fails its first review, and its judge finds a bug."""
    return IssueQueueWorld(
        implementer=[implemented(f"turn {n}") for n in range(6)],
        judge=[judged(passed=False), judged(bugs=("a bug",)), judged(), judged()],
        perf=[evaluated(), evaluated()],
    )


def _board(run: SimulatedRun) -> list[tuple[object, ...]]:
    world = run.world
    assert isinstance(world, IssueQueueWorld)
    return world.history()


NORMAL = simulate(_world())
assert NORMAL.finished


def _recovers_the_run(recovered: SimulatedRun, label: object) -> None:
    assert recovered.finished, label
    assert recovered.halted is None, label
    assert recovered.result == NORMAL.result, label
    assert _board(recovered) == _board(NORMAL), label
    assert recovered.no_effect_ran_twice(), label
    assert {d.decision_id.root for d in recovered.decisions} == {
        d.decision_id.root for d in NORMAL.decisions
    }, label


def test_a_restart_after_any_crash_finishes_the_same_run() -> None:
    for crash in NORMAL.crash_points():
        _recovers_the_run(NORMAL.rerun_with_crash(crash), crash)


@settings(max_examples=8, deadline=None, derandomize=True, database=None)
@given(data=st.data())
def test_a_second_death_during_recovery_is_recovered_too(data: st.DataObject) -> None:
    first = data.draw(st.sampled_from(NORMAL.crash_points()), label="first")
    recovered = NORMAL.rerun_with_crash(first)
    second = data.draw(st.sampled_from(recovered.crash_points()), label="second")
    _recovers_the_run(recovered.rerun_with_crash(second), (first, second))
