"""The production run loop (``vs_runtime._core_run``) over the real shell and executors.

Time is a fake clock whose ``sleep`` advances a counter, so nothing waits. The inert
strategy proposes nothing: the run stays open until a control or the deadline ends it.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.skeleton_world import (
    LEASE,
    Process,
    StalledError,
    World,
    drive,
    open_skeleton_world,
)

from vs_core.api import (
    AdoptRevision,
    ArtifactRef,
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptsState,
    AttemptView,
    ItemId,
    ReleaseDependency,
    ResourceId,
    RunControlEvent,
    RunResultProposal,
    RunStatus,
    SessionInputReceived,
    StopMode,
    VerifyAdoption,
    WorkspaceMode,
    WorkspacePlan,
    escalate_stop,
)
from vs_core.testing.builders import initial_state, sample_limits
from vs_runtime.api.core import (
    CoreRunHost,
    DispatchCapExceededError,
    LeaseUnavailableError,
    RunControlBridge,
    RunLoopConfig,
    RunOutcome,
    RunStalledError,
    drive_core,
    start_core,
    start_core_awaiting_lease,
)
from vs_runtime.api.infrastructure import (
    RunControlTransition,
    RunControlTransitionKind,
    RuntimeRunControlChannel,
)
from vs_runtime.api.testing import ProbedRunClock
from vs_sim.api import yield_now
from vs_sim.api.testing import SimThreads, clock_from, examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreState
    from vs_sim.api.testing import VirtualClock

#: Event loop turns a stuck fake turn lasts, standing for "longer than anything the run waits".
_TURN_BOUND = 1000

INERT = SkeletonStrategy.idle()
BASELINE_ONLY = SkeletonStrategy.baseline_only()


class ScriptedClock(ProbedRunClock):
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
    return RuntimeRunControlChannel(_ignore, threads=SimThreads())


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
    world: World, clock: ProbedRunClock, channel: RuntimeRunControlChannel | None = None
) -> tuple[Process, CoreRunHost]:
    process = world.runtime()
    controls = None
    if channel is not None:
        controls = RunControlBridge(channel, Steers())
    host = CoreRunHost(process.shell, process.delivery, clock, controls)
    return process, host


@pytest.mark.parametrize("explicit_resume", [False, True])
async def test_reopening_a_finished_run_requires_operator_resume(
    tmp_path: Path, *, explicit_resume: bool
) -> None:
    """Replacing a writer returns its result; only operator Resume reopens admission."""
    clock = ProbedRunClock(clock_from(1.0))
    with open_skeleton_world(tmp_path, BASELINE_ONLY) as world:
        first, host = _host(world, clock)
        start_core(host, _config())
        ended = await drive_core(host, _config())
        assert ended.status == RunStatus.TERMINAL
        first.shell.release_lease(now_at=clock.now())
        replacement = world.runtime()
        bridge = RunControlBridge(_channel(), Steers(), lifts_standing_stop=explicit_resume)
        restarted = CoreRunHost(replacement.shell, replacement.delivery, clock, bridge)
        start_core(restarted, _config())
        outcome = await drive_core(restarted, _config())
        controls = replacement.shell.record.envelope.core.run.controls
        assert any(control.action == "resume" for control in controls) is explicit_resume
        assert outcome.status == RunStatus.TERMINAL
        assert outcome.result == ended.result


async def test_a_stop_control_ends_the_run_with_the_proposed_result(tmp_path: Path) -> None:
    channel = _channel()
    clock = ScriptedClock(clock_from(1.0))
    clock.actions[2] = lambda: channel.request_stop(StopMode.DRAIN)
    with open_skeleton_world(tmp_path, INERT) as world:
        process, host = _host(world, clock, channel)
        channel.pause_for_quota()
        start_core(host, _config())
        outcome = await drive_core(host, _config())
    assert outcome.status == RunStatus.TERMINAL
    assert outcome.result == RunResultProposal(
        outcome="cancelled", reason="stopped: no work was asked for"
    )
    assert process.shell.record.envelope.core.run.result == outcome.result


async def test_the_deadline_ends_an_otherwise_idle_paused_run(tmp_path: Path) -> None:
    channel = _channel()
    clock = ProbedRunClock(clock_from(1.0))
    with open_skeleton_world(tmp_path, INERT) as world:
        process, host = _host(world, clock, channel)
        channel.pause_for_quota()
        start_core(host, _config(lease_duration=5000.0, control_poll_interval=300.0))
        outcome = await drive_core(
            host, _config(lease_duration=5000.0, control_poll_interval=300.0)
        )
        deadline = process.shell.record.envelope.core.run.deadline_at
    assert outcome.status == RunStatus.TERMINAL
    assert outcome.result == RunResultProposal(
        outcome="cancelled", reason="deadline: no work was asked for"
    )
    assert clock.at >= deadline


@settings(
    max_examples=examples(pr=1, nightly=12),
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
        clock = ProbedRunClock(clock_from(1.0))
        process, host = _host(world, clock, channel)
        channel.pause_for_quota()
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


class _PauseTrace:
    """Public control transitions and the dispatch count at every observed boundary."""

    def __init__(self, dispatches: Callable[[], int]) -> None:
        self.dispatches = dispatches
        self.events: list[tuple[RunControlTransitionKind | None, int]] = []

    def record(self, kind: RunControlTransitionKind | None = None) -> None:
        self.events.append((kind, self.dispatches()))

    def changed(self, transition: RunControlTransition) -> None:
        self.record(transition.kind)

    @property
    def transitions(self) -> list[str]:
        return [kind.value for kind, _ in self.events if kind is not None]

    def assert_held(self) -> None:
        """An acknowledged pause dispatches nothing until its matching resume."""
        paused_at = None
        for kind, dispatches in self.events:
            if paused_at is not None:
                assert dispatches == paused_at, (kind, self.events)
            if kind is RunControlTransitionKind.PAUSED:
                assert paused_at is None
                paused_at = dispatches
            elif kind is RunControlTransitionKind.RESUMED:
                assert paused_at is not None, self.events
                paused_at = None
        assert paused_at is None, self.events


class _Operator(ProbedRunClock):
    """Request a quota hold; release after the public acknowledgement and hold."""

    def __init__(
        self, inner: VirtualClock, channel: RuntimeRunControlChannel, trace: _PauseTrace
    ) -> None:
        super().__init__(inner)
        self.channel = channel
        self.trace = trace
        self.pause_at = 0
        self.held_for = 0
        self.paused_waits = 0
        self.waits = 0

    async def sleep(self, seconds: float) -> None:
        """The operator responds to public events, never core's intermediate pause state."""
        self.waits += 1
        self.trace.record()
        if self.waits == self.pause_at:
            self.channel.pause_for_quota()
        elif "paused" in self.trace.transitions and self.channel.pause_requested():
            self.paused_waits += 1
            if self.paused_waits >= self.held_for:
                self.channel.resume()
        await super().sleep(seconds)


