"""C6: an operator pause injected while any request runs parks the run with nothing open.

A pause that exits like a crash leaves the request it met in flight, and the resume that follows
has to prove what became of it or end the run blocked. A pause instead closes admission, lets each
running request record its end, and only then parks, so a resume finds a log with no open request
to account for. Whichever request the pause lands on, in either mode, the run parks with every
request it dispatched ended (a measurement submission whose job the park released stays open by
design: it is the measurement the next epoch runs again), and the hosts that resume it reach the
end an unpaused run reaches. A pull request pauses at a sample of the requests; `--depth=nightly`
at each.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, get_args

import pytest
from tests.system.delivery._skeleton_subject import (
    DEADLINE,
    Faulted,
    Pause,
    harness,
    launch,
)

from vs_core.api import (
    ContractError,
    ControlId,
    ControlInput,
    RequestPhase,
    RunControlEvent,
    RunResultProposal,
    RunStatus,
    StopMode,
    SubmitMeasurement,
    step,
)
from vs_core.testing.liveness import End, final_state
from vs_project.api import FakeStateStore
from vs_runtime.api.core import drive_core, start_core
from vs_runtime.api.testing import (
    LEASE,
    FakePublicationDelivery,
    HostOptions,
    ScriptedExecutors,
    scripted_host,
)
from vs_sim.api.testing import run_virtual, swept

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_runtime.api.core import ExecutionContext, ExecutionResult
    from vs_runtime.api.testing import TranslatorFactory

type Action = Literal["pause", "pause_now"]

MAX_HOSTS = 3
"""A pause needs the host that met it and one resume; a third would mean the resume pauses again."""

OPEN = (RequestPhase.DISPATCHED, RequestPhase.RECONCILING)


@functools.cache
def _unpaused() -> tuple[int, RunResultProposal]:
    """How many requests an unpaused run executes, and the end it reaches."""
    probe = Faulted()
    spec = launch()
    run = scripted_host(spec, probe, harness(deadline_at=DEADLINE), spec.search.reply_schemas).run()
    assert run.core.run.result is not None
    return probe.requests, run.core.run.result


def _kinds() -> list[str]:
    probe = Faulted()
    spec = launch()
    scripted_host(spec, probe, harness(deadline_at=DEADLINE), spec.search.reply_schemas).run()
    return probe.kinds


def _points(samples: int = 3) -> list[object]:
    count, _ = _unpaused()
    return [
        pytest.param(action, at, id=f"{action}-at-{at}")
        for action in get_args(Action.__value__)
        for at in swept(count, samples)
    ]


def _open(core: CoreState) -> list[tuple[str, str]]:
    """The requests the run dispatched and has no end for, bar the measurements a park keeps."""
    kept = {job.submission_id for job in core.evaluation.jobs}
    return [
        (row.request.kind, row.phase.value)
        for row in core.records.records
        if row.phase in OPEN
        and not (isinstance(row.request, SubmitMeasurement) and row.request_id in kept)
    ]


def _hosts(world: Faulted, options: HostOptions | None = None) -> list[CoreState]:
    """Run hosts over one store until the run ends; the core state each host left.

    The first host meets the world's pause; each later one opens the run as an operator's resume
    does. ``options`` says how the hosts are arranged beyond that.
    """
    arranged = options or HostOptions()
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
            replace(arranged, attach=world.attach, resumed=hosts > 0),
            store=store,
            delivery=delivery,
            clock_at=clock_at,
        )
        start_core(host.host, host.config)
        outcome = run_virtual(host.clock, drive_core(host.host, host.config))
        core = host.shell.record.envelope.core
        left.append(core)
        assert outcome.status is core.run.status
        if core.run.status is RunStatus.TERMINAL:
            return left
        assert core.run.status is RunStatus.PARKED
        host.shell.release_lease(now_at=host.clock.now())
        clock_at = host.clock.now() + LEASE + 1
    pytest.fail(f"the run was still parking after {MAX_HOSTS} hosts")


@pytest.mark.parametrize(("action", "pause_at"), _points())
def test_c6_a_pause_at_any_request_parks_with_nothing_open_and_resumes_to_the_same_end(
    action: Action, pause_at: int
) -> None:
    _, reference = _unpaused()
    world = Faulted(pause=Pause(pause_at, action))
    left = _hosts(world)
    assert world.paused
    parked, ended = left[0], left[-1]
    if parked.run.status is RunStatus.TERMINAL:
        # The run decided its own end in the step the pause arrived: there was nothing to park.
        assert parked.run.result == reference
        return
    assert parked.run.status is RunStatus.PARKED
    assert parked.run.result is None
    assert _open(parked) == []
    assert ended.run.status is RunStatus.TERMINAL
    assert ended.run.result is not None
    # A resume restarts the search, which the skeleton cannot do once its one attempt has begun
    # (it ends with no winner); before that, the resumed run is the unpaused run.
    if parked.attempts.attempts:
        assert ended.run.result.outcome != "blocked", ended.run.result.reason
    else:
        assert ended.run.result == reference
    assert final_state(ended, End.TERMINAL) == []


def _stops(core: CoreState) -> list[RunControlEvent]:
    """Every stop an operator, the deadline or a blocked recovery can send, at ``core``'s time."""
    return [
        RunControlEvent(
            control=ControlInput(
                control_id=ControlId(root=f"stop-{mode.value}-{outcome}"),
                action="stop",
                mode=mode,
            ),
            result=RunResultProposal(outcome=outcome, reason="test"),
            now_at=core.run.now_at,
        )
        for mode in StopMode
        for outcome in ("cancelled", "blocked")
    ]


