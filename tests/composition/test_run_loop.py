"""The production run loop (``vs_runtime._core_run``) over the real shell and executors.

Time is a fake clock whose ``sleep`` advances a counter, so nothing waits. The inert
strategy proposes nothing: the run stays open until a control or the deadline ends it.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.support.host_clock import ProbedClock, clock_from
from tests.support.skeleton_strategy import SkeletonState, SkeletonStrategy
from tests.support.skeleton_world import (
    LEASE,
    Process,
    StalledError,
    World,
    drive,
    open_skeleton_world,
)

from vs_core.api import ArtifactRef, Limits, Proposal, RunResultProposal, RunStatus
from vs_core.testing.builders import initial_state
from vs_runtime.api.core import (
    CoreRunHost,
    DispatchCapExceededError,
    LeaseUnavailableError,
    RunControlBridge,
    RunLoopConfig,
    RunStalledError,
    drive_core,
    start_core,
    start_core_awaiting_lease,
)
from vs_runtime.api.infrastructure import RunStopped, RuntimeRunControlChannel

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import RunView, StrategyEvent
    from vs_sim.api.testing import VirtualClock

#: Event loop turns a stuck fake turn lasts, standing for "longer than anything the run waits".
_TURN_BOUND = 1000

INERT = SkeletonStrategy(state=SkeletonState(schema_version=1, phase="done"), measured=False)


class BaselineOnly(SkeletonStrategy):
    """Measure the baseline, then cancel the run: no attempt is run."""

    def on_event(self, view: RunView, event: StrategyEvent) -> SkeletonState:
        """Skip the attempt: the baseline measurement is followed by the cancellation."""
        state = super().on_event(view, event)
        return state.model_copy(update={"phase": "cancel"}) if state.phase == "start" else state


class ScriptedClock(ProbedClock):
    """A fake clock that runs an action when the loop makes its n-th sleep."""

    def __init__(self, inner: VirtualClock) -> None:
        """Wrap the loop's clock."""
        super().__init__(inner)
        self.actions: dict[int, Callable[[], None]] = {}

    async def sleep(self, seconds: float) -> None:
        """Advance time, then run the action scheduled for this sleep, if any."""
        await super().sleep(seconds)
        action = self.actions.get(len(self.sleeps))
        if action is not None:
            action()


class Steers:
    """An in-memory steer text store."""

    def __init__(self) -> None:
        self.texts: dict[str, str] = {}

    def put(self, ref: ArtifactRef, text: str) -> None:
        """Keep the text under its digest."""
        self.texts[ref.digest] = text


def _ignore(transition: object) -> None:
    del transition


def _channel() -> RuntimeRunControlChannel:
    return RuntimeRunControlChannel(_ignore)


def _config(
    *,
    lease_duration: float = LEASE,
    control_poll_interval: float = 1.0,
    min_sleep: float = 0.05,
    max_dispatches: int = 100_000,
) -> RunLoopConfig:
    return RunLoopConfig(
        host_id="loop",
        lease_duration=lease_duration,
        control_poll_interval=control_poll_interval,
        min_sleep=min_sleep,
        max_dispatches=max_dispatches,
    )


def _host(
    world: World, clock: ProbedClock, channel: RuntimeRunControlChannel | None = None
) -> tuple[Process, CoreRunHost]:
    process = world.runtime()
    controls = None
    if channel is not None:
        controls = RunControlBridge(
            channel, Steers(), stop_result=RunResultProposal(outcome="cancelled", reason="operator")
        )
    host = CoreRunHost(process.shell, process.delivery, clock, controls)
    return process, host


async def test_a_stop_control_ends_the_run_with_the_proposed_result(tmp_path: Path) -> None:
    channel = _channel()
    clock = ScriptedClock(clock_from(1.0))
    clock.actions[2] = channel.request_stop
    with open_skeleton_world(tmp_path, INERT) as world:
        process, host = _host(world, clock, channel)
        channel.request_pause()
        start_core(host, _config())
        outcome = await drive_core(host, _config())
    assert outcome.status == RunStatus.TERMINAL
    assert outcome.result == RunResultProposal(outcome="cancelled", reason="operator")
    assert process.shell.record.envelope.core.run.result == outcome.result


