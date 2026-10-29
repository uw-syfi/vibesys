"""The serial designer's roadmap, told as stories against both serial searches.

The designer sees the run's roadmap as typed items in its prompt and changes it only through its
reply; the search puts an accepted change through the roadmap's owner, so the next design sees
it. A change the roadmap refuses is corrected once, like a plan the ledger refuses, and a roadmap
that cannot be read ends the design with the reason. Every story runs live and again with a
restart after every step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.vibesys.serial_search._replies import plan_reply
from tests.vibesys.serial_search.test_shared_stories import (
    IMPLEMENTER,
    PLANNER,
    implement,
    start,
)
from tests.vibesys.vibesys_flows import across_restarts

from vs_core.api import Finish, Operation
from vs_issue_tracker.api import (
    FakeRoadmap,
    ReadRoadmap,
    RoadmapItem,
    RoadmapItemKind,
    RoadmapSnapshot,
    RoadmapStatus,
    roadmap_problem,
)

if TYPE_CHECKING:
    from tests.vibesys.serial_search._host import SerialHost

MODES = across_restarts
SEARCHES = pytest.mark.parametrize("search", ["single", "multi"])


def _major(item_id: str, status: RoadmapStatus = RoadmapStatus.TODO) -> RoadmapItem:
    return RoadmapItem(id=item_id, kind=RoadmapItemKind.MAJOR, title=item_id, status=status)


def _minor(item_id: str, major: str) -> RoadmapItem:
    return RoadmapItem(
        id=item_id,
        kind=RoadmapItemKind.MINOR,
        title=item_id,
        status=RoadmapStatus.TODO,
        major=major,
    )


def _change(*items: RoadmapItem) -> list[object]:
    return [item.model_dump(mode="json") for item in items]


def _designer_prompt_roadmap(host: SerialHost) -> tuple[RoadmapItem, ...]:
    host.drain_until_turn(PLANNER)
    return host.run.expect_turn(role=PLANNER).context.roadmap  # ty: ignore[unresolved-attribute]


@SEARCHES
@MODES
def test_the_designers_roadmap_change_is_kept_and_shown_at_the_next_design(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting, config={"max_rounds": 2})
    seeded = (_major("major-1"), _minor("minor-1", "major-1"))

    assert _designer_prompt_roadmap(host) == ()
    host.run.reply_raw(
        host.run.expect_turn(role=PLANNER), plan_reply("h1", roadmap=_change(*seeded))
    )
    implement(host, search, "c1")

    assert _designer_prompt_roadmap(host) == seeded
    parked = _major("major-1", RoadmapStatus.PARKED)
    host.run.reply_raw(
        host.run.expect_turn(role=PLANNER), plan_reply("h2", roadmap=_change(parked))
    )
    host.drain_until_turn(IMPLEMENTER)

    assert host.roadmap.items() == (parked, seeded[1])


_REFUSED = {
    "a minor naming no major": (_minor("minor-2", "major-9"),),
    "a changed kind": (_minor("major-1", "major-1"),),
    "a repeated id": (_major("major-2"), _major("major-2")),
}


@SEARCHES
@MODES
@pytest.mark.parametrize("refused", sorted(_REFUSED))
def test_a_roadmap_change_the_roadmap_refuses_is_corrected_by_a_turn_that_names_why(
    *, search: str, restarting: bool, refused: str
) -> None:
    held = (_major("major-1"),)
    host = start(search, restarting=restarting, roadmap=FakeRoadmap(held))
    batch = _REFUSED[refused]

    host.drain_until_turn(PLANNER)
    host.run.reply_raw(
        host.run.expect_turn(role=PLANNER), plan_reply("h1", roadmap=_change(*batch))
    )
    correction = host.run.expect_turn(role=PLANNER)

    assert getattr(correction.context, "problem", None) == roadmap_problem(held, batch)
    assert host.roadmap.items() == held
    host.run.reply_raw(correction, plan_reply("h1"))
    host.drain_until_turn(IMPLEMENTER)


@SEARCHES
@MODES
def test_a_roadmap_that_cannot_be_read_ends_the_run_without_a_round_naming_why(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting)
    for _ in range(50):
        reads = [
            asked
            for asked in host.open(Operation)
            if isinstance(asked.request, Operation)
            and isinstance(asked.request.request, ReadRoadmap)
        ]
        if reads:
            break
        assert host.step(0), host.run.timeline()
    host.run.finish_operation(reads[0], RoadmapSnapshot(status="failed", detail="store is down"))
    host.drain(limit=100)

    assert not host.opened
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "failure"
    assert "reading the roadmap: failed: store is down" in finish.result.reason
