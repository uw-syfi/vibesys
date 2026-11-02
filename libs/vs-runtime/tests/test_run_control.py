"""Public contract tests for cooperative run control."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given
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
    escalate_stop,
    initial_state,
    step,
    stop_mode,
)
from vs_runtime.api.core import RunControlBridge
from vs_runtime.api.infrastructure import (
    CapacityWake,
    ChannelRunControl,
    ControlStatus,
    PauseWake,
    RefusalReason,
    ResumeRequest,
    RunControlTransition,
    RunControlTransitionKind,
    RunStopped,
    SteerDelivery,
    SteerDeliveryState,
    SteerMode,
    SteerTurn,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import HANG_GUARD_S, SimThreads, wait_or_fail

if TYPE_CHECKING:
    from vs_core.api import CoreEvent
    from vs_runtime.api.infrastructure import RunControlChannel


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


def test_stop_lands_on_every_boundary_and_no_resume_cancels_it() -> None:
    events = FakeRunControlEventSink()
    control = create_run_control_channel(events, threads=SimThreads())
    control.request_stop(StopMode.DRAIN)

    with pytest.raises(RunStopped):
        control.raise_if_stopped()
    assert not control.resume()
    with pytest.raises(RunStopped):
        control.raise_if_stopped()

    assert [transition.kind for transition in events.transitions] == [
        RunControlTransitionKind.STOP_REQUESTED,
        RunControlTransitionKind.STOPPED,
        RunControlTransitionKind.STOPPED,
    ]


def test_consumed_steering_records_the_landing_invocation() -> None:
    events = FakeRunControlEventSink()
    control = create_run_control_channel(events, threads=SimThreads())

    control.notify_steer_consumed(
        SteerTurn(
            role="implementer",
            round_label="round-1",
            execution_id="execution-1",
            attempt_id="attempt-1",
        ),
        control_id="s1",
    )

    assert events.transitions == [
        RunControlTransition(
            kind=RunControlTransitionKind.STEER_CONSUMED,
            agent_kind="implementer",
            round_label="round-1",
            execution_id="execution-1",
            delivery=SteerDelivery(
                state=SteerDeliveryState.REACHED,
                control_id="s1",
                role="implementer",
                attempt_id="attempt-1",
            ),
        )
    ]


_FACTS = {
    SteerDeliveryState.QUEUED: {"mode": SteerMode.NEXT_TURN},
    SteerDeliveryState.WITHDRAW_QUEUED: {"withdraws": "keep"},
    SteerDeliveryState.REACHED: {"role": "implementer"},
    SteerDeliveryState.REACHED_MID_TURN: {"role": "implementer"},
    SteerDeliveryState.IN_FORCE: {"role": "implementer"},
    SteerDeliveryState.DROPPED: {"reason": "gone"},
    SteerDeliveryState.ENDED: {"reason": "withdrawn"},
    SteerDeliveryState.REFUSED: {"reason": "run ending"},
}
_EXTRA_FACT = {"mode": SteerMode.STANDING, "withdraws": "keep", "role": "judge", "reason": "why"}


@given(state=st.sampled_from(SteerDeliveryState), fact=st.sampled_from(sorted(_EXTRA_FACT)))
def test_a_steer_step_carries_the_facts_its_state_names_and_no_other(
    state: SteerDeliveryState, fact: str
) -> None:
    facts = _FACTS[state]
    assert SteerDelivery.model_validate({"state": state, **facts}).state is state
    if fact in facts:
        with pytest.raises(ValidationError, match=f"{fact}: a {state.value} step needs {fact}"):
            SteerDelivery.model_validate(
                {"state": state, **{k: v for k, v in facts.items() if k != fact}}
            )
    else:
        with pytest.raises(ValidationError, match=f"{fact}: a {state.value} step has no {fact}"):
            SteerDelivery.model_validate({"state": state, **facts, fact: _EXTRA_FACT[fact]})


@given(kind=st.sampled_from(RunControlTransitionKind))
def test_a_transition_carries_the_step_of_its_kind_or_none(
    kind: RunControlTransitionKind,
) -> None:
    steps = {
        RunControlTransitionKind.STEER_QUEUED: SteerDeliveryState.QUEUED,
        RunControlTransitionKind.GUIDANCE_QUEUED: SteerDeliveryState.QUEUED,
        RunControlTransitionKind.WITHDRAW_QUEUED: SteerDeliveryState.WITHDRAW_QUEUED,
        RunControlTransitionKind.STEER_CONSUMED: SteerDeliveryState.REACHED,
        RunControlTransitionKind.STEER_DELIVERED_MID_TURN: SteerDeliveryState.REACHED_MID_TURN,
        RunControlTransitionKind.GUIDANCE_REACHED: SteerDeliveryState.IN_FORCE,
        RunControlTransitionKind.STEER_DROPPED: SteerDeliveryState.DROPPED,
        RunControlTransitionKind.GUIDANCE_ENDED: SteerDeliveryState.ENDED,
        RunControlTransitionKind.CONTROL_REFUSED: SteerDeliveryState.REFUSED,
    }
    request = "r1" if kind is RunControlTransitionKind.REQUEST_REJECTED else None
    for state in SteerDeliveryState:
        step = SteerDelivery.model_validate({"state": state, **_FACTS[state]})
        if steps.get(kind) is state:
            assert RunControlTransition(kind=kind, request_id=request, delivery=step).delivery
        else:
            with pytest.raises(ValidationError):
                RunControlTransition(kind=kind, request_id=request, delivery=step)
    if kind in steps and kind is not RunControlTransitionKind.CONTROL_REFUSED:
        with pytest.raises(ValidationError):
            RunControlTransition(kind=kind)
    else:
        assert RunControlTransition(kind=kind, request_id=request).delivery is None


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
    control.pause_for_quota(0.0)

    resumed = threads.run(control.wait_resumed)

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
    control.pause_for_quota(0.0)
    control.resume()

    assert threads.run(control.wait_resumed) is PauseWake.RESUMED


def test_a_stop_ends_a_bounded_wait_and_is_never_cleared_by_its_timeout() -> None:
    threads = SimThreads()
    control = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control.pause_for_quota(0.0)
    control.request_stop(StopMode.DRAIN)

    with pytest.raises(RunStopped):
        threads.run(control.wait_resumed)

    assert control.stop_requested()


def test_a_fallback_request_is_taken_once_and_a_plain_resume_clears_a_stale_one() -> None:
    control = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    control.enter_capacity(timed=True)

    control.pause_for_quota()
    assert control.resume_with_fallback() is True
    assert control.consume_fallback_request() is True
    assert control.consume_fallback_request() is False

    control.pause_for_quota()
    control.resume_with_fallback()
    control.resume()
    assert control.consume_fallback_request() is False


_Unpause = Literal["resume", "elapsed", "fallback"]


@given(st.lists(st.sampled_from(["resume", "elapsed", "fallback"]), min_size=1, max_size=6))
# The request a fallback resume left, outliving the timed resume of the next pause.
@example(["fallback", "elapsed"])
def test_a_fallback_request_answers_only_the_quota_pause_it_ended(
    unpauses: list[_Unpause],
) -> None:
    """Each quota pause ends one way; only a fallback resume leaves a request to take.

    No turn takes the requests here, so a request that outlived its pause would be taken
    by the turn a later pause resumes.
    """
    threads = SimThreads()
    control = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control.enter_capacity(timed=True)

    for unpause in unpauses:
        match unpause:
            case "resume":
                control.pause_for_quota()
                control.resume()
            case "elapsed":
                control.pause_for_quota(0.0)
                assert threads.run(control.wait_resumed) is PauseWake.ELAPSED
            case "fallback":
                control.pause_for_quota()
                assert control.resume_with_fallback() is True

    assert control.consume_fallback_request() is (unpauses[-1] == "fallback")


@pytest.mark.parametrize("pause", ["none", "operator", "stopping", "no_turn_left"])
def test_a_fallback_is_refused_and_changes_nothing_unless_a_waiting_turn_would_take_it(
    pause: Literal["none", "operator", "stopping", "no_turn_left"],
) -> None:
    """Refused unless a quota stop paused the run and a stopped turn still waits on it.

    ``no_turn_left``: the only stopped turn was cancelled while the pause's timed resume
    was armed, so the run stays paused until then and no turn would take the switch.
    """
    events = FakeRunControlEventSink()
    control = create_run_control_channel(events, threads=SimThreads())
    if pause == "operator":
        control.request_pause()
    if pause == "stopping":
        control.enter_capacity(timed=True)
        control.pause_for_quota()
        control.request_stop(StopMode.DRAIN)
    if pause == "no_turn_left":
        control.enter_capacity(timed=True)
        control.pause_for_quota(600.0)
        control.leave_capacity(timed=True)
    before = list(events.transitions)

    assert control.resume_with_fallback() is False

    assert control.pause_requested() is (pause != "none")
    assert control.stop_requested() is (pause == "stopping")
    assert control.consume_fallback_request() is False
    assert events.transitions == before


def test_resuming_with_the_fallback_releases_a_parked_run_like_a_resume() -> None:
    events = FakeRunControlEventSink()
    control = create_run_control_channel(events, threads=SimThreads())
    control.request_pause()
    control.enter_capacity()
    control.pause_for_quota()

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
    control.pause_for_quota()
    released: list[float] = []

    def boundary() -> None:
        control.wait_while_paused()
        released.append(threads.now())

    def turn() -> None:
        if source == "timed":
            control.pause_for_quota(seconds)
            assert control.wait_resumed() is PauseWake.ELAPSED
            return
        control.enter_capacity()
        try:
            request = control.park_for_capacity(waiting=True)
            assert isinstance(request, int)
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
        self.control.pause_for_quota()
        self.base = self.threads.now()
        self.ended = [False] * len(waiters)
        self.outcomes: dict[int, tuple[object, float]] = {}
        self.requests: dict[int, int] = {}

    def turn(self, index: int) -> None:
        waiter, control = self.waiters[index], self.control

        def gone() -> bool:
            return self.ended[index]

        if waiter.kind == "timed":
            control.pause_for_quota(waiter.seconds)
            wake = control.wait_resumed(ended=gone)
            self.outcomes[index] = (wake, self.threads.now())
            return
        control.enter_capacity()
        try:
            request = control.park_for_capacity(ended=gone)
            assert isinstance(request, int)
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


@example(waiters=[_Waiter(kind="capacity", seconds=100.0, cancelled=True)], cancel_at=2.0, seed=0)
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
    When every turn was a cancelled capacity wait nothing owns the pause: the run resumes at
    the cancel.
    Before, a cancelled wait counted as elapsed and resumed the run, and the first capacity
    turn to leave dropped the operator's request for all of them.
    """
    run = _ParkedTurns(waiters, seed)
    threads, control = run.threads, run.control
    deadlines = [w.seconds for w in waiters if w.kind == "timed"]
    deadlines += [1.0 + w.seconds for w in waiters if w.kind == "capacity" and not w.cancelled]
    resume_at = min(deadlines, default=None)
    # No timed resume and no capacity turn left: nothing owns the pause, so the last turn
    # out ends it (``leave_capacity``), at the cancel.
    orphaned = resume_at is None and all(w.cancelled for w in waiters)

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

    assert paused_after_cancel is not orphaned
    assert survivors_parked
    assert len(set(run.requests.values())) <= 1
    assert paused_at_end is (resume_at is None and not orphaned)
    assert run.resumed() == (0 if resume_at is None and not orphaned else 1)
    for index, waiter in enumerate(waiters):
        wake, at = run.outcomes.get(index, (None, None))
        if waiter.cancelled:
            assert wake in {PauseWake.DETACHED, CapacityWake.DETACHED}
            assert at == pytest.approx(run.base + cancel_at)
        elif resume_at is not None:
            assert wake in {PauseWake.ELAPSED, CapacityWake.ELAPSED}
            assert at == pytest.approx(run.base + resume_at)