async def _operated_run(pause_at: int, held_for: int) -> tuple[_Operator, RunOutcome, list[str]]:
    """The full skeleton run with an internal quota hold and its release."""
    trace = _PauseTrace(lambda: process.shell.dispatched)
    channel = RuntimeRunControlChannel(trace.changed, threads=SimThreads())
    clock = _Operator(clock_from(1.0), channel, trace)
    clock.pause_at, clock.held_for = pause_at, held_for
    with (
        tempfile.TemporaryDirectory() as directory,
        open_skeleton_world(Path(directory), timed=(clock, 20.0)) as world,
    ):
        process, host = _host(world, clock, channel)
        start_core(host, _config())
        outcome = await drive_core(host, _config())
        trace.record()
    return clock, outcome, trace.transitions


#: How the run ends with no pause: it adopts the candidate its one attempt kept.
_UNPAUSED_END = ("success", "adopted: settlement:settle.1")


@settings(max_examples=examples(pr=4, nightly=40), deadline=None)
@pytest.mark.regression
# Pauses during the baseline measurement (the host-b case: a pause before the first step).
@example(pause_at=7, held_for=20)
@example(pause_at=40, held_for=5)
@example(pause_at=4, held_for=1)
@example(pause_at=27, held_for=1)
@example(pause_at=59, held_for=1)
@given(pause_at=st.integers(min_value=1, max_value=60), held_for=st.integers(1, 30))
async def test_a_quota_hold_waits_until_released_and_the_run_then_ends_as_it_would_have(
    pause_at: int, held_for: int
) -> None:
    """An internal quota hold waits, and after release the run ends unchanged.

    The search keeps deciding while the run is paused (a measurement in flight still
    finishes); core takes none of that work until the resume, and the search is never told
    its work was refused. Whenever core commits the pause with nothing running, the channel
    reports it landed (``paused``), so the operator sees Paused, then Running on the resume.
    An adoption already dispatched may end the run before the pause lands; that pending
    pause has no acknowledgement and is never resumed.
    """
    clock, outcome, transitions = await _operated_run(pause_at, held_for)
    assert outcome.status == RunStatus.TERMINAL
    assert outcome.result is not None
    assert (outcome.result.outcome, outcome.result.reason) == _UNPAUSED_END
    clock.trace.assert_held()
    if "paused" in transitions:
        assert clock.paused_waits >= held_for
        assert transitions[:2] == ["pause_requested", "paused"]
        assert transitions[-1] == "resumed"
    else:
        assert transitions in ([], ["pause_requested"])


