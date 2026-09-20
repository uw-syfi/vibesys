"""Unit tests for `RunControlChannel`, the mailbox between `RunControl` and the run loop."""

from __future__ import annotations

import threading
import time

import pytest

from vibesys.events import CoreEventType
from vibesys.run.event_journal import EventJournal
from vibesys.run.run_control import RunControlChannel, RunStopped, splice_steering


def _channel() -> tuple[RunControlChannel, list[CoreEventType]]:
    """Build a channel over a fresh journal, recording the event types it emits."""
    events = EventJournal()
    observed: list[CoreEventType] = []
    events.subscribe(lambda event: observed.append(event.type))
    return RunControlChannel(events), observed


def test_queue_steer_drains_once_and_emits_per_call() -> None:
    channel, observed = _channel()

    channel.queue_steer("focus on latency")
    channel.queue_steer("check for reward hacking")

    assert channel.take_pending_steer() == ["focus on latency", "check for reward hacking"]
    assert channel.take_pending_steer() == []
    assert observed == [CoreEventType.STEER_QUEUED, CoreEventType.STEER_QUEUED]


def test_wait_while_paused_returns_immediately_when_not_paused() -> None:
    channel, observed = _channel()

    channel.wait_while_paused()

    assert observed == []


def test_pause_blocks_until_resume() -> None:
    channel, observed = _channel()
    channel.request_pause()

    released = threading.Event()
    waiter = threading.Thread(target=lambda: (channel.wait_while_paused(), released.set()))
    waiter.start()
    time.sleep(0.02)
    assert waiter.is_alive()

    channel.resume()
    waiter.join(timeout=1)

    assert released.is_set()
    assert observed == [CoreEventType.PAUSE_REQUESTED, CoreEventType.PAUSED, CoreEventType.RESUMED]


def test_raise_if_stopped_lands_the_stop_and_raises() -> None:
    channel, observed = _channel()
    channel.request_stop()

    with pytest.raises(RunStopped):
        channel.raise_if_stopped()

    assert observed == [CoreEventType.STOP_REQUESTED, CoreEventType.STOPPED]
    # Landing is idempotent: a second check finds nothing new to land, but
    # the stop is still in effect, so it still raises.
    with pytest.raises(RunStopped):
        channel.raise_if_stopped()
    assert observed == [CoreEventType.STOP_REQUESTED, CoreEventType.STOPPED, CoreEventType.STOPPED]


def test_stop_while_paused_releases_the_wait_and_unwinds() -> None:
    channel, observed = _channel()
    channel.request_pause()

    raised: list[BaseException] = []

    def wait_at_boundary() -> None:
        try:
            channel.wait_while_paused()
        except BaseException as error:  # noqa: BLE001  # The unwind signal is the assertion.
            raised.append(error)

    waiter = threading.Thread(target=wait_at_boundary)
    waiter.start()
    time.sleep(0.02)
    assert waiter.is_alive()

    channel.request_stop()
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert [type(error) for error in raised] == [RunStopped]
    assert observed == [
        CoreEventType.PAUSE_REQUESTED,
        CoreEventType.PAUSED,
        CoreEventType.STOP_REQUESTED,
        CoreEventType.STOPPED,
    ]


def test_resume_cancels_a_pending_stop() -> None:
    channel, observed = _channel()
    channel.request_stop()
    channel.resume()

    channel.raise_if_stopped()  # Does not raise: resume cleared the stop.

    assert observed == [CoreEventType.STOP_REQUESTED, CoreEventType.RESUMED]


def test_notify_steer_consumed_emits_with_the_landing_invocations_identity() -> None:
    channel, observed = _channel()

    channel.notify_steer_consumed(
        agent_kind="implementer", round_label="round-1", execution_id="e1"
    )

    assert observed == [CoreEventType.STEER_CONSUMED]


def test_splice_steering_appends_an_operator_block() -> None:
    assert splice_steering("Do the work", []) == "Do the work"

    spliced = splice_steering("Do the work", ["focus on latency", "check reward hacking"])

    assert spliced.startswith("Do the work\n\n## Operator steering (live)")
    assert "- focus on latency" in spliced
    assert "- check reward hacking" in spliced