def _strongest(modes: list[StopMode]) -> StopMode | None:
    """The stop in force after ``modes``: a cancel once any asked to cancel, else a drain."""
    if StopMode.CANCEL in modes:
        return StopMode.CANCEL
    return StopMode.DRAIN if modes else None


_STOP_OPS = st.lists(
    st.one_of(st.sampled_from(StopMode).map(lambda mode: ("stop", mode)), st.just(("poll",))),
    max_size=8,
)


@pytest.mark.regression
@example(ops=[("stop", StopMode.DRAIN), ("poll",), ("stop", StopMode.CANCEL)])
@example(ops=[("stop", StopMode.CANCEL), ("stop", StopMode.DRAIN)])
@given(ops=_STOP_OPS)
def test_a_stop_only_escalates_and_every_escalation_reaches_core(
    ops: list[tuple[str, StopMode] | tuple[str]],
) -> None:
    """periodic-1330 MED-2: the run kept the last stop's mode and told core only the first.

    However stop requests and the run loop's polls interleave, the run's stop is the
    strongest one asked for (a drain never replaces a cancel), and core records it.
    """
    threads = SimThreads()
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control = ChannelRunControl(channel, threads)
    bridge = RunControlBridge(channel, _DiscardingSteerArtifacts(), control=control)
    core = initial_state()
    asked: list[StopMode] = []
    for op in [*ops, ("poll",)]:
        match op:
            case ("stop", StopMode() as mode):
                control.stop(mode)
                asked.append(mode)
            case _:
                for event in bridge.poll(core, core.run.now_at):
                    core = step(core, event).state
        assert channel.stop_mode() == _strongest(asked)
    recorded = [c.mode for c in core.run.controls if c.action == "stop" and c.mode is not None]
    assert _strongest(recorded) == _strongest(asked)


