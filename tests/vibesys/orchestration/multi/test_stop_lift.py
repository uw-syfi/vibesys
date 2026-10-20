"""A multi run the operator stopped goes on, with its rounds and its best, once a resume lifts the stop.

The stop reaches the flow while it waits for an agent. Without a lift it ends the run after the
work in flight. When an explicit resume lifts it before the search concluded, the search is a
search that was never stopped as far as what it has still to do: the rounds it already had stay in
its record, it asks for the rounds that remain, and it adopts the best of them all. Work the stop
had already ended (a turn whose task took ``Stopped``) stays ended. Every story runs live and
restarted at a few steps.
"""

from __future__ import annotations

import pytest
from tests.vibesys.orchestration.multi._host import IMPLEMENTER, JUDGE, PLANNER, MultiHost, config
from tests.vibesys.orchestration.multi.test_stories import design, implement, judge, start
from tests.vibesys.vibesys_flows import across_restarts

from vs_core.api import Finish

MODES = across_restarts
_WAITS = (PLANNER, IMPLEMENTER, JUDGE)
_SETTLES = (IMPLEMENTER, JUDGE)
"""The waits at which the stop leaves work to settle: the round holds a workspace."""


def _to_the_second_round_wait(host: MultiHost, role: str) -> None:
    """Finish round one (passed), then open round two up to a wait of ``role``."""
    design(host, "h1")
    implement(host, candidate="def456")
    judge(host, passed=True)
    host.drain_until_turn(PLANNER)
    if role == PLANNER:
        return
    design(host, "h2")
    implement(host, candidate="abc123") if role == JUDGE else host.drain_until_turn(IMPLEMENTER)
    if role == JUDGE:
        host.drain_until_turn(JUDGE)


def _finish_the_second_round(host: MultiHost, role: str) -> None:
    if role == PLANNER:
        design(host, "h2")
        implement(host, candidate="abc123")
        judge(host, passed=True)
    elif role == IMPLEMENTER:
        host.drain_until_turn(IMPLEMENTER)
        implement(host, candidate="abc123")
        judge(host, passed=True)
    else:
        judge(host, passed=True)
    host.drain()


@pytest.mark.parametrize("role", _SETTLES)
@MODES
def test_a_stop_a_resume_lifted_leaves_the_run_going_with_its_rounds(
    role: str, *, restarting: bool
) -> None:
    host = start(config(max_rounds=2), restarting=restarting)
    _to_the_second_round_wait(host, role)
    host.run.stop()
    assert host.run.finished is None  # the round settles its workspace before the search ends
    host.run.lift_stop()
    assert host.run.finished is None
    _finish_the_second_round(host, role)
    record = host.record
    assert record is not None
    # the round before the stop is still in the record, and the rounds after it follow
    assert [item.rounds[0].passed for item in record.hypotheses] == [True, True]
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome != "cancelled", finish.result


@pytest.mark.parametrize("role", _WAITS)
@MODES
def test_the_same_stop_without_a_lift_ends_the_run(role: str, *, restarting: bool) -> None:
    host = start(config(max_rounds=2), restarting=restarting)
    _to_the_second_round_wait(host, role)
    host.run.stop()
    host.drain()
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "cancelled", finish.result


@pytest.mark.parametrize("role", _WAITS)
@MODES
def test_a_stop_a_resume_lifts_after_the_search_ended_the_run_goes_on_with_its_rounds(
    role: str, *, restarting: bool
) -> None:
    """Regression: a stop whose ending committed was final, so its rounds and best were lost.

    The search ends the run under the stop (adopts what led and finishes). Core reopens the run
    for the resume that lifts the stop, and the search starts again from its checkpoint: it
    plays the rounds that remain and adopts the best of them all, as a search never stopped does.
    """
    host = start(config(max_rounds=2), restarting=restarting)
    _to_the_second_round_wait(host, role)
    host.run.stop()
    host.drain()
    assert isinstance(host.run.finished, Finish)
    host.run.lift_stop()
    if role == PLANNER:
        host.drain_until_turn(PLANNER)  # the round the stop cut short starts again
    _finish_the_second_round(host, role)
    record = host.record
    assert record is not None
    assert [item.rounds[0].passed for item in record.hypotheses] == [True, True]
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome != "cancelled", finish.result
