"""VibeSys persistence adapter tests for runtime run-control transitions."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vibesys.api import CoreEventType, RunStopped
from vibesys.run.integration import LocalRunIntegration

if TYPE_CHECKING:
    from pathlib import Path


def test_control_transitions_keep_the_existing_persisted_event_format(
    tmp_path: Path,
) -> None:
    integration = LocalRunIntegration()
    integration.attach(tmp_path, run_id="run-1")
    unsubscribe = integration.events.subscribe(
        lambda event: integration.control.resume() if event.type is CoreEventType.PAUSED else None
    )
    try:
        integration.control.queue_steer("focus on latency")
        assert integration.control.take_pending_steer() == ["focus on latency"]
        integration.control.notify_steer_consumed(
            agent_kind="implementer",
            round_label="round-1",
            execution_id="execution-1",
        )
        integration.control.request_pause()
        integration.control.wait_while_paused()
        integration.control.request_stop()
        with pytest.raises(RunStopped):
            integration.control.raise_if_stopped()
    finally:
        unsubscribe()
        integration.close()

    persisted = [
        json.loads(line) for line in (tmp_path / "core-events.jsonl").read_text().splitlines()
    ]
    assert [event["type"] for event in persisted] == [
        "steer_queued",
        "steer_consumed",
        "pause_requested",
        "paused",
        "resumed",
        "stop_requested",
        "stopped",
    ]
    assert persisted[0]["text"] == "focus on latency"
    assert persisted[1]["agent_kind"] == "implementer"
    assert persisted[1]["round_label"] == "round-1"
    assert persisted[1]["execution_id"] == "execution-1"
    assert all(event["run_id"] == "run-1" for event in persisted)


def test_a_mid_turn_delivery_persists_with_the_identity_of_the_turn_that_took_it(
    tmp_path: Path,
) -> None:
    class _Turn:
        agent_kind = "implementer"
        round_label = "round-1"
        execution_id = "execution-1"

        def offer_steer(self, text: str, on_rejected: object) -> bool:
            del text, on_rejected
            return True

    integration = LocalRunIntegration()
    integration.attach(tmp_path, run_id="run-1")
    try:
        integration.control.attach_steer_target(_Turn())
        integration.control.queue_steer("focus on latency")
        assert integration.control.take_pending_steer() == []
    finally:
        integration.close()

    persisted = [
        json.loads(line) for line in (tmp_path / "core-events.jsonl").read_text().splitlines()
    ]
    assert [event["type"] for event in persisted] == ["steer_queued", "steer_delivered"]
    assert persisted[1]["text"] == "focus on latency"
    assert (persisted[1]["agent_kind"], persisted[1]["execution_id"]) == (
        "implementer",
        "execution-1",
    )
