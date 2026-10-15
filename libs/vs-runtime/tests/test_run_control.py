"""Public contract tests for cooperative run control."""

from __future__ import annotations

import pytest
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

    assert control.take_pending_steer() == [
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

    assert resumed is False
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

    assert threads.run(lambda: control.wait_resumed(0.0)) is True


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