async def test_an_in_flight_adoption_finishes_while_a_pause_is_pending(tmp_path: Path) -> None:
    """A graceful pause never abandons adoption, nor acknowledges a run that already ended.

    The adoption request in flight is the ``AdoptRevision``: its report carries the publication,
    so no ``VerifyAdoption`` follows it in this world (see the test below), and the pause rule
    does not look at the kind of an adoption request.
    """
    trace = _PauseTrace(lambda: process.shell.dispatched)
    channel = RuntimeRunControlChannel(trace.changed, threads=SimThreads())

    class AdoptionClock(ProbedRunClock):
        requested = False

        async def sleep(self, seconds: float) -> None:
            trace.record()
            if not self.requested and any(
                isinstance(request, AdoptRevision) for request in process.shell.in_flight
            ):
                self.requested = True
                channel.pause_for_quota()
            await super().sleep(seconds)

    clock = AdoptionClock(clock_from(1.0))
    with open_skeleton_world(tmp_path) as world:
        process, host = _host(world, clock, channel)
        start_core(host, _config())
        outcome = await drive_core(host, _config())
        trace.record()
    assert clock.requested, "the pause must arrive while the adoption request is in flight"
    assert outcome.status == RunStatus.TERMINAL
    assert outcome.result is not None
    assert (outcome.result.outcome, outcome.result.reason) == _UNPAUSED_END
    assert trace.transitions == ["pause_requested"]
    trace.assert_held()


async def test_a_succeeded_adoption_that_carries_its_publication_is_not_verified_again(
    tmp_path: Path,
) -> None:
    """The report of an adoption says where it published, so core completes on it alone.

    A ``VerifyAdoption`` follows an adoption only when its answer lacks a publication or is
    unknown. The workspace executor of this world attaches the publication to the report of every
    adoption it succeeds, so the run adopts with one request and verifies without a second.
    """
    clock = ProbedRunClock(clock_from(1.0))
    with open_skeleton_world(tmp_path) as world:
        process, host = _host(world, clock)
        start_core(host, _config())
        outcome = await drive_core(host, _config())
        core = process.shell.record.envelope.core
    assert outcome.status == RunStatus.TERMINAL
    assert core.settlement.adoption is not None
    assert core.settlement.adoption.verified
    requests = [row.request for row in core.records.records]
    assert any(isinstance(request, AdoptRevision) for request in requests)
    assert not any(isinstance(request, VerifyAdoption) for request in requests)


