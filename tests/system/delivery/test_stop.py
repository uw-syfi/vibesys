"""C5: an operator stop injected while any request runs ends the run terminal.

A measurement submission and a job poll have no cancellation in core, so a stop that arrives
while one is in flight must wait for its observation, then cancel the run's owned jobs through
requests core issues. Whichever request the stop lands on, however long the host waits before
giving up on it, and however the measurements and turns before it went, the run reaches a
terminal record with every owned job released, and a resumed host finishes the drain instead of
giving up again. A pull request stops at a sample of the requests; `--depth=nightly` at each.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.system.delivery._skeleton_subject import (
    DEADLINE,
    Faulted,
    Outcomes,
    harness,
    launch,
)

from vs_core.api import OwnedJob, RegisteredOwnedJob, RunStatus, reopens_on_resume
from vs_core.testing.liveness import Budget, End, assert_live, final_state
from vs_dst.api import Effects, RecoverableExecutors
from vs_project.api import FakeStateStore
from vs_runtime.api.core import drive_core, start_core
from vs_runtime.api.infrastructure import RunStopped
from vs_runtime.api.testing import (
    LEASE,
    FakePublicationDelivery,
    HostOptions,
    drive_shell,
    scripted_host,
)
from vs_sim.api.testing import Depth, depth, run_virtual, swept

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

VARIANTS = {
    "plain": Outcomes(),
    "workload_failure": Outcomes(measurements=("workload_failure",)),
    "unparsable_turn": Outcomes(turns=("unparsable",)),
}


def _request_count(chosen: Outcomes) -> int:
    probe = Faulted(chosen)
    drive_shell(launch(), probe, harness(), launch().search.reply_schemas)
    return probe.requests


def _stops(samples: int = 3) -> list[tuple[str, int]]:
    return [
        (variant, at)
        for variant, chosen in VARIANTS.items()
        for at in swept(_request_count(chosen), samples)
    ]


def _stop_params() -> list[object]:
    return [pytest.param(variant, at, id=f"{variant}-stop-at-{at}") for variant, at in _stops(2)]


def _adoption_stops() -> list[object]:
    """Every request of the run's adoption, as a point the stop lands on."""
    points = []
    for variant, chosen in VARIANTS.items():
        probe = Faulted(chosen)
        drive_shell(launch(), probe, harness(), launch().search.reply_schemas)
        points += [
            pytest.param(variant, at, id=f"{variant}-stop-during-{kind}-{at}")
            for at, kind in enumerate(probe.kinds)
            if kind in ("adopt_revision", "verify_adoption")
        ]
    return points


GRACES: tuple[float | None, ...] = (None, 0.0, 5.0, 60.0)


def _drain_params() -> list[object]:
    """Each stop point with the host's grace: every grace at nightly, one per point on a PR.

    The grace decides only how long the first host waits before it gives up, and the points
    differ in the request the stop finds in flight, so a pull request pairs them off (every
    grace meets some point) and the nightly job crosses them.
    """
    stops = _stops(4)
    pairs = [
        (variant, at, grace)
        for index, (variant, at) in enumerate(stops)
        for grace in (GRACES if depth() is Depth.NIGHTLY else (GRACES[index % len(GRACES)],))
    ]
    return [pytest.param(v, at, g, id=f"{v}-stop-at-{at}-{g}") for v, at, g in pairs]


@pytest.mark.parametrize(("variant", "stop_at"), _stop_params())
def test_c5_an_operator_stop_at_any_request_ends_the_run_and_leaves_nothing(
    variant: str, stop_at: int
) -> None:
    world = Faulted(VARIANTS[variant], stop_at=stop_at)
    hosting = harness(deadline_at=DEADLINE)
    spec = launch()
    run = drive_shell(
        spec, world, hosting, spec.search.reply_schemas, HostOptions(attach=world.attach)
    )
    assert world.stopped
    # The loop waits for requests core cannot cancel, so the run never ends open for a resume.
    assert not run.stopped
    if run.halted is not None:
        raise run.halted
    # The run may have decided its own result in the same step the stop arrived; whatever it
    # is, the run is terminal with every attempt settled and nothing open.
    assert run.core.run.result is not None
    budget = Budget(retries=hosting.limits.request_retries.limit)
    assert_live(run.journal, run.core, budget, End.TERMINAL)


