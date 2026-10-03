"""Public contract tests for cooperative run control."""

from __future__ import annotations

import asyncio
import threading

import pytest
from pydantic import ValidationError

from vs_runtime.api.infrastructure import (
    BlockingOperations,
    RunControlTransition,
    RunControlTransitionKind,
    RunStopped,
    create_run_control_channel,
    create_runtime_control,
)
from vs_runtime.api.testing import FakeRunControlEventSink


def test_steering_is_ordered_and_drained_exactly_once() -> None:
    events = FakeRunControlEventSink()
    control = create_run_control_channel(events)

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
    paused = threading.Event()
    released = threading.Event()
    events = FakeRunControlEventSink(
        on_transition=lambda transition: (
            paused.set() if transition.kind is RunControlTransitionKind.PAUSED else None
        )
    )
    control = create_run_control_channel(events)
    control.request_pause()

    waiter = threading.Thread(
        target=lambda: (control.wait_while_paused(), released.set()),
    )
    waiter.start()
    paused.wait()
    assert not released.is_set()

    control.resume()
    waiter.join()

    assert released.is_set()
    assert [transition.kind for transition in events.transitions] == [
        RunControlTransitionKind.PAUSE_REQUESTED,
        RunControlTransitionKind.PAUSED,
        RunControlTransitionKind.RESUMED,
    ]


def test_stop_releases_a_paused_boundary_and_unwinds() -> None:
    paused = threading.Event()
    events = FakeRunControlEventSink(
        on_transition=lambda transition: (
            paused.set() if transition.kind is RunControlTransitionKind.PAUSED else None
        )
    )
    control = create_run_control_channel(events)
    control.request_pause()
    raised: list[BaseException] = []

    def wait_at_boundary() -> None:
        try:
            control.wait_while_paused()
        except RunStopped as error:
            raised.append(error)

    waiter = threading.Thread(target=wait_at_boundary)
    waiter.start()
    paused.wait()
    control.request_stop()
    waiter.join()

    assert [type(error) for error in raised] == [RunStopped]
    assert [transition.kind for transition in events.transitions] == [
        RunControlTransitionKind.PAUSE_REQUESTED,
        RunControlTransitionKind.PAUSED,
        RunControlTransitionKind.STOP_REQUESTED,
        RunControlTransitionKind.STOPPED,
    ]


def test_stop_lands_on_every_boundary_until_resume_cancels_it() -> None:
    events = FakeRunControlEventSink()
    control = create_run_control_channel(events)
    control.request_stop()

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


def test_runtime_checkpoint_lands_stop_before_opening_a_worker() -> None:
    events = FakeRunControlEventSink()
    channel = create_run_control_channel(events)
    blocking = BlockingOperations()
    control = create_runtime_control(channel, blocking)
    channel.request_stop()

    async def exercise() -> None:
        with pytest.raises(RunStopped):
            await control.checkpoint()
        blocking.begin_close()
        assert await blocking.drain() == []

    asyncio.run(exercise())

    assert [transition.kind for transition in events.transitions] == [
        RunControlTransitionKind.STOP_REQUESTED,
        RunControlTransitionKind.STOPPED,
    ]


def test_cancelled_runtime_checkpoint_drains_its_paused_worker() -> None:
    async def exercise() -> list[RunControlTransitionKind]:
        paused = asyncio.Event()
        loop = asyncio.get_running_loop()

        def observe_transition(transition: RunControlTransition) -> None:
            if transition.kind is RunControlTransitionKind.PAUSED:
                loop.call_soon_threadsafe(paused.set)

        events = FakeRunControlEventSink(
            on_transition=observe_transition,
        )
        channel = create_run_control_channel(events)
        blocking = BlockingOperations()
        control = create_runtime_control(channel, blocking)
        channel.request_pause()
        checkpoint = asyncio.create_task(control.checkpoint())
        await paused.wait()
        checkpoint.cancel()
        await asyncio.sleep(0)
        assert not checkpoint.done()
        channel.resume()
        with pytest.raises(asyncio.CancelledError):
            await checkpoint
        blocking.begin_close()
        assert await blocking.drain() == []
        return [transition.kind for transition in events.transitions]

    assert asyncio.run(exercise()) == [
        RunControlTransitionKind.PAUSE_REQUESTED,
        RunControlTransitionKind.PAUSED,
        RunControlTransitionKind.RESUMED,
    ]


def test_consumed_steering_records_the_landing_invocation() -> None:
    events = FakeRunControlEventSink()
    control = create_run_control_channel(events)

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