async def _measure_a_job_running(tmp_path: Path, runtime: float) -> tuple[World, Process, bool]:
    """Run the baseline measurement of a job that takes ``runtime`` clock seconds, then cancel.

    Returns the world, the process and whether the run reached its terminal status.
    """
    clock = ProbedRunClock(clock_from(1.0))
    with open_skeleton_world(tmp_path, BASELINE_ONLY, timed=(clock, runtime)) as world:
        process = world.runtime()
        process.shell.start("host-a", now_at=1.0, lease_duration=LEASE)
        try:
            assert await drive(process, start=1.0, clock=clock) is None
        except StalledError:
            return world, process, False
        return world, process, True


@pytest.mark.parametrize("runtime", [45.0, 150.0])
async def test_a_running_job_is_polled_at_the_observe_interval(
    tmp_path: Path, runtime: float
) -> None:
    """A job that runs ``runtime`` seconds is polled once per ``observe_interval`` while it runs.

    Bound: one poll per interval over the runtime, plus the first poll (due as soon as the
    job is submitted), the one that sees the end, and the two the submission itself makes
    (whether to submit, then its own view). The answers never fail, so the backoff never
    applies. A loop that polled on every wake would exceed it by orders of magnitude.
    """
    interval = sample_limits().observe_interval
    world, process, terminal = await _measure_a_job_running(tmp_path, runtime)
    assert terminal, "the run closes once the job ends"
    core = process.shell.record.envelope.core
    assert [job.status for job in core.evaluation.jobs] == ["succeeded"]
    assert len(world.cluster.submissions) == 1
    assert runtime / interval - 1 <= len(world.polls) <= runtime / interval + 4


# Eight lease durations is the most that fit in the 880 s a skeleton measurement may take.
@pytest.mark.parametrize("leases", [1, 8])
async def test_a_dispatch_longer_than_the_lease_does_not_lose_the_lease(
    tmp_path: Path, leases: int
) -> None:
    """One dispatch (an agent turn) can run for many lease durations; the loop renews beside it.

    The submission stands for such a dispatch: run-clock time passes while it is in flight and
    the loop body does not run. Without renewal the next commit is stamped after the lease
    expired and the run dies with a fence conflict.
    """
    clock = ProbedRunClock(clock_from(1.0))

    async def lease_durations_pass() -> None:
        for _ in range(leases * 4):
            # Real virtual time, not a jump of ``clock.at``: the heartbeat renews as the
            # timeline passes each third of the lease, as it would beside a real turn.
            await clock.pass_time(LEASE / 4)

    with open_skeleton_world(tmp_path, BASELINE_ONLY, timed=(clock, 0.0)) as world:
        world.during_submit = lease_durations_pass
        process, host = _host(world, clock)
        start_core(host, _config())
        outcome = await drive_core(host, _config())
        assert process.shell.holds_lease(now_at=clock.now())
    assert outcome.status == RunStatus.TERMINAL


@pytest.mark.parametrize("lease_durations_first", [0, 2])
async def test_a_stop_during_a_submission_waits_for_it_and_closes_the_run(
    tmp_path: Path, lease_durations_first: int
) -> None:
    """A stop that arrives while a submission is in flight waits for it, then closes the run.

    A submission is a request core cannot cancel, so the loop does not abandon it at the
    stop (that left the run record closing and every owned job uncancelled until the host
    exited). It drains the submission, cancels what the run owns through core, and ends
    terminal. The wait is bounded by the host's grace period, which is not reached here.
    """
    clock = ProbedRunClock(clock_from(1.0))
    channel = _channel()
    stop_seen_in_flight: list[bool] = []

    async def slow_submission() -> None:
        for _ in range(lease_durations_first * 4):
            # Real virtual time, not a jump of ``clock.at``: the heartbeat renews as the
            # timeline passes each third of the lease, as it would beside a real turn.
            await clock.pass_time(LEASE / 4)
        channel.request_stop(StopMode.DRAIN)
        for _ in range(_TURN_BOUND):
            await yield_now()
        stop_seen_in_flight.append(True)

    with open_skeleton_world(tmp_path, BASELINE_ONLY, timed=(clock, 0.0)) as world:
        world.during_submit = slow_submission
        _, host = _host(world, clock, channel)
        start_core(host, _config())
        outcome = await drive_core(host, _config())
    assert outcome.status == RunStatus.TERMINAL
    assert stop_seen_in_flight == [True]
    # Draining and closing take a few control polls, far inside the host's 60 s grace.
    spent_before_stop = 1.0 + lease_durations_first * LEASE
    assert spent_before_stop <= clock.at <= spent_before_stop + 60.0


