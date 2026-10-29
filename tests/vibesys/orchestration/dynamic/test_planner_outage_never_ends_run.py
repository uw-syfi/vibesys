"""Turns lost to a provider outage never make the planner or the outliner stuck (S179).

On the live run a provider outage longer than the planner's 30 minute ``turn_time`` made core
end each planner turn as lost (``TRANSPORT_LOST``). The planning call asked again twice (its
``lost_turns``), then gave up with ``NoPlan(stuck=True)`` because nothing else ran, and the run
ended with no winner after about 1.5 hours of an outage the capacity gate would have waited out
for six.

Core now ends a turn the capacity gate still held for an outage as ``PROVIDER_UNAVAILABLE``, and
the agents library asks such a turn again without counting it against the role's lost turns,
so however many turns the outage takes, the call goes on once the provider answers.
"""

from __future__ import annotations

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import (
    OUTLINER_LABEL,
    PLANNER_LABEL,
    begin,
    config,
    plan_config,
)

from vs_core.api import Finish, TurnFailureKind
from vs_sim.api.testing import examples

# The planner's ``lost_turns`` is 3: twice that many outage losses would have ended the run.
_ROLE_LOST_TURNS = 3


def _lose_to_the_outage(role: str, losses: int, *, restarting: bool, outline: bool) -> None:
    cfg = plan_config(max_rounds=2) if outline else config(max_in_flight=1, max_rounds=2)
    host = begin(cfg, restarting=restarting)
    host.drain_until_turn(role)
    for _ in range(losses):
        turn = host.run.expect_turn(role=role)
        host.run.advance_to(host.run.view.now_at + 1800.0)  # the turn's limit passed
        host.run.fail_turn(turn, TurnFailureKind.PROVIDER_UNAVAILABLE)
        assert host.run.finished is None, "an outage loss ended the run"
        if host.run.sleeping_until:
            host.run.advance_to(min(host.run.sleeping_until))
    # The provider answers again: the call goes on and the run finishes with a winner.
    host.drain()
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "success", finish.result


@pytest.mark.regression
@pytest.mark.parametrize("restarting", [False, True])
@given(losses=st.integers(min_value=1, max_value=4 * _ROLE_LOST_TURNS))
@example(losses=_ROLE_LOST_TURNS)
@settings(max_examples=examples(pr=4, nightly=20), deadline=None)
def test_planner_turns_lost_to_an_outage_never_end_the_run(
    *, restarting: bool, losses: int
) -> None:
    _lose_to_the_outage(PLANNER_LABEL, losses, restarting=restarting, outline=False)


@pytest.mark.regression
@pytest.mark.parametrize("restarting", [False, True])
@given(losses=st.integers(min_value=1, max_value=4 * _ROLE_LOST_TURNS))
@example(losses=_ROLE_LOST_TURNS)
@settings(max_examples=examples(pr=4, nightly=20), deadline=None)
def test_outliner_turns_lost_to_an_outage_never_end_the_run(
    *, restarting: bool, losses: int
) -> None:
    _lose_to_the_outage(OUTLINER_LABEL, losses, restarting=restarting, outline=True)
