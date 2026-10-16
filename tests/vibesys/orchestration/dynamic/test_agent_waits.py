"""What the dynamic search chooses about agents that wait for their own evaluations or are lost.

Which roles may end a turn waiting for an evaluation, which are asked again after a lost turn,
and that the planning flow asks a lost planner again itself, are the dynamic search's. What core
does between (record the wait, suspend and resume the turn, hold a write turn's reply for its
checkpoint, ask again after a backoff) is core's own: `test_agent_turn_runs.py`,
`test_role_governs_session.py` in `libs/vs-core/tests`.
"""

from __future__ import annotations

from tests.vibesys.orchestration.dynamic._host import PLANNER_LABEL, Host, begin, config
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.search.api import ROLES, dynamic
from vs_core.api import Capabilities, LifecycleCapability, RequestTurn
from vs_core.api.testing import FakeView

MODES = across_restarts
LIFECYCLE: frozenset[LifecycleCapability] = frozenset({"suspend", "profile-capture"})
WAITERS = ("implementer", "judge")


def offering() -> FakeView:
    """A host that can suspend a turn that waits and capture profiles."""
    return FakeView(changes={"capabilities": Capabilities(lifecycle=LIFECYCLE)})


def start(*, restarting: bool, **settings: object) -> Host:
    host = begin(config(**settings), restarting=restarting, view=offering())
    host.drain_until_turn(PLANNER_LABEL)
    return host


LOST_WAITS = (5.0, 10.0)
"""The run-clock waits before the first and second re-ask: ``Backoff(first=5, cap=120)`` doubling."""


def lose_and_wait(host: Host, role: str) -> float:
    """Lose the open turn of ``role``; return how long the run clock waits to ask it again."""
    before = host.run.view.now_at
    host.run.lose(host.run.expect_turn(role=role))
    until = min(host.run.sleeping_until)  # the others are limits of turns still open
    host.run.advance_to(until)
    return until - before


def test_the_roles_that_work_on_a_candidate_are_asked_again_after_a_lost_turn() -> None:
    """How often and after how long is core's; who is asked again is ours.

    The planner is not a role core asks again: the planning flow does it itself, which
    `test_a_lost_planner_turn_is_asked_again_after_five_then_ten_seconds` pins. The outliner,
    asked once for a plan's steps, leaves it to core.
    """
    again = {role.name for role in ROLES if role.lost_turns > 0}
    assert again == {"implementer", "judge", "profiler", "merger", "outliner"}


@MODES
def test_a_lost_planner_turn_is_asked_again_after_five_then_ten_seconds(
    *, restarting: bool
) -> None:
    host = start(restarting=restarting)

    waits = [lose_and_wait(host, PLANNER_LABEL) for _ in LOST_WAITS]

    assert waits == list(LOST_WAITS)
    assert host.open(RequestTurn, label=PLANNER_LABEL)


def test_only_the_roles_that_wait_for_evaluations_may_end_a_turn_waiting() -> None:
    assert {role.name for role in ROLES if role.may_wait_for_evaluations} == set(WAITERS)


# -- what the host must offer ----------------------------------------------------------------------


def test_the_search_offers_suspension_and_profile_capture_as_optional_and_requires_nothing() -> (
    None
):
    declaration = dynamic.declaration

    assert {"suspend", "profile-capture"} <= declaration.optional
    assert not declaration.required