async def _until_the_host_gives_up(
    world: Faulted, clock: VirtualClock, run: Coroutine[object, object, RunOutcome], grace: float
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


def _hosts_until_ended(world: Faulted, grace: float | None) -> CoreState:
    """Run hosts over one store until the run ends; return its core state.

    With a ``grace`` the first host gives up that long after the stop; a resume then runs
    without a bound.
    """
    store = FakeStateStore()
    effects = Effects()
    delivery = FakePublicationDelivery()
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
    hosting = harness(deadline_at=DEADLINE)
    for hosts in range(1, MAX_HOSTS + 1):
        spec = launch()
        scripted = scripted_host(
            spec,
            world,
            hosting,
            spec.search.reply_schemas,
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
            # The next host starts once the dead host's lease, last renewed now, has lapsed.
            clock_at = clock.now() + LEASE + 1
            continue
        assert outcome.status == RunStatus.TERMINAL
        return shell.record.envelope.core
    pytest.fail(f"the run was still open after {MAX_HOSTS} hosts: a resume never finishes")


def _jobs(core: CoreState) -> list[OwnedJob | RegisteredOwnedJob]:
    return [*core.evaluation.jobs, *core.evaluation.registered_jobs]


@pytest.mark.parametrize(("variant", "stop_at", "grace"), _drain_params())
def test_a_stop_at_any_request_drains_to_a_terminal_record_and_cancels_every_job(
    variant: str, stop_at: int, grace: float | None
) -> None:
    world = Faulted(VARIANTS[variant], stop_at=stop_at)
    core = _hosts_until_ended(world, grace)
    assert world.stopped
    assert core.run.status is RunStatus.TERMINAL
    assert final_state(core, End.TERMINAL) == []
    # Every job the run owns was cancelled or had ended on its own: none is left running.
    assert all(job.released for job in _jobs(core)), [
        (job.resource_id, job.status) for job in _jobs(core) if not job.released
    ]


def _resumed_until_it_ends_itself(world: Faulted) -> list[CoreState]:
    """Resume the run after each stop that ends it, until a host reaches an end that is its own.

    Each host opens the run the way a resume does (its controls lift the stop the record holds),
    over the store the one before left. Returns the core state each host left.
    """
    hosting = harness(deadline_at=DEADLINE)
    store = FakeStateStore()
    delivery = FakePublicationDelivery()
    schemas = launch().search.reply_schemas
    left: list[CoreState] = []
    clock_at = 1.0
    for hosts in range(MAX_HOSTS):
        host = scripted_host(
            launch(),
            world,
            hosting,
            schemas,
            HostOptions(attach=world.attach, resumed=hosts > 0),
            store=store,
            delivery=delivery,
            clock_at=clock_at,
        )
        start_core(host.host, host.config)
        run_virtual(host.clock, drive_core(host.host, host.config))
        core = host.shell.record.envelope.core
        left.append(core)
        assert core.run.status is RunStatus.TERMINAL
        assert core.run.result is not None
        if core.run.result.outcome != "cancelled":
            return left
        assert reopens_on_resume(core.run), core.run.result
        clock_at = host.clock.now() + LEASE + 1
    pytest.fail(f"the run was still stopping after {MAX_HOSTS} hosts")


@pytest.mark.parametrize(("variant", "stop_at"), [*_stop_params(), *_adoption_stops()])
def test_a_run_stopped_at_any_request_is_resumable_and_goes_on_to_its_own_end(
    variant: str, stop_at: int
) -> None:
    """Whichever request the operator's stop finds in flight, the run is not over for good.

    Regression: a stop that raced the run's adoption (or any request) ended the run, cancelled or
    failed, with no way back, so a run with hours of rounds in it could only be abandoned. The
    stop's ending cleans up and keeps the result, and the resume that opens the run reopens it:
    it goes on from what it held and reaches an end of its own (no stop ends it a second time).
    """
    world = Faulted(VARIANTS[variant], stop_at=stop_at)
    left = _resumed_until_it_ends_itself(world)
    assert world.stopped
    resumed = left[-1]
    assert resumed.run.result is not None
    assert resumed.run.result.outcome != "cancelled" or len(left) == 1
    assert final_state(resumed, End.TERMINAL) == []
    assert all(job.released for job in _jobs(resumed))


def _stop_pairs() -> list[object]:
    """Two stops of one run: the second finds the run after a resume, with what the first adopted."""
    points = [at for variant, at in _stops(3) if variant == "plain"]
    return [
        pytest.param(first, later, id=f"stops-at-{first}-and-{later}")
        for first in points
        for later in points
        if first < later
    ]


@pytest.mark.parametrize(("first", "later"), _stop_pairs())
def test_a_run_stopped_again_after_a_resume_is_resumable_again(first: int, later: int) -> None:
    """A second stop finds the run reopened by a resume: nothing it holds ends it for good.

    The first stop's adoption answered that stop, so the second adopts afresh; the core-level
    story of it is ``test_a_lifted_stop_takes_back_the_adoption_it_completed``.
    """
    world = Faulted(VARIANTS["plain"], stop_at=(first, later))
    left = _resumed_until_it_ends_itself(world)
    results = [state.run.result for state in left]
    assert all(result is not None for result in results)
    assert not any("adoption-failed" in (result.reason if result else "") for result in results)
    assert final_state(left[-1], End.TERMINAL) == []