def _never_weaker(before: StopMode | None, after: StopMode | None) -> bool:
    """Whether ``after`` keeps every stop ``before`` held: a stop only escalates."""
    return before is None or (after is not None and escalate_stop(before, after) is after)


_STOP_RESUME_OPS = st.lists(
    st.one_of(
        st.sampled_from(StopMode).map(lambda mode: ("stop", mode)),
        st.just(("resume",)),
        st.just(("pause",)),
        st.just(("poll",)),
    ),
    max_size=10,
)


@pytest.mark.regression
@example(ops=[("stop", StopMode.CANCEL), ("poll",), ("resume",)])
@example(ops=[("stop", StopMode.CANCEL), ("resume",), ("poll",)])
@example(ops=[("pause",), ("stop", StopMode.DRAIN), ("resume",)])
@example(ops=[("pause",), ("poll",), ("resume",), ("stop", StopMode.DRAIN)])
@given(ops=_STOP_RESUME_OPS)
def test_no_resume_ever_weakens_a_pending_stop(
    ops: list[tuple[str, StopMode] | tuple[str]],
) -> None:
    """PR #3209 MED-3: a resume cleared the channel's stop while core went on stopping.

    However stops, pauses, resumes and the run loop's polls interleave, a resume while a
    stop is pending is refused as ``STOPPING``, and neither the channel nor core ever holds
    a weaker stop than it held before.
    """
    threads = SimThreads()
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control = ChannelRunControl(channel, threads)
    bridge = RunControlBridge(channel, _DiscardingSteerArtifacts(), control=control)
    core = initial_state()
    asked: list[StopMode] = []
    for number, op in enumerate([*ops, ("poll",)]):
        held, recorded = channel.stop_mode(), stop_mode(core.run)
        match op:
            case ("stop", StopMode() as mode):
                control.stop(mode)
                asked.append(mode)
            case ("resume",):
                ack = control.submit(ResumeRequest(control_id=f"op-{number}"))
                if held is not None:
                    assert (ack.status, ack.reason) == (
                        ControlStatus.REFUSED,
                        RefusalReason.STOPPING,
                    )
            case ("pause",):
                channel.pause_for_quota()
            case _:
                for event in bridge.poll(core, core.run.now_at):
                    core = step(core, event).state
        assert _never_weaker(held, channel.stop_mode())
        assert _never_weaker(recorded, stop_mode(core.run))
    assert channel.stop_mode() == _strongest(asked)
    assert stop_mode(core.run) == _strongest(asked)


