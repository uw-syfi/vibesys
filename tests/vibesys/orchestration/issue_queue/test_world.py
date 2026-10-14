"""Whole issue-queue runs on the production shell, simulated by vs-dst: the end result only.

The flow stories (`test_stories.py`) pin each decision, including the adoption a final accuracy
gate refuses; these runs check that the same search, through the real core and shell, ends with
what the story says: one smoke run, and a tracker that fails around its writes. (A host that
dies is `tests/system/crash`'s.)

Budget: each run is a whole run of the search through core and shell (about 1 CPU-s).
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.issue_queue._replies import evaluated, implemented, judged
from tests.vibesys.orchestration.issue_queue.world import IssueQueueWorld

from vs_core.api import Finish
from vs_dst.api import SimulatedRun, simulate
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_issue_tracker.api import FaultPhase


def finish(run: SimulatedRun) -> Finish:
    """The run's conclusion, once it ended cleanly."""
    assert run.finished
    assert not run.liveness_violations
    final = run.decisions[-1]
    assert isinstance(final, Finish)
    return final


def clean_world(*, tracker_faults: Mapping[int, FaultPhase] | None = None) -> IssueQueueWorld:
    """One issue whose first attempt the judge rejects, and the second passes."""
    return IssueQueueWorld(
        implementer=[implemented(f"turn {n}") for n in range(2)],
        judge=[judged(passed=False), judged()],
        perf=[evaluated()],
        tracker_faults=tracker_faults,
    )


@cache
def normal() -> SimulatedRun:
    """The fault-free reference run."""
    run = simulate(clean_world())
    assert run.finished
    return run


def board_of(run: SimulatedRun) -> list[tuple[object, ...]]:
    """The issues of a finished run, with their statuses, attempts and recorded actions."""
    world = run.world
    assert isinstance(world, IssueQueueWorld)
    return world.history()


def test_a_closed_issue_is_adopted_and_the_run_succeeds() -> None:
    """The smoke run: a judge rejection, a pass, one issue closed, its tip adopted."""
    run = normal()

    final = finish(run)
    assert final.result.outcome == "success"
    assert final.result.selection is not None
    assert final.result.selection.kind == "retained_candidate"
    assert board_of(run) == [*board_of(run)]
    world = run.world
    assert isinstance(world, IssueQueueWorld)
    assert world.board() == {1: ("closed", 2)}


@example(failing={0: "before"})
@example(failing={3: "after", 7: "before"})
@settings(max_examples=examples(pr=1, nightly=24), deadline=None, derandomize=True, database=None)
@given(
    failing=st.dictionaries(
        st.integers(min_value=0, max_value=14), st.sampled_from(["before", "after"]), max_size=3
    )
)
def test_a_tracker_failing_around_any_write_leaves_the_board_of_a_clean_run(
    failing: dict[int, FaultPhase],
) -> None:
    run = simulate(clean_world(tracker_faults=failing))

    assert run.finished
    assert run.halted is None
    assert not run.liveness_violations
    assert run.result == normal().result
    assert board_of(run) == board_of(normal())
