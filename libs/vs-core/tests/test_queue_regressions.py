"""A wait whose job queued past its stated deadline and then started runs on, for every source.

Regression (H2): a wait's deadline was moved by the job's queue time only for an agent's own
non-repeat submission. A wait on a registered operation's job, or an agent's repeat call
naming a submission a strategy's ``Measure`` allocated, kept its stated deadline; when its
job queued past that deadline and then started, core raised ``ContractError: deadline must
freeze before job facts`` and halted the run. Queue time is now each job's own data, so
every source's limit counts from its job's start.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import vs_core.api as core
from .queue_worlds import BUILDERS, LIMIT, Source, World, fact, limits

POLL = 10.0
"""A fresh poll cadence: the job's queue time is credited in full."""
STARTS_AT = 205.0
"""The job starts long after the stated deadline (``LIMIT``)."""


def _apply(world: World, events: tuple[core.CoreEvent, ...]) -> World:
    for event in events:
        world = replace(world, state=core.step(world.state, event).state)
    return world


def _queued_then_started(source: Source) -> World:
    world = BUILDERS[source](limits())
    at = 1.0
    while at + POLL < STARTS_AT:
        at += POLL
        for index in range(len(world.jobs)):
            world, events = fact(world, index, at, state="pending")
            world = _apply(world, events)
    for index in range(len(world.jobs)):
        world, events = fact(world, index, STARTS_AT, state="running")
        world = _apply(world, events)
    return world


@pytest.mark.parametrize(
    "source", [Source.AGENT, Source.AGENT_REPEAT, Source.REGISTERED, Source.MULTI]
)
def test_a_wait_whose_job_queued_past_its_deadline_runs_its_limit_from_its_start(
    source: Source,
) -> None:
    world = _queued_then_started(source)
    (wait,) = world.state.evaluation.continuations
    assert wait.timeout is None
    # Started at 205 after 204 s of queue: its limit ends its stated span of running later.
    ends_at = world.stated_at + STARTS_AT - 1.0
    assert ends_at >= STARTS_AT + LIMIT - 1.0
    ran = core.step(world.state, core.ClockAdvanced(now_at=ends_at - 1.0)).state
    (wait,) = ran.evaluation.continuations
    assert wait.timeout is None
    ended = core.step(ran, core.ClockAdvanced(now_at=ends_at)).state
    (wait,) = ended.evaluation.continuations
    assert wait.timeout is not None
    assert wait.timeout.deadline_at == ends_at