@pytest.mark.regression
@example(modes=[StopMode.DRAIN, StopMode.DRAIN])
@example(modes=[StopMode.DRAIN, StopMode.CANCEL, StopMode.DRAIN, StopMode.CANCEL])
@given(modes=st.lists(st.sampled_from(StopMode), max_size=6))
def test_a_stop_request_is_announced_only_when_it_changes_the_stop(modes: list[StopMode]) -> None:
    """PR #3209 LOW-4: every repeated stop emitted another ``STOP_REQUESTED``.

    The channel announces a stop request, to its event sink and its stop listeners, only
    when it changes the stop in force: the first stop, and a cancel that escalates a drain.
    """
    events = FakeRunControlEventSink()
    channel = create_run_control_channel(events, threads=SimThreads())
    heard: list[StopMode | None] = []
    channel.on_stop_requested(lambda: heard.append(channel.stop_mode()))
    in_force: list[StopMode] = []
    for mode in modes:
        channel.request_stop(mode)
        mode_now = channel.stop_mode()
        assert mode_now is not None
        if not in_force or in_force[-1] is not mode_now:
            in_force.append(mode_now)

    kinds = [transition.kind for transition in events.transitions]
    assert kinds == [RunControlTransitionKind.STOP_REQUESTED] * len(in_force)
    assert heard == in_force


