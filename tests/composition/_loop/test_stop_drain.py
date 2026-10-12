"""A stop that finds an uncancellable request in flight drains the run to a terminal record.

A measurement submission and a job poll have no cancellation in core, so a stop that arrives
while one is in flight must wait for its observation and then cancel the run's owned jobs
through requests core issues. Whatever request the stop lands on, and however long the host waits
before giving up on it, the run must reach a terminal record with every owned job released, and a resumed
host must finish the drain instead of giving up again.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st
from tests.composition._loop import conformance as c
from tests.composition._loop.subjects import SUBJECTS
from tests.composition._loop.test_conformance import outcomes

from vs_core.api import OwnedJob, RegisteredOwnedJob, RunStatus
from vs_core.testing.liveness import End, final_state
from vs_dst.api import Effects, RecoverableExecutors
from vs_project.api import FakeStateStore
from vs_runtime.api.core import drive_core, start_core
from vs_runtime.api.infrastructure import RunStopped
from vs_runtime.api.testing import (
    LEASE,
    FakePublicationDelivery,
    HostOptions,
    scripted_host,
)
from vs_sim.api.testing import run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from pydantic import BaseModel

    from vs_core.api import CoreState, OperationRegistry, SchemaRef
    from vs_core.api.testing import Journal
    from vs_runtime.api.core import RunOutcome
    from vs_runtime.api.testing import Script
    from vs_sim.api.testing import VirtualClock

MAX_HOSTS = 4
"""A stop needs the host that met it and at most one resume per death; more is a loop."""

PURE = settings(
    max_examples=12,
    deadline=None,
    derandomize=True,
    phases=(Phase.explicit, Phase.generate),
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


async def _until_the_host_gives_up(
    world: c.Faulted, clock: VirtualClock, run: Coroutine[object, object, RunOutcome], grace: float
) -> RunOutcome | None:
    """Drive the loop; abandon it ``grace`` seconds after the stop reaches the run.

    This is what the host does when its grace bound passes: it cancels the loop, which leaves
    the requests it was waiting for dispatched and the run's record open for a resume.
    """
    loop = asyncio.ensure_future(run)
    while not loop.done() and not world.stopped:
        await clock.sleep(1.0)
    if not loop.done():
        await asyncio.wait([loop], timeout=grace)
    if loop.done():
        return await loop
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)
    return None


def _hosts_until_ended(
    subject: c.Subject, world: c.Faulted, grace: float | None
) -> tuple[CoreState, int]:
    """Run hosts over one store until the run ends; return its core state and the host count.

    With a ``grace`` the first host gives up that long after the stop (see above); a resume
    then runs without a bound.
    """
    store = FakeStateStore()
    effects = Effects()
    delivery = FakePublicationDelivery(store)
    clock_at = 1.0

    def translator(
        script: Script,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        journal: Journal,
    ) -> RecoverableExecutors:
        return RecoverableExecutors(script, core, registry, schemas, journal, effects)

    options = HostOptions(attach=world.attach, translator=translator)
    for hosts in range(1, MAX_HOSTS + 1):
        scripted = scripted_host(
            subject.new_strategy(),
            world,
            subject.harness,
            subject.schemas,
            options,
            store=store,
            delivery=delivery,
            clock_at=clock_at,
        )
        shell, host, config, clock = scripted.shell, scripted.host, scripted.config, scripted.clock
        start_core(host, config)
        running = drive_core(host, config)
        try:
            if grace is not None and hosts == 1:
                outcome = run_virtual(clock, _until_the_host_gives_up(world, clock, running, grace))
            else:
                outcome = run_virtual(clock, running)
        except RunStopped:
            outcome = None
        if outcome is None:
            clock_at += LEASE + 1
            continue
        assert outcome.status == RunStatus.TERMINAL
        return shell.record.envelope.core, hosts
    pytest.fail(f"the run was still open after {MAX_HOSTS} hosts: a resume never finishes")


def _jobs(core: CoreState) -> list[OwnedJob | RegisteredOwnedJob]:
    return [*core.evaluation.jobs, *core.evaluation.registered_jobs]


@pytest.mark.parametrize("name", sorted(SUBJECTS))
@PURE
@given(
    data=st.data(),
    stop_at=st.integers(min_value=0, max_value=40),
    grace=st.one_of(st.none(), st.sampled_from([0.0, 1.0, 5.0, 60.0])),
)
def test_a_stop_at_any_request_drains_to_a_terminal_record_and_cancels_every_job(
    name: str, data: st.DataObject, stop_at: int, grace: float | None
) -> None:
    subject = replace(SUBJECTS[name], harness=replace(SUBJECTS[name].harness, deadline_at=15_000.0))
    world = c.Faulted(data.draw(subject.worlds)(), data.draw(outcomes), stop_at=stop_at)
    core, _hosts = _hosts_until_ended(subject, world, grace)
    if not world.stopped:
        return
    assert core.run.status is RunStatus.TERMINAL
    assert final_state(core, End.TERMINAL) == []
    # Every job the run owns was cancelled or had ended on its own: none is left running.
    assert all(job.released for job in _jobs(core)), [
        (job.resource_id, job.status) for job in _jobs(core) if not job.released
    ]
