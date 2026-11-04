"""An agent turn core suspends on its own evaluations parks its workstream's agent slot.

An implementer or judge that replies ``waiting_for_evaluation`` runs no agent until core
authorizes its resume, however long its jobs queue (S190). Its slot is lent to other work, so
the planner runs while every workstream waits (S191: an operator's steer for the planner is
stored for its next turn, which must come). The resume is bound to a slot late: it waits until
one is free, so agents at work never outnumber ``max_in_flight``. A run restarted after a code
change keeps that bound: a workstream that waited in a suspended turn asks its agent again only
once it has a slot. Core admits at most a limited number of attempts at once (an operator may
lower it to ``max_in_flight``); a parked turn keeps its attempt open, and new work queued for
admission must never keep the parked turns from resuming. Every story admits attempts as core
does, up to the run's launch limit unless it lowers it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._host import PLANNER_LABEL, Host, begin, config
from tests.vibesys.orchestration.dynamic.test_agent_waits import offering
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.search.api import budget, dynamic
from vs_core.api import Finish, RequestTurn, StartAttempt
from vs_core.api.testing import code_changed

if TYPE_CHECKING:
    from vs_core.api import Continuation
    from vs_core.api.testing import Asked, RestartPolicy

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


def admissible(host: Host, *, limit: int | None = None) -> list[Asked]:
    """The open waits core would answer now: a start only while fewer than ``limit`` attempts
    are open (core's admission; by default the run's launch limit)."""
    limit = budget(host.config).parallel if limit is None else limit
    full = len(host.unsettled) >= limit
    return [
        asked
        for asked in host.run.open
        if asked.until is not None and not (full and isinstance(asked.request, StartAttempt))
    ]


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
        host.answer(next(a for a in admissible(host) if a not in turns))
    message = f"the workstreams never asked their implementers: {host.run.timeline()}"
    raise AssertionError(message)


def start_new_work(host: Host) -> None:
    """Answer the planner, and every wait but the agents', until the new work's agents ask."""
    host.answer(host.open(RequestTurn, label=PLANNER_LABEL)[0])
    for _ in range(50):
        if len(at_work(host)) == WIDTH:
            return
        waits = [a for a in admissible(host) if a.label not in AGENTS]
        if not waits:
            break  # the new work's attempts wait for core's admission
        host.answer(waits[0])
    message = f"the new work never asked its agents: {host.run.timeline()}"
    raise AssertionError(message)


def drain_within_width(host: Host, *, limit: int = 800) -> None:
    """Answer everything the ordinary way, checking no more agents work than the run's width."""
    for _ in range(limit):
        assert len(at_work(host)) <= WIDTH, (at_work(host), host.run.timeline())
        if waits := admissible(host):
            host.answer(waits[0])
        elif host.run.sleeping_until:
            host.run.advance_to(min(host.run.sleeping_until))
        else:
            return
    message = f"the flow did not settle in {limit} steps: {host.run.timeline()}"
    raise AssertionError(message)


def drain_admitting(host: Host, *, limit: int, steps: int = 800) -> None:
    """Answer everything the ordinary way, admitting an attempt only while fewer than ``limit``
    are open (core's admission), and never moving the clock: a run that idles until its deadline
    is stuck."""
    for _ in range(steps):
        assert len(at_work(host)) <= WIDTH, (at_work(host), host.run.timeline())
        if not (waits := admissible(host, limit=limit)):
            return
        host.answer(waits[0])
    message = f"the flow did not settle in {steps} steps: {host.run.timeline()}"
    raise AssertionError(message)


@pytest.mark.regression
@across_restarts
def test_the_planner_runs_while_every_workstream_waits_in_a_suspended_turn(
    *, restarting: bool | RestartPolicy
) -> None:
    host, _ = all_waiting(restarting=restarting)

    assert host.open(RequestTurn, label=PLANNER_LABEL), host.run.timeline()


@pytest.mark.regression
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
def test_a_code_change_while_turns_wait_restarts_them_only_into_free_slots() -> None:
    host, waits = all_waiting(restarting=False)
    start_new_work(host)

    host.run.swap(code_changed(dynamic))
    host.run.resume_ready(waits[0])  # the first event after the change restarts every flow

    assert len(at_work(host)) <= WIDTH, (at_work(host), host.run.timeline())
    drain_within_width(host)
    assert isinstance(host.run.finished, Finish)


@pytest.mark.regression
@across_restarts
def test_new_work_queued_for_admission_never_keeps_parked_turns_from_resuming(
    *, restarting: bool | RestartPolicy
) -> None:
    host, waits = all_waiting(restarting=restarting)  # h1 and h2 park, each holding an attempt
    host.answer(host.open(RequestTurn, label=PLANNER_LABEL)[0])  # the planner plans h3 and h4
    drain_admitting(host, limit=WIDTH)  # an operator set core's limit to the width

    for wait in waits:
        host.run.resume_ready(wait)
    drain_admitting(host, limit=WIDTH)

    assert isinstance(host.run.finished, Finish), (
        f"stuck: open attempts {len(host.unsettled)}, waiting {[a.path for a in host.run.open]}"
    )


@pytest.mark.regression
def test_a_turn_whose_wait_timed_out_asks_its_agent_again_only_in_a_free_slot() -> None:
    host, _ = all_waiting(restarting=False)
    start_new_work(host)

    host.run.advance_to(max(host.run.sleeping_until))  # the waits time out while h3 and h4 work
    drain_within_width(host)  # h1 and h2 are asked again, each once a slot is free

    timeline = host.run.timeline()
    assert "000001.h1/implementer.2:" in timeline, timeline
    assert "000002.h2/implementer.2:" in timeline, timeline