async def test_the_deadline_ends_an_otherwise_idle_paused_run(tmp_path: Path) -> None:
    channel = _channel()
    clock = ProbedClock(clock_from(1.0))
    with open_skeleton_world(tmp_path, INERT) as world:
        process, host = _host(world, clock, channel)
        channel.request_pause()
        start_core(host, _config(lease_duration=5000.0, control_poll_interval=300.0))
        outcome = await drive_core(
            host, _config(lease_duration=5000.0, control_poll_interval=300.0)
        )
        deadline = process.shell.record.envelope.core.run.deadline_at
    assert outcome.status == RunStatus.TERMINAL
    assert outcome.result == RunResultProposal(outcome="cancelled", reason="run deadline reached")
    assert clock.at >= deadline


@settings(
    max_examples=5,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@example(poll=200.0, min_sleep=1.0)
@example(poll=166.0, min_sleep=1.0)
@given(
    poll=st.floats(min_value=25.0, max_value=300.0),
    min_sleep=st.floats(min_value=0.01, max_value=20.0),
)
async def test_an_idle_run_is_woken_a_bounded_number_of_times(
    poll: float, min_sleep: float
) -> None:
    """Idle time is spent sleeping: every sleep lasts at least ``min_sleep``, and the number
    of sleeps over the run's horizon is bounded by the configured intervals.
    """
    with (
        tempfile.TemporaryDirectory() as directory,
        open_skeleton_world(Path(directory), INERT) as world,
    ):
        channel = _channel()
        clock = ProbedClock(clock_from(1.0))
        process, host = _host(world, clock, channel)
        channel.request_pause()
        config = _config(control_poll_interval=poll, min_sleep=min_sleep)
        start_core(host, config)
        outcome = await drive_core(host, config)
        horizon = process.shell.record.envelope.core.run.deadline_at - 1.0
    assert outcome.status == RunStatus.TERMINAL
    assert all(seconds >= min_sleep for seconds in clock.sleeps)
    # Wakes come from the control poll and the lease renewal, each at most that often; a
    # partial interval at the horizon costs one wake (the ceilings), and a poll that falls
    # just after a lease wake is a second, short wake (the poll term counts twice).
    assert (
        len(clock.sleeps)
        <= 2 * math.ceil(horizon / max(poll, min_sleep))
        + math.ceil(horizon / max(LEASE / 3, min_sleep))
        + 3
    )


async def _measure_a_job_running(tmp_path: Path, runtime: float) -> tuple[World, Process, bool]:
    """Run the baseline measurement of a job that takes ``runtime`` clock seconds, then cancel.

    Returns the world, the process and whether the run reached its terminal status.
    """
    clock = ProbedClock(clock_from(1.0))
    with open_skeleton_world(tmp_path, BaselineOnly(), timed=(clock, runtime)) as world:
        process = world.runtime()
        process.shell.start("host-a", now_at=1.0, lease_duration=LEASE)
        try:
            assert await drive(process, start=1.0, clock=clock) is None
        except StalledError:
            return world, process, False
        return world, process, True


@pytest.mark.parametrize("runtime", [45.0, 300.0])
async def test_a_running_job_is_polled_at_the_observe_interval(
    tmp_path: Path, runtime: float
) -> None:
    """A job that runs ``runtime`` seconds is polled once per ``observe_interval`` while it runs.

    Bound: one poll per interval over the runtime, plus the first poll (due as soon as the
    job is submitted), the one that sees the end, and the two the submission itself makes
    (whether to submit, then its own view). The answers never fail, so the backoff never
    applies. A loop that polled on every wake would exceed it by orders of magnitude.
    """
    interval = Limits().observe_interval
    world, process, _ = await _measure_a_job_running(tmp_path, runtime)
    core = process.shell.record.envelope.core
    assert [job.status for job in core.evaluation.jobs] == ["succeeded"]
    assert len(world.cluster.submissions) == 1
    assert runtime / interval - 1 <= len(world.polls) <= runtime / interval + 4


@pytest.mark.parametrize("leases", [1, 3, 10])
async def test_a_dispatch_longer_than_the_lease_does_not_lose_the_lease(
    tmp_path: Path, leases: int
) -> None:
    """One dispatch (an agent turn) can run for many lease durations; the loop renews beside it.

    The submission stands for such a dispatch: run-clock time passes while it is in flight and
    the loop body does not run. Without renewal the next commit is stamped after the lease
    expired and the run dies with a fence conflict.
    """
    clock = ProbedClock(clock_from(1.0))

    async def lease_durations_pass() -> None:
        for _ in range(leases * 4):
            # Real virtual time, not a jump of ``clock.at``: the heartbeat renews as the
            # timeline passes each third of the lease, as it would beside a real turn.
            await clock.pass_time(LEASE / 4)

    with open_skeleton_world(tmp_path, BaselineOnly(), timed=(clock, 0.0)) as world:
        world.during_submit = lease_durations_pass
        process, host = _host(world, clock)
        start_core(host, _config())
        outcome = await drive_core(host, _config())
        assert process.shell.holds_lease(now_at=clock.now())
    assert outcome.status == RunStatus.TERMINAL


@pytest.mark.parametrize("lease_durations_first", [0, 1, 5])
async def test_a_stop_during_a_turn_that_never_ends_cancels_it_once_without_waiting(
    tmp_path: Path, lease_durations_first: int
) -> None:
    """A stop that arrives while one dispatch (an agent turn) is in flight is acted on then.

    The submission stands for a turn that never ends on its own. The loop reads controls
    between drains, so before the fix it never saw the stop: the run ended only when the
    host's grace bound cancelled it. Now the loop cancels the dispatch once and ends the
    run as stopped, with no run-clock time spent waiting for a bound.
    """
    clock = ProbedClock(clock_from(1.0))
    channel = _channel()
    cancellations: list[str] = []

    async def turn_that_never_ends() -> None:
        for _ in range(lease_durations_first * 4):
            # Real virtual time, not a jump of ``clock.at``: the heartbeat renews as the
            # timeline passes each third of the lease, as it would beside a real turn.
            await clock.pass_time(LEASE / 4)
        channel.request_stop()
        try:
            # Never ends on its own as far as the run is concerned. The bound counts event
            # loop turns, not time: a loop that ignores the stop lets it elapse and fails.
            for _ in range(_TURN_BOUND):
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            cancellations.append("cancelled")
            raise

    with open_skeleton_world(tmp_path, BaselineOnly(), timed=(clock, 0.0)) as world:
        world.during_submit = turn_that_never_ends
        _, host = _host(world, clock, channel)
        start_core(host, _config())
        with pytest.raises(RunStopped):
            await drive_core(host, _config())
    assert cancellations == ["cancelled"]
    # The only run-clock time that passed is what the turn itself spent before the stop
    # (the loop's control polls inside it); acting on the stop waited for nothing.
    assert clock.at == pytest.approx(1.0 + lease_durations_first * LEASE)


async def test_a_run_with_a_long_measurement_closes_after_the_job_ends(tmp_path: Path) -> None:
    _, _, terminal = await _measure_a_job_running(tmp_path, 45.0)
    assert terminal


async def test_a_run_nothing_can_wake_is_reported_stalled(tmp_path: Path) -> None:
    with open_skeleton_world(tmp_path, INERT) as world:
        _, host = _host(world, ProbedClock(clock_from(1.0)))
        start_core(host, _config())
        with pytest.raises(RunStalledError, match="stalled"):
            await drive_core(host, _config())


class WaitsUntil(SkeletonStrategy):
    """Holds all work back until a run-clock time, then cancels the run."""

    until: float = 0.0

    def decide(self, view: RunView) -> Proposal[SkeletonState]:
        """Ask to be woken at ``until``; from then on stop the run."""
        if view.run.now_at < self.until:
            return Proposal(state=self.state, decisions=(), wake_at=self.until)
        cancelling = self.bind(self.state.model_copy(update={"phase": "cancel"}))
        return SkeletonStrategy.decide(cancelling, view)


@settings(max_examples=5, derandomize=True, deadline=None)
@example(until=7.0)
@given(until=st.floats(min_value=2.0, max_value=900.0))
async def test_a_strategy_that_waits_for_a_time_is_woken_then_and_not_reported_stalled(
    until: float,
) -> None:
    waiting = WaitsUntil(state=SkeletonState(schema_version=1, phase="done"), until=until)
    clock = ProbedClock(clock_from(1.0))
    with (
        tempfile.TemporaryDirectory() as directory,
        open_skeleton_world(Path(directory), waiting) as world,
    ):
        _, host = _host(world, clock)
        start_core(host, _config())
        outcome = await drive_core(host, _config())
    assert outcome.status == RunStatus.TERMINAL
    assert outcome.result == RunResultProposal(outcome="cancelled", reason="attempt cancelled")
    # The loop slept toward the wanted time and was awake at or after it, never before.
    assert until <= clock.at < until + _config().lease_duration


async def test_restart_mid_run_resumes_from_durable_state(tmp_path: Path) -> None:
    """A shell stopped by the dispatch cap after its first request is replaced by a new
    process over the same disk, which finishes the run.
    """
    with open_skeleton_world(tmp_path, SkeletonStrategy.cancelled()) as world:
        clock = ProbedClock(clock_from(1.0))
        first, host = _host(world, clock)
        start_core(host, _config(max_dispatches=1))
        with pytest.raises(DispatchCapExceededError) as capped:
            await drive_core(host, _config(max_dispatches=1))
        assert sum(capped.value.kinds.values()) >= 1
        assert first.shell.record.envelope.core.run.status != RunStatus.TERMINAL
        clock.at += LEASE + 1.0
        second, resumed = _host(world, clock)
        start_core(resumed, _config())
        outcome = await drive_core(resumed, _config())
    assert outcome.status == RunStatus.TERMINAL
    assert outcome.result is not None
    assert outcome.result.outcome == "cancelled"
    assert second.shell.record.envelope.core.revision > first.shell.record.envelope.core.revision


@pytest.mark.parametrize("elapsed", [0.0, 7.0, LEASE - 0.5])
async def test_a_restart_waits_in_clock_time_for_a_dead_hosts_lease(
    tmp_path: Path, elapsed: float
) -> None:
    with open_skeleton_world(tmp_path, INERT) as world:
        clock = ProbedClock(clock_from(1.0))
        first, crashed = _host(world, clock)
        start_core(crashed, _config())
        del first  # the process dies holding its lease, which it never releases
        clock.at += elapsed
        _second, resumed = _host(world, clock)
        started_at = clock.at

        await start_core_awaiting_lease(resumed, _config())

    # The wait ends at the first poll at or after the lease's expiry, never earlier.
    waited = clock.at - started_at
    assert LEASE - elapsed <= waited < LEASE - elapsed + 1.0


async def test_a_restart_gives_up_after_one_lease_while_another_host_renews_it(
    tmp_path: Path,
) -> None:
    with open_skeleton_world(tmp_path, INERT) as world:
        clock = ProbedClock(clock_from(1.0))
        holder, live = _host(world, clock)
        start_core(live, _config())

        class RenewingClock(ProbedClock):
            async def sleep(self, seconds: float) -> None:
                await super().sleep(seconds)
                holder.shell.renew(now_at=self.at, lease_duration=LEASE)

        waiting = RenewingClock(clock_from(clock.at))
        _other, restarted = _host(world, waiting)
        with pytest.raises(LeaseUnavailableError, match="still held"):
            await start_core_awaiting_lease(restarted, _config())

    assert LEASE <= sum(waiting.sleeps) <= LEASE + 1.0


async def test_a_released_lease_is_free_at_once(tmp_path: Path) -> None:
    with open_skeleton_world(tmp_path, INERT) as world:
        clock = ProbedClock(clock_from(1.0))
        first, host = _host(world, clock)
        start_core(host, _config())

        first.shell.release_lease(now_at=clock.now())
        _second, restarted = _host(world, clock)
        await start_core_awaiting_lease(restarted, _config())

    assert clock.sleeps == []


_OPERATIONS = st.lists(
    st.one_of(
        st.just("pause"),
        st.just("resume"),
        st.just("stop"),
        st.text(min_size=1, max_size=5).map(lambda text: f"steer:{text}"),
    ),
    max_size=12,
)


@given(operations=_OPERATIONS)
def test_the_control_bridge_emits_one_ordered_event_per_change(operations: list[str]) -> None:
    channel = _channel()
    steers = Steers()
    bridge = RunControlBridge(
        channel, steers, stop_result=RunResultProposal(outcome="cancelled", reason="operator")
    )
    core = initial_state()
    events = []
    for operation in operations:
        if operation == "pause":
            channel.request_pause()
        elif operation == "resume":
            channel.resume()
        elif operation == "stop":
            channel.request_stop()
        else:
            channel.queue_steer(operation.removeprefix("steer:"))
        events.extend(bridge.poll(core, now_at=1.0))
    identities = [event.control.control_id.root for event in events]
    assert identities == [f"control-{n}" for n in range(1, len(events) + 1)]
    actions = [event.control.action for event in events]
    assert actions.count("stop") <= 1
    if "stop" in actions:
        assert all(action == "steer" for action in actions[actions.index("stop") + 1 :])
    levels = [action for action in actions if action in ("pause", "resume")]
    assert all(a != b for a, b in itertools.pairwise(levels))
    assert not levels or levels[0] == "pause"
    for event in events:
        assert (event.result is not None) == (event.control.action == "stop")
        if event.control.action == "steer":
            assert event.control.artifact is not None
            assert event.control.artifact.digest in steers.texts