async def test_a_run_nothing_can_wake_is_reported_stalled(tmp_path: Path) -> None:
    with open_skeleton_world(tmp_path, INERT) as world:
        _, host = _host(world, ProbedRunClock(clock_from(1.0)))
        start_core(host, _config())
        with pytest.raises(RunStalledError, match="stalled"):
            await drive_core(host, _config())


async def test_a_stalled_run_with_no_open_intent_still_names_what_is_unfinished(
    tmp_path: Path,
) -> None:
    """``open records []`` is never the whole report: core says it waits on nothing."""
    with open_skeleton_world(tmp_path, INERT) as world:
        _, host = _host(world, ProbedRunClock(clock_from(1.0)))
        start_core(host, _config())
        with pytest.raises(
            RunStalledError,
            match=r"open records \[\]; core waits on nothing, so the strategy is the one that has "
            r"stopped proposing; unfinished attempts \[.*\], sessions \[.*\], continuations \[",
        ):
            await drive_core(host, _config())


async def test_a_stalled_run_reports_what_the_host_knows_about_the_cause(tmp_path: Path) -> None:
    with open_skeleton_world(tmp_path, INERT) as world:
        _, host = _host(world, ProbedRunClock(clock_from(1.0)))
        start_core(host, _config())
        with pytest.raises(RunStalledError, match=r"open records \[\].*; a submission nobody told"):
            await drive_core(host, _config(), stall_notes=lambda: ["a submission nobody told"])


@dataclasses.dataclass
class StuckClosingWorld(World):
    """A world whose run starts with an attempt that is closing and waits for a job's release."""

    def initial(self) -> CoreState:
        """The run's first state, with the closing attempt added."""
        fresh = super().initial()
        stuck = AttemptView(
            attempt_id=AttemptId(root="attempt-1"),
            item_id=ItemId(root="item"),
            generation=0,
            phase=AttemptPhase.CLOSING,
            workspace=WorkspacePlan(
                mode=WorkspaceMode.EXCLUSIVE_ROOT, base=fresh.run.facts.baseline
            ),
            budget=AttemptBudget(),
            release_dependencies=(
                ReleaseDependency(kind="job", identity=ResourceId(root="job-1")),
            ),
        )
        return fresh.model_copy(update={"attempts": AttemptsState(attempts=(stuck,))})


async def test_a_stalled_run_names_the_release_a_closing_attempt_waits_for(tmp_path: Path) -> None:
    """The cause is the attempt stuck closing, not only that no intent is open."""
    with open_skeleton_world(tmp_path, INERT) as opened:
        world = StuckClosingWorld(
            **{f.name: getattr(opened, f.name) for f in dataclasses.fields(opened)}
        )
        _, host = _host(world, ProbedRunClock(clock_from(1.0)))
        start_core(host, _config())
        with pytest.raises(RunStalledError, match=r"attempt-1 closing on \['job:job-1'\]"):
            await drive_core(host, _config())


@settings(max_examples=examples(pr=2, nightly=8), derandomize=True, deadline=None)
@example(until=7.0)
@given(until=st.floats(min_value=2.0, max_value=900.0))
async def test_a_strategy_that_waits_for_a_time_is_woken_then_and_not_reported_stalled(
    until: float,
) -> None:
    waiting = SkeletonStrategy.waiting_until(until)
    clock = ProbedRunClock(clock_from(1.0))
    with (
        tempfile.TemporaryDirectory() as directory,
        open_skeleton_world(Path(directory), waiting) as world,
    ):
        _, host = _host(world, clock)
        start_core(host, _config())
        outcome = await drive_core(host, _config())
    assert outcome.status == RunStatus.TERMINAL
    assert outcome.result == RunResultProposal(outcome="failure", reason="no-winner: woken")
    # The loop slept toward the wanted time and was awake at or after it, never before.
    assert until <= clock.at < until + _config().lease_duration