@pytest.mark.parametrize(("action", "pause_at"), _points(2))
def test_c6_a_stop_of_any_mode_ends_a_run_that_was_paused_without_publishing_requests(
    action: Action, pause_at: int
) -> None:
    """Ending a run cannot fail: a terminal transition publishes nothing, whichever stop ends it.

    The states are those the run held when the pause was sent and when its resume dispatched
    first, the two a blocked recovery or an operator's stop finds a paused run in.
    """
    world = Faulted(pause=Pause(pause_at, action))
    left = _hosts(world)
    first = left[0].records.recovery.epoch
    resumed = next((core for core in world.cores if core.records.recovery.epoch > first), None)
    for core in (world.cores[pause_at], left[0], *(() if resumed is None else (resumed,))):
        for event in _stops(core):
            try:
                after = step(core, event)
            except ContractError as error:
                pytest.fail(f"{event.control.control_id.root} at {core.run.status}: {error}")
            if after.state.run.status is RunStatus.TERMINAL:
                assert not after.requests


@dataclass
class _Hang:
    """A session start whose reply never arrives, once: the far side of a request that outlives a drain."""

    world: Faulted
    entered: int = 0
    cleaned: int = 0

    def translator(self) -> TranslatorFactory:
        hang = self

        class Hanging(ScriptedExecutors):
            async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
                result = await super().execute(request, context)
                if request.kind == "ensure_session" and not hang.entered:
                    hang.entered += 1
                    assert hang.world.clock is not None
                    try:
                        await hang.world.clock.sleep(1e9)
                    finally:
                        hang.cleaned += 1
                return result

        return Hanging


@pytest.mark.parametrize("action", get_args(Action.__value__))
def test_c6_a_request_still_running_at_the_drain_deadline_is_cancelled_and_run_again(
    action: Action,
) -> None:
    """The park is not held by a request that never returns, and the request is not lost.

    The session start of the implementer is cancelled at the deadline, so its executor's cleanup
    runs, and the log holds it as a request to send again: neither a failure nor a retry. (Whether
    the resumed search still wants it is the search's: the skeleton restarts without its attempt.
    ``test_a_pause_past_its_deadline_interrupts_a_request_that_can_run_again_and_the_resume_sends_it``
    follows the resume that does.)
    """
    ensure = _kinds().index("ensure_session")
    world = Faulted(pause=Pause(ensure, action, drain_seconds=5.0))
    hang = _Hang(world)
    left = _hosts(world, HostOptions(translator=hang.translator(), controlled=True))
    parked, ended = left[0], left[-1]
    assert (hang.entered, hang.cleaned) == (1, 1)
    assert parked.run.status is RunStatus.PARKED
    assert _open(parked) == []
    [rerun] = [row for row in parked.records.records if row.request.kind == "ensure_session"]
    assert rerun.phase is RequestPhase.PREPARED
    assert rerun.retry_count == 0
    assert rerun.observation is None
    assert ended.run.status is RunStatus.TERMINAL
    assert ended.run.result is not None
    assert ended.run.result.outcome != "blocked", ended.run.result.reason
