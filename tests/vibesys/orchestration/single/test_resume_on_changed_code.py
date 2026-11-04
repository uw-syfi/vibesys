"""A single run that resumes on changed code goes on at the next round with its ledger and best.

The root's checkpoint holds what the search knows between rounds, so after a code change the
journal no longer replays, the flows restart from their checkpoints, and the search plays the
round after the last it finished: nothing played is played again and the best stays the best.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.orchestration.single._host import (
    IMPLEMENTER,
    PLANNER,
    SingleHost,
    config,
    implemented,
)
from tests.vibesys.orchestration.single.test_stories import design, implement, selection_of
from tests.vibesys.serial_search._replies import plan_reply

from vibesys.orchestration.single.search.api import single
from vs_core.api import RequestTurn, Retained, RevisionRef
from vs_core.api.testing import code_changed

if TYPE_CHECKING:
    from vs_core.api.testing import Asked

_CHANGED = code_changed(single)


def test_a_run_resumed_on_changed_code_continues_at_the_next_round_with_the_same_best() -> None:
    host = SingleHost.begin(
        single,
        config(max_rounds=3),
        restarting=False,
        throughputs={"c1": 400.0, "c2": 200.0, "c3": 1.0},
    )
    host.drain_until_turn(PLANNER)
    for index in (1, 2):
        design(host, f"h{index}")
        host.drain_until_turn(IMPLEMENTER)
        implement(host, candidate=f"c{index}")
        host.drain_until_turn(PLANNER)
    stale = {item.decision for item in host.run.open if isinstance(item.request, RequestTurn)}

    host.change_code(_CHANGED)
    host.run.advance_to(host.run.view.now_at + 1.0)  # the first event after the change

    def fresh(role: str) -> Asked:
        for _ in range(50):
            now = [item for item in host.run.open if item.decision not in stale]
            turn = next((item for item in now if item.label == role), None)
            if turn is not None:
                return turn
            host.answer(next(item for item in now if item.until is not None))
        raise AssertionError(host.run.timeline())

    host.run.reply_raw(fresh(PLANNER), plan_reply("h3"))
    host.run.reply(
        fresh(IMPLEMENTER),
        implemented(),
        checkpoint=Retained(revision=RevisionRef.of_git_commit("c3")),
    )
    host.drain()

    record = host.record
    assert record is not None
    assert [r.round_number for h in record.hypotheses for r in h.rounds] == [1, 2, 3]
    assert selection_of(host) == ("retained_candidate", "c1")