async def test_restart_mid_run_resumes_from_durable_state(tmp_path: Path) -> None:
    """A shell stopped by the dispatch cap after its first request is replaced by a new
    process over the same disk, which finishes the run.

    The run is waiting on its baseline measurement when the first process dies, so the
    request in flight at the cap is one the new process can look up and carry on.
    """
    channel = _channel()
    with open_skeleton_world(tmp_path, SkeletonStrategy.baseline_probe()) as world:
        clock = ProbedRunClock(clock_from(1.0))
        first, host = _host(world, clock)
        start_core(host, _config(max_dispatches=1))
        with pytest.raises(DispatchCapExceededError) as capped:
            await drive_core(host, _config(max_dispatches=1))
        assert sum(capped.value.kinds.values()) >= 1
        assert first.shell.record.envelope.core.run.status != RunStatus.TERMINAL
        clock.at += LEASE + 1.0
        second, resumed = _host(world, clock, channel)
        channel.request_stop(StopMode.CANCEL)
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
        clock = ProbedRunClock(clock_from(1.0))
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
        clock = ProbedRunClock(clock_from(1.0))
        holder, live = _host(world, clock)
        start_core(live, _config())

        class RenewingClock(ProbedRunClock):
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
        clock = ProbedRunClock(clock_from(1.0))
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
        st.sampled_from(StopMode).map(lambda mode: f"stop:{mode.value}"),
        st.text(min_size=1, max_size=5).map(lambda text: f"steer:{text}"),
    ),
    max_size=12,
)


@settings(max_examples=examples(pr=25, nightly=200), deadline=None)
@given(operations=_OPERATIONS)
def test_the_control_bridge_emits_one_ordered_event_per_change(operations: list[str]) -> None:
    channel = _channel()
    steers = Steers()
    bridge = RunControlBridge(channel, steers)
    core = initial_state()
    events = []
    for operation in operations:
        if operation == "pause":
            channel.pause_for_quota()
        elif operation == "resume":
            channel.resume()
        elif operation.startswith("stop:"):
            channel.request_stop(StopMode(operation.removeprefix("stop:")))
        else:
            channel.queue_steer(operation.removeprefix("steer:"))
        events.extend(bridge.poll(core, now_at=1.0))
    inputs = [event.input for event in events if isinstance(event, SessionInputReceived)]
    events = [event for event in events if isinstance(event, RunControlEvent)]
    identities = [event.control.control_id.root for event in events]
    assert identities == [f"control-{n}" for n in range(1, len(events) + 1)]
    actions = [event.control.action for event in events]
    requested = [StopMode(op.removeprefix("stop:")) for op in operations if op.startswith("stop:")]
    stops = [event.control.mode for event in events if event.control.action == "stop"]
    # A stop only escalates: at most one drain, then at most one cancel, and nothing after it.
    assert stops in ([], [StopMode.DRAIN], [StopMode.CANCEL], [StopMode.DRAIN, StopMode.CANCEL])
    # The strongest stop the run records is the strongest one asked for.
    strongest = None
    for mode in requested:
        strongest = escalate_stop(strongest, mode)
    assert (stops[-1] if stops else None) == strongest
    if "stop" in actions:
        after = actions[actions.index("stop") + 1 :]
        assert all(action in ("steer", "stop") for action in after)
    levels = [action for action in actions if action in ("pause", "resume")]
    assert all(a != b for a, b in itertools.pairwise(levels))
    assert not levels or levels[0] == "pause"
    for event in events:
        # The search proposes the run's result; a control never carries one.
        assert event.result is None
        assert (event.control.mode is not None) == (event.control.action == "stop")
        if event.control.action == "steer":
            assert event.control.artifact is not None
            assert event.control.artifact.digest in steers.texts
    # Every steer becomes exactly one run-wide next-turn input, in the same order.
    assert [item.artifact for item in inputs] == [
        event.control.artifact for event in events if event.control.action == "steer"
    ]
    assert len({item.input_id for item in inputs}) == len(inputs)
