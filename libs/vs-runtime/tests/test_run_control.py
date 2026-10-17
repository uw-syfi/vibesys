"""Public contract tests for cooperative run control."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    ControlId,
    ControlInput,
    RunControlEvent,
    SessionInputReceived,
    StopMode,
    initial_state,
    step,
)
from vs_runtime.api.core import RunControlBridge
from vs_runtime.api.infrastructure import (
    CapacityWake,
    PauseWake,
    RunControlTransition,
    RunControlTransitionKind,
    RunStopped,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import HANG_GUARD_S, SimThreads, wait_or_fail


def test_steering_is_ordered_and_drained_exactly_once() -> None:
    events = FakeRunControlEventSink()
    control = create_run_control_channel(events, threads=SimThreads())

    control.queue_steer("focus on latency")
    control.queue_steer("check for reward hacking")

    assert [steer.text for steer in control.take_pending_steer()] == [
        "focus on latency",
        "check for reward hacking",
    ]
    assert control.take_pending_steer() == []
    assert [transition.kind for transition in events.transitions] == [
        RunControlTransitionKind.STEER_QUEUED,
        RunControlTransitionKind.STEER_QUEUED,
    ]
    assert [transition.text for transition in events.transitions] == [
        "focus on latency",
        "check for reward hacking",
    ]


def test_pause_parks_at_a_boundary_until_resume() -> None:
    threads = SimThreads()
    paused = threads.event()
    released: list[bool] = []
    events = FakeRunControlEventSink(
        on_transition=lambda transition: (
            paused.set() if transition.kind is RunControlTransitionKind.PAUSED else None
        )
    )
    control = create_run_control_channel(events, threads=threads)
    control.request_pause()

    def boundary() -> None:
        control.wait_while_paused()
        released.append(True)

    def main() -> None:
        waiter = threads.spawn(boundary, name="boundary", daemon=False)
        wait_or_fail(paused, "the pause at the boundary")
        assert released == []
        control.resume()
        waiter.join(HANG_GUARD_S)

    threads.run(main)

    assert released == [True]
    assert [transition.kind for transition in events.transitions] == [
        RunControlTransitionKind.PAUSE_REQUESTED,
        RunControlTransitionKind.PAUSED,
        RunControlTransitionKind.RESUMED,
    ]


def test_stop_releases_a_paused_boundary_and_unwinds() -> None:
    threads = SimThreads()
    paused = threads.event()
    events = FakeRunControlEventSink(
        on_transition=lambda transition: (
            paused.set() if transition.kind is RunControlTransitionKind.PAUSED else None
        )
    )
    control = create_run_control_channel(events, threads=threads)
    control.request_pause()
    raised: list[BaseException] = []

    def wait_at_boundary() -> None:
        try:
            control.wait_while_paused()
        except RunStopped as error:
            raised.append(error)

    def main() -> None:
        waiter = threads.spawn(wait_at_boundary, name="boundary", daemon=False)
        wait_or_fail(paused, "the pause at the boundary")
        control.request_stop(StopMode.DRAIN)
        waiter.join(HANG_GUARD_S)

    threads.run(main)

    assert [type(error) for error in raised] == [RunStopped]
    assert [transition.kind for transition in events.transitions] == [
        RunControlTransitionKind.PAUSE_REQUESTED,
        RunControlTransitionKind.PAUSED,
        RunControlTransitionKind.STOP_REQUESTED,
        RunControlTransitionKind.STOPPED,
    ]


def test_stop_lands_on_every_boundary_until_resume_cancels_it() -> None:
    events = FakeRunControlEventSink()
    control = create_run_control_channel(events, threads=SimThreads())
    control.request_stop(StopMode.DRAIN)

    with pytest.raises(RunStopped):
        control.raise_if_stopped()
    with pytest.raises(RunStopped):
        control.raise_if_stopped()

    control.resume()
    control.raise_if_stopped()
    assert [transition.kind for transition in events.transitions] == [
        RunControlTransitionKind.STOP_REQUESTED,
        RunControlTransitionKind.STOPPED,
        RunControlTransitionKind.STOPPED,
        RunControlTransitionKind.RESUMED,
    ]


def test_consumed_steering_records_the_landing_invocation() -> None:
    events = FakeRunControlEventSink()
    control = create_run_control_channel(events, threads=SimThreads())

    control.notify_steer_consumed(
        agent_kind="implementer",
        round_label="round-1",
        execution_id="execution-1",
    )

    assert events.transitions == [
        RunControlTransition(
            kind=RunControlTransitionKind.STEER_CONSUMED,
            agent_kind="implementer",
            round_label="round-1",
            execution_id="execution-1",
        )
    ]


def test_transition_contract_rejects_unknown_fields_and_is_immutable() -> None:
    with pytest.raises(ValidationError):
        RunControlTransition.model_validate({"kind": "paused", "unexpected": "value"})

    transition = RunControlTransition(kind=RunControlTransitionKind.PAUSED)
    with pytest.raises(ValidationError):
        transition.__setattr__("text", "changed")


def test_a_bounded_wait_that_times_out_resumes_the_run_itself() -> None:
    events = FakeRunControlEventSink()
    threads = SimThreads()
    control = create_run_control_channel(events, threads=threads)
    control.request_pause()

    resumed = threads.run(lambda: control.wait_resumed(0.0))

    assert resumed is PauseWake.ELAPSED
    assert not control.pause_requested()
    assert [transition.kind for transition in events.transitions] == [
        RunControlTransitionKind.PAUSE_REQUESTED,
        RunControlTransitionKind.PAUSED,
        RunControlTransitionKind.RESUMED,
    ]


def test_a_bounded_wait_that_something_resumed_reports_it() -> None:
    threads = SimThreads()
    control = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control.request_pause()
    control.resume()

    assert threads.run(lambda: control.wait_resumed(0.0)) is PauseWake.RESUMED


def test_a_stop_ends_a_bounded_wait_and_is_never_cleared_by_its_timeout() -> None:
    threads = SimThreads()
    control = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control.request_pause()
    control.request_stop(StopMode.DRAIN)

    with pytest.raises(RunStopped):
        threads.run(lambda: control.wait_resumed(0.0))

    assert control.stop_requested()


def test_a_fallback_request_is_taken_once_and_a_plain_resume_clears_a_stale_one() -> None:
    control = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())

    control.resume_with_fallback()
    assert control.consume_fallback_request() is True
    assert control.consume_fallback_request() is False

    control.resume_with_fallback()
    control.resume()
    assert control.consume_fallback_request() is False


def test_resuming_with_the_fallback_releases_a_parked_run_like_a_resume() -> None:
    events = FakeRunControlEventSink()
    control = create_run_control_channel(events, threads=SimThreads())
    control.request_pause()

    control.resume_with_fallback()

    assert not control.pause_requested()
    assert events.transitions[-1].kind is RunControlTransitionKind.RESUMED


@pytest.mark.parametrize(
    ("request_control", "requested", "boundary", "landed"),
    [
        pytest.param(
            "request_stop",
            RunControlTransitionKind.STOP_REQUESTED,
            "raise_if_stopped",
            RunControlTransitionKind.STOPPED,
            id="stop",
        ),
        pytest.param(
            "request_pause",
            RunControlTransitionKind.PAUSE_REQUESTED,
            "pause_requested",
            RunControlTransitionKind.PAUSED,
            id="pause",
        ),
    ],
)
def test_a_boundary_reached_while_a_request_is_published_does_not_land_it(
    request_control: str,
    requested: RunControlTransitionKind,
    boundary: str,
    landed: RunControlTransitionKind,
) -> None:
    """A request is published before any boundary can observe it.

    The run thread may reach a boundary at any moment, including while the
    requesting thread is still publishing the request. The sink stands in for
    that boundary: it checks the channel the instant the request is published.
    A boundary that already saw the request would land it first, so a consumer
    folding the events would see the landing ahead of the request.
    """
    seen: list[RunControlTransitionKind] = []
    observed: list[object] = []

    def sink(transition: RunControlTransition) -> None:
        seen.append(transition.kind)
        if transition.kind is requested:
            try:
                observed.append(getattr(control, boundary)())
            except RunStopped:
                observed.append(RunStopped)

    control = create_run_control_channel(sink, threads=SimThreads())

    if request_control == "request_stop":
        control.request_stop(StopMode.DRAIN)
    else:
        getattr(control, request_control)()

    assert observed in ([None], [False])
    assert seen == [requested]
    assert landed not in seen


class _DiscardingSteerArtifacts:
    def put(self, ref: ArtifactRef, text: str) -> None:
        del ref, text


def test_a_steer_control_without_its_input_gets_the_input_on_the_next_poll() -> None:
    """A crash between a steer control and its input is repaired: the poll supplies the input."""
    control = ControlInput(
        control_id=ControlId(root="control-0"),
        action="steer",
        artifact=ArtifactRef(artifact_id=ArtifactId(root="steer-text"), digest="d"),
    )
    held = step(initial_state(), RunControlEvent(control=control, now_at=1.0)).state
    assert held.sessions.inputs == ()
    bridge = RunControlBridge(
        create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads()),
        _DiscardingSteerArtifacts(),
    )
    repaired = bridge.poll(held, 2.0)
    assert [
        event.input.input_id.root for event in repaired if isinstance(event, SessionInputReceived)
    ] == ["steer-control-0"]
    recorded = step(held, repaired[0]).state
    assert bridge.poll(recorded, 3.0) == ()


def test_the_bridge_publishes_the_run_deadline_on_its_first_poll() -> None:
    """Work on other threads (a turn waiting out an outage) bounds its waits by this deadline."""
    core = initial_state()
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    bridge = RunControlBridge(channel, _DiscardingSteerArtifacts())
    assert channel.run_deadline() is None

    bridge.poll(core, core.run.now_at)

    assert channel.run_deadline() == core.run.deadline_at


@pytest.mark.parametrize("source", ["timed", "capacity"])
@given(seconds=st.floats(min_value=0.5, max_value=5_000.0), seed=st.integers(0, 2**16))
def test_a_self_resume_releases_every_parked_waiter(
    source: Literal["timed", "capacity"], seconds: float, seed: int
) -> None:
    """When the channel resumes the run itself, every parked waiter wakes, not only its own.

    Before, the timed resume of a quota wait and the elapsed wait for capacity cleared the
    pause without a notify, so a boundary parked in ``wait_while_paused`` stayed parked.
    """
    threads = SimThreads(schedule_seed=seed)
    events = FakeRunControlEventSink()
    control = create_run_control_channel(events, threads=threads)
    control.request_pause()
    released: list[float] = []

    def boundary() -> None:
        control.wait_while_paused()
        released.append(threads.now())

    def turn() -> None:
        if source == "timed":
            assert control.wait_resumed(seconds) is PauseWake.ELAPSED
            return
        control.enter_capacity()
        try:
            request = control.park_for_capacity(waiting=True)
            assert request is not None
            assert control.wait_for_capacity(seconds, request).wake is CapacityWake.ELAPSED
        finally:
            control.leave_capacity()

    def main() -> float:
        start = threads.now()
        waiter = threads.spawn(boundary, name="boundary", daemon=False)
        worker = threads.spawn(turn, name="turn", daemon=False)
        worker.join(seconds + HANG_GUARD_S)
        waiter.join(HANG_GUARD_S)
        assert not waiter.is_alive(), "the parked boundary was never woken"
        return start

    start = threads.run(main)

    assert released == [pytest.approx(start + seconds)]
    assert not control.pause_requested()
    assert [t.kind for t in events.transitions].count(RunControlTransitionKind.RESUMED) == 1


@dataclass(frozen=True)
class _Waiter:
    """One turn parked on the paused run: a timed quota wait, or a wait for capacity."""

    kind: Literal["timed", "capacity"]
    seconds: float
    cancelled: bool


_WAITERS = st.lists(
    st.builds(
        _Waiter,
        kind=st.sampled_from(["timed", "capacity"]),
        seconds=st.floats(min_value=100.0, max_value=1_000.0),
        cancelled=st.booleans(),
    ),
    min_size=1,
    max_size=4,
)


class _ParkedTurns:
    """Turns parked on one paused run, each ending only through the channel's public waits.

    A turn's cancellation is its *ended* condition becoming true, followed by
    ``interrupt_waits``, as the capacity gate's production timer does. ``outcomes`` holds
    each finished turn's wake and the time it ended; ``requests`` the operator's request
    each capacity turn waited under.
    """

    def __init__(self, waiters: list[_Waiter], seed: int) -> None:
        self.waiters = waiters
        self.threads = SimThreads(schedule_seed=seed)
        self.events = FakeRunControlEventSink()
        self.control = create_run_control_channel(self.events, threads=self.threads)
        self.control.request_pause()
        self.base = self.threads.now()
        self.ended = [False] * len(waiters)
        self.outcomes: dict[int, tuple[object, float]] = {}
        self.requests: dict[int, int] = {}

    def turn(self, index: int) -> None:
        waiter, control = self.waiters[index], self.control

        def gone() -> bool:
            return self.ended[index]

        if waiter.kind == "timed":
            wake = control.wait_resumed(waiter.seconds, ended=gone)
            self.outcomes[index] = (wake, self.threads.now())
            return
        control.enter_capacity()
        try:
            request = control.park_for_capacity(ended=gone)
            assert request is not None
            self.requests[index] = request
            end = control.wait_for_capacity(waiter.seconds, request, ended=gone)
            self.outcomes[index] = (end.wake, self.threads.now())
            assert end.request == request
        finally:
            control.leave_capacity()

    def cancel(self) -> None:
        for index, waiter in enumerate(self.waiters):
            if waiter.cancelled:
                self.ended[index] = True
        self.control.interrupt_waits()

    def survivors_parked(self) -> bool:
        return all(
            index not in self.outcomes
            for index, waiter in enumerate(self.waiters)
            if not waiter.cancelled
        )

    def resumed(self) -> int:
        kinds = [transition.kind for transition in self.events.transitions]
        return kinds.count(RunControlTransitionKind.RESUMED)


@given(
    waiters=_WAITERS,
    cancel_at=st.floats(min_value=2.0, max_value=99.0),
    seed=st.integers(0, 2**16),
)
def test_a_turns_cancel_ends_only_its_own_wait(
    waiters: list[_Waiter], cancel_at: float, seed: int
) -> None:
    """Cancelling any subset of the parked turns leaves the run, and every other turn, as it was.

    Timed turns arm the run's timed resume at 0 s; capacity turns wait under the operator's
    request from 1 s. Cancelled turns end at once and change nothing: the run stays paused,
    the others stay parked under the same request, and the run resumes itself, once, at the
    earliest deadline still in force (every timed one, and each surviving capacity wait). The
    timed resume belongs to the channel, so it fires even when every turn that armed it left.
    Before, a cancelled wait counted as elapsed and resumed the run, and the first capacity
    turn to leave dropped the operator's request for all of them.
    """
    run = _ParkedTurns(waiters, seed)
    threads, control = run.threads, run.control
    deadlines = [w.seconds for w in waiters if w.kind == "timed"]
    deadlines += [1.0 + w.seconds for w in waiters if w.kind == "capacity" and not w.cancelled]
    resume_at = min(deadlines, default=None)

    def main() -> tuple[bool, bool, bool]:
        workers = [
            threads.spawn(lambda i=i: run.turn(i), name=f"turn-{i}", daemon=False)
            for i in range(len(waiters))
        ]
        threads.sleep(1.0)
        if any(w.kind == "capacity" for w in waiters):
            assert control.resume_when_capacity_returns()
        threads.sleep(cancel_at - 1.0)
        run.cancel()
        threads.sleep(1.0)
        after_cancel = (control.pause_requested(), run.survivors_parked())
        # The run loop's control poll, after the earliest deadline still in force.
        threads.sleep(max(run.base + (resume_at or 0.0) + 1.0 - threads.now(), 0.0))
        paused_at_end = control.pause_requested()
        for worker in workers:
            worker.join(HANG_GUARD_S)
        return (*after_cancel, paused_at_end)

    paused_after_cancel, survivors_parked, paused_at_end = threads.run(main)

    assert paused_after_cancel
    assert survivors_parked
    assert len(set(run.requests.values())) <= 1
    assert paused_at_end is (resume_at is None)
    assert run.resumed() == (0 if resume_at is None else 1)
    for index, waiter in enumerate(waiters):
        wake, at = run.outcomes.get(index, (None, None))
        if waiter.cancelled:
            assert wake in {PauseWake.DETACHED, CapacityWake.DETACHED}
            assert at == pytest.approx(run.base + cancel_at)
        elif resume_at is not None:
            assert wake in {PauseWake.ELAPSED, CapacityWake.ELAPSED}
            assert at == pytest.approx(run.base + resume_at)
