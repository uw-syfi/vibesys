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


def _events(world: World, events: tuple[core.CoreEvent, ...]) -> tuple[World, list[object]]:
    heard: list[object] = []
    for event in events:
        result = core.step(world.state, event)
        world = replace(world, state=result.state)
        heard.extend(result.events)
    return world, heard


def test_a_jobs_spent_queue_budget_ends_it_alone_and_its_running_sibling_is_delivered() -> None:
    """Regression (M1): one job's spent queue budget froze its whole wait and cancelled a
    sibling that was running within its own limit. Now only the job that ran out ends."""
    world = BUILDERS[Source.MULTI](limits(queue_wait_budget=40.0))
    running, queued = world.jobs
    # A starts at 11 and runs within its limit; B queues until its budget runs out at 41.
    world, _ = _events(world, fact(world, 0, 11.0, state="running")[1])
    world = replace(world, jobs=(replace(running, sequence=2), queued))
    at = 1.0
    heard: list[object] = []
    while at + POLL <= 51.0:
        at += POLL
        world, events = fact(world, 1, at, state="pending")
        world, more = _events(world, events)
        heard.extend(more)
    (wait,) = world.state.evaluation.continuations
    assert wait.timeout is None
    assert [job.resource_id for job in wait.spent] == [queued.resource_id]
    (spent,) = wait.spent
    assert spent.by is core.WaitEnd.QUEUE_BUDGET
    assert spent.at == 1.0 + 40.0
    # A is still running, and nothing has asked to cancel it.
    assert not world.state.evaluation.jobs[0].terminal
    cancelled = {
        record.request.resource_id
        for record in world.state.records.records
        if isinstance(record.request, core.CancelOwnedJob)
    }
    assert cancelled == {queued.resource_id}
    # A ends within its limit: its result is delivered, and B is reported ended by its budget.
    world, events = fact(world, 0, 55.0, state="ended", status=core.ObservationStatus.SUCCEEDED)
    world, more = _events(world, events)
    heard.extend(more)
    (wait,) = world.state.evaluation.continuations
    assert wait.timeout is not None
    assert wait.timeout.by is core.WaitEnd.QUEUE_BUDGET
    assert wait.timeout.unfinished == (spent,)
    assert wait.timeout.reached_at == 55.0
    # The agent resumes once the executor confirms B's cancel.
    world, events = fact(world, 1, 56.0, state="ended", status=core.ObservationStatus.CANCELLED)
    world, more = _events(world, events)
    heard.extend(more)
    (resumed,) = [event for event in heard if isinstance(event, core.ResumeAuthorized)]
    assert resumed.timeout == wait.timeout


DOWN = 500.0
"""A host outage longer than the job's remaining limit and its whole queue budget."""
