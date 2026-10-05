"""The production run loop (``vs_runtime._core_run``) over the real shell and executors.

Time is a fake clock whose ``sleep`` advances a counter, so nothing waits. The inert
strategy proposes nothing: the run stays open until a control or the deadline ends it.
"""

from __future__ import annotations

import itertools
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.fake_run_clock import FakeRunClock
from tests.support.skeleton_strategy import SkeletonState, SkeletonStrategy
from tests.support.skeleton_world import (
    LEASE,
    Process,
    StalledError,
    World,
    drive,
    open_skeleton_world,
)

from vs_core.api import ArtifactRef, Limits, RunResultProposal, RunStatus
from vs_core.testing.builders import initial_state
from vs_runtime.api.core import (
    CoreRunHost,
    DispatchCapExceededError,
    RunControlBridge,
    RunLoopConfig,
    RunStalledError,
    drive_core,
    start_core,
)
from vs_runtime.api.infrastructure import RuntimeRunControlChannel

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import RunView, StrategyEvent

INERT = SkeletonStrategy(state=SkeletonState(schema_version=1, phase="done"), measured=False)


class BaselineOnly(SkeletonStrategy):
    """Measure the baseline, then cancel the run: no attempt is run."""

    def on_event(self, view: RunView, event: StrategyEvent) -> SkeletonState:
        """Skip the attempt: the baseline measurement is followed by the cancellation."""
        state = super().on_event(view, event)
        return state.model_copy(update={"phase": "cancel"}) if state.phase == "start" else state


@dataclass
class ScriptedClock(FakeRunClock):
    """A fake clock that runs an action when the loop makes its n-th sleep."""

    actions: dict[int, Callable[[], None]] = field(default_factory=dict)

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
    world: World, clock: FakeRunClock, channel: RuntimeRunControlChannel | None = None
) -> tuple[Process, CoreRunHost]:
    process = world.runtime()
    controls = None
    if channel is not None:
        controls = RunControlBridge(
            channel, Steers(), stop_result=RunResultProposal(outcome="cancelled", reason="operator")
        )
    host = CoreRunHost(process.shell, process.delivery, clock, controls)
    return process, host


@pytest.mark.asyncio
async def test_a_stop_control_ends_the_run_with_the_proposed_result(tmp_path: Path) -> None:
    channel = _channel()
    clock = ScriptedClock(at=1.0)
    clock.actions[2] = channel.request_stop
    with open_skeleton_world(tmp_path, INERT) as world:
        process, host = _host(world, clock, channel)
        channel.request_pause()
        start_core(host, _config())
        outcome = await drive_core(host, _config())
    assert outcome.status == RunStatus.TERMINAL
    assert outcome.result == RunResultProposal(outcome="cancelled", reason="operator")
    assert process.shell.record.envelope.core.run.result == outcome.result


@pytest.mark.asyncio
async def test_the_deadline_ends_an_otherwise_idle_paused_run(tmp_path: Path) -> None:
    channel = _channel()
    clock = FakeRunClock(at=1.0)
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
@given(
    poll=st.floats(min_value=25.0, max_value=300.0),
    min_sleep=st.floats(min_value=0.01, max_value=20.0),
)
@pytest.mark.asyncio
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
        clock = FakeRunClock(at=1.0)
        process, host = _host(world, clock, channel)
        channel.request_pause()
        config = _config(control_poll_interval=poll, min_sleep=min_sleep)
        start_core(host, config)
        outcome = await drive_core(host, config)
        horizon = process.shell.record.envelope.core.run.deadline_at - 1.0
    assert outcome.status == RunStatus.TERMINAL
    assert all(seconds >= min_sleep for seconds in clock.sleeps)
    # Wakes come from the control poll and the lease renewal, each at most that often; a
    # partial period at the end of the horizon still costs one wake, hence the ceilings.
    assert len(clock.sleeps) <= (
        math.ceil(horizon / max(poll, min_sleep))
        + math.ceil(horizon / max(LEASE / 3, min_sleep))
        + 3
    )


async def _measure_a_job_running(tmp_path: Path, runtime: float) -> tuple[World, Process, bool]:
    """Run the baseline measurement of a job that takes ``runtime`` clock seconds, then cancel.

    Returns the world, the process and whether the run reached its terminal status.
    """
    clock = FakeRunClock(at=1.0)
    with open_skeleton_world(tmp_path, BaselineOnly(), timed=(clock, runtime)) as world:
        process = world.runtime()
        process.shell.start("host-a", now_at=1.0, lease_duration=LEASE)
        try:
            assert await drive(process, start=1.0, clock=clock) is None
        except StalledError:
            return world, process, False
        return world, process, True


@pytest.mark.parametrize("runtime", [45.0, 300.0])
@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_a_run_with_a_long_measurement_closes_after_the_job_ends(tmp_path: Path) -> None:
    _, _, terminal = await _measure_a_job_running(tmp_path, 45.0)
    assert terminal


@pytest.mark.asyncio
async def test_a_run_nothing_can_wake_is_reported_stalled(tmp_path: Path) -> None:
    with open_skeleton_world(tmp_path, INERT) as world:
        _, host = _host(world, FakeRunClock(at=1.0))
        start_core(host, _config())
        with pytest.raises(RunStalledError, match="stalled"):
            await drive_core(host, _config())


@pytest.mark.asyncio
async def test_restart_mid_run_resumes_from_durable_state(tmp_path: Path) -> None:
    """A shell stopped by the dispatch cap after its first request is replaced by a new
    process over the same disk, which finishes the run.
    """
    with open_skeleton_world(tmp_path, SkeletonStrategy.cancelled()) as world:
        clock = FakeRunClock(at=1.0)
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
