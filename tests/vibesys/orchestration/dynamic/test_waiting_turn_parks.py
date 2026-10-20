"""An agent turn core suspends on its own evaluations parks its workstream's agent slot.

An implementer or judge that replies ``waiting_for_evaluation`` runs no agent until core
authorizes its resume, however long its jobs queue (S190). Its slot is lent to other work, so
the planner runs while every workstream waits (S191: an operator's steer for the planner is
stored for its next turn, which must come). The resume is bound to a slot late: it waits until
one is free, so agents at work never outnumber ``max_in_flight``. A run restarted after a code
change keeps that bound: a workstream that waited in a suspended turn asks its agent again only
once it has a slot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._host import PLANNER_LABEL, Host, begin, config
from tests.vibesys.orchestration.dynamic.test_agent_waits import offering
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.search.api import dynamic
from vs_core.api import Finish, RequestTurn
from vs_core.api.testing import code_changed

if TYPE_CHECKING:
    from vs_core.api import Continuation
    from vs_core.api.testing import RestartPolicy

WIDTH = 2
AGENTS = frozenset({"implementer", "judge", "merger", "profiler"})
"""The roles that work in a workstream's slot; the planner works in none."""


def at_work(host: Host) -> set[str]:
    """The workstreams with an agent turn open.

    By workstream: after a code change the fake still lists the turns asked before it, which
    their restarted workstreams ask again.
    """
    turns = host.open(RequestTurn)
    return {asked.decision.root.split("/")[0] for asked in turns if asked.label in AGENTS}


def all_waiting(*, restarting: bool | RestartPolicy) -> tuple[Host, list[Continuation]]:
    """A run whose ``WIDTH`` workstreams each suspended their implementer's first turn."""
    host = begin(
        config(max_rounds=3, max_in_flight=WIDTH, max_backlog=None),
        restarting=restarting,
        view=offering(),
    )
    host.drain_until_turn(PLANNER_LABEL)
    host.answer(host.open(RequestTurn, label=PLANNER_LABEL)[0])
    for _ in range(50):
        turns = host.open(RequestTurn, label="implementer")
        if len(turns) == WIDTH:
            return host, [host.run.suspend(turn) for turn in turns]
        host.answer(next(a for a in host.run.open if a.until is not None and a not in turns))
    message = f"the workstreams never asked their implementers: {host.run.timeline()}"
    raise AssertionError(message)


def start_new_work(host: Host) -> None:
    """Answer the planner, and every wait but the agents', until the new work's agents ask."""
    host.answer(host.open(RequestTurn, label=PLANNER_LABEL)[0])
    for _ in range(50):
        if len(at_work(host)) == WIDTH:
            return
        host.answer(next(a for a in host.run.open if a.until is not None and a.label not in AGENTS))
    message = f"the new work never asked its agents: {host.run.timeline()}"
    raise AssertionError(message)


def drain_within_width(host: Host, *, limit: int = 800) -> None:
    """Answer everything the ordinary way, checking no more agents work than the run's width."""
    for _ in range(limit):
        assert len(at_work(host)) <= WIDTH, (at_work(host), host.run.timeline())
        if not host.step(0):
            return
    message = f"the flow did not settle in {limit} steps: {host.run.timeline()}"
    raise AssertionError(message)


@pytest.mark.regression
@pytest.mark.xfail(strict=True, reason="S190/S191: a suspended turn keeps its agent slot")
@across_restarts
def test_the_planner_runs_while_every_workstream_waits_in_a_suspended_turn(
    *, restarting: bool | RestartPolicy
) -> None:
    host, _ = all_waiting(restarting=restarting)

    assert host.open(RequestTurn, label=PLANNER_LABEL), host.run.timeline()


@pytest.mark.regression
@pytest.mark.xfail(strict=True, reason="S190/S191: a suspended turn keeps its agent slot")
@across_restarts
def test_a_resumed_turn_waits_for_a_free_slot_and_agents_never_outnumber_the_width(
    *, restarting: bool | RestartPolicy
) -> None:
    host, waits = all_waiting(restarting=restarting)
    start_new_work(host)
    working = at_work(host)

    for wait in waits:
        host.run.resume_ready(wait)

    assert at_work(host) == working  # the resumed turns wait: both slots are taken
    drain_within_width(host)
    assert isinstance(host.run.finished, Finish)
    timeline = host.run.timeline()
    assert "000001.h1/implementer.2:" in timeline, timeline  # each resumed in a slot
    assert "000002.h2/implementer.2:" in timeline, timeline


@pytest.mark.regression
@pytest.mark.xfail(strict=True, reason="S190/S191: a suspended turn keeps its agent slot")
def test_a_code_change_while_turns_wait_restarts_them_only_into_free_slots() -> None:
    host, waits = all_waiting(restarting=False)
    start_new_work(host)

    host.run.swap(code_changed(dynamic))
    host.run.resume_ready(waits[0])  # the first event after the change restarts every flow

    assert len(at_work(host)) <= WIDTH, (at_work(host), host.run.timeline())
    drain_within_width(host)
    assert isinstance(host.run.finished, Finish)