@dataclass
class _Process:
    """One process of a run: its channel, operators' controls, bridge, and queued events."""

    channel: RunControlChannel
    control: ChannelRunControl
    bridge: RunControlBridge
    queued: list[CoreEvent]


def _process() -> _Process:
    threads = SimThreads()
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control = ChannelRunControl(channel, threads)
    bridge = RunControlBridge(channel, _DiscardingSteerArtifacts(), control=control)
    return _Process(channel, control, bridge, [])


_RESTART_OPS = st.lists(
    st.one_of(
        st.sampled_from(StopMode).map(lambda mode: ("stop", mode)),
        st.sampled_from([("poll",), ("poll-queued",), ("commit",), ("restart",), ("resume",)]),
    ),
    max_size=12,
)


@pytest.mark.regression
@example(ops=[("stop", StopMode.CANCEL), ("poll",), ("restart",), ("stop", StopMode.DRAIN)])
@example(ops=[("stop", StopMode.DRAIN), ("poll",), ("restart",), ("stop", StopMode.CANCEL)])
@given(ops=_RESTART_OPS)
def test_a_stop_core_recorded_stays_in_force_across_a_runtime_restart(
    ops: list[tuple[str, StopMode] | tuple[str]],
) -> None:
    """A restarted process holds the stop core recorded, and only escalates it.

    ``restart`` starts a new process over core's committed state: what the old one had
    not committed is lost with it. From the new process's first poll on, its channel
    holds at least the stop core records, so a later drain never weakens a recorded
    cancel, a resume is refused as ``STOPPING``, and a later cancel still reaches core.
    """
    process, core = _process(), initial_state()
    polled = False
    for number, op in enumerate([*ops, ("poll",)]):
        match op:
            case ("stop", StopMode() as mode):
                process.control.stop(mode)
            case ("resume",):
                ack = process.control.submit(ResumeRequest(control_id=f"op-{number}"))
                if polled and stop_mode(core.run) is not None:
                    assert ack.reason is RefusalReason.STOPPING
            case ("restart",):
                process, polled = _process(), False
            case ("poll-queued",):
                process.queued += process.bridge.poll(core, core.run.now_at)
                polled = True
            case _:
                if op == ("poll",):
                    process.queued += process.bridge.poll(core, core.run.now_at)
                    polled = True
                for event in process.queued:
                    core = step(core, event).state
                process.queued = []
        if polled:
            assert _never_weaker(stop_mode(core.run), process.channel.stop_mode())
    assert stop_mode(core.run) == process.channel.stop_mode()
