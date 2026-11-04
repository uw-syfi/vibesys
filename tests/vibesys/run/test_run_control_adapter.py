"""VibeSys persistence adapter tests for runtime run-control transitions."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from tests.support.real_git import NO_GITHUB, REAL_GIT

from vibesys.api import CoreEventType, RunStopped
from vibesys.run.integration import LocalRunIntegration
from vs_core.api import StopMode
from vs_runtime.api.infrastructure import (
    RunControlTransition,
    RunControlTransitionKind,
    SteerTurn,
)
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import CoreEvent


def test_control_transitions_keep_the_existing_persisted_event_format(
    tmp_path: Path,
) -> None:
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )
    integration.attach(tmp_path, run_id="run-1")

    def resume_when_paused(event: CoreEvent) -> None:
        if event.type is CoreEventType.PAUSED:
            integration.control.resume()

    unsubscribe = integration.events.subscribe(resume_when_paused)
    try:
        integration.control.queue_steer("focus on latency")
        assert [steer.text for steer in integration.control.take_pending_steer()] == [
            "focus on latency"
        ]
        integration.control.notify_steer_consumed(
            SteerTurn(
                role="implementer",
                round_label="round-1",
                execution_id="execution-1",
                attempt_id=None,
            ),
            control_id=None,
        )
        integration.control.notify_steer_dropped(
            "role 'judge' is not declared", why="role 'judge' is not declared", control_id=None
        )
        integration.control.request_pause()
        integration.control.wait_while_paused()
        integration.control.request_stop(StopMode.DRAIN)
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
        "steer_dropped",
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
    assert persisted[2]["text"] == "role 'judge' is not declared"
    assert all(event["run_id"] == "run-1" for event in persisted)


def test_every_run_control_transition_has_its_persisted_event_type() -> None:
    """The adapter persists a transition under the core event type of the same value."""
    assert {kind.value for kind in RunControlTransitionKind} <= {
        event_type.value for event_type in CoreEventType
    }


def test_a_rejected_inbox_request_is_persisted_naming_it() -> None:
    transition = RunControlTransition(
        kind=RunControlTransitionKind.REQUEST_REJECTED, text="it ended nothing", request_id="w-1"
    )
    assert transition.summary == "control request w-1 rejected: it ended nothing"
    with pytest.raises(ValueError, match="request_id"):
        RunControlTransition(kind=RunControlTransitionKind.REQUEST_REJECTED, text="no id")
    with pytest.raises(ValueError, match="request_id"):
        RunControlTransition(kind=RunControlTransitionKind.PAUSED, request_id="w-1")
