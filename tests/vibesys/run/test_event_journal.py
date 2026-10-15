from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from tests.support.real_git import NO_GITHUB, REAL_GIT

from vibesys.events import (
    AgentExecutionStartedData,
    AgentOutputChunkData,
    CoreEventType,
    RoundFinishedData,
    WinnerSelectedData,
)
from vibesys.run.integration import LocalRunIntegration
from vs_runtime.api.infrastructure import (
    AgentExecutionFinished,
    AgentExecutionStarted,
    AgentExecutionStatus,
)
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from pathlib import Path


def test_current_round_event_requires_explicit_profile_outcome() -> None:
    with pytest.raises(ValidationError, match="profile_skipped"):
        RoundFinishedData.model_validate({"attempts": 1, "judge_verdict": "pass"})


def test_a_semantic_event_uses_the_unified_durable_stream(tmp_path: Path) -> None:
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )
    try:
        integration.attach(tmp_path, run_id="run-winner")
        data = WinnerSelectedData(selection="candidate", revision="abc123", settlement_id="s-1")
        integration.events.emit(CoreEventType.WINNER_SELECTED, data=data)

        event = integration.events.read()[0]
        assert event.run_id == "run-winner"
        assert event.type is CoreEventType.WINNER_SELECTED
        assert event.data == data
    finally:
        integration.close()


def test_agent_lifecycle_adapter_records_complete_invocation(tmp_path: Path) -> None:
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )
    try:
        integration.attach(tmp_path, run_id="run-1")
        integration.agent_execution_event(
            AgentExecutionStarted(
                agent_id="implementer",
                label="round-1",
                execution_id="invocation-1",
                system_prompt="system",
                user_prompt="task",
                provider="test",
                model="model-for-implementer",
            )
        )
        integration.agent_execution_event(
            AgentExecutionFinished(
                agent_id="implementer",
                label="round-1",
                execution_id="invocation-1",
                status=AgentExecutionStatus.COMPLETED,
                result={"value": "done"},
            )
        )

        assert [event.type for event in integration.events.read()] == [
            CoreEventType.AGENT_EXECUTION_STARTED,
            CoreEventType.PHASE_STARTED,
            CoreEventType.INVOCATION_STARTED,
            CoreEventType.AGENT_EXECUTION_FINISHED,
            CoreEventType.INVOCATION_FINISHED,
            CoreEventType.PHASE_FINISHED,
        ]
        assert {event.execution_id for event in integration.events.read()} == {"invocation-1"}
        started = integration.events.read()[0]
        assert isinstance(started.data, AgentExecutionStartedData)
        assert started.data.attempt is None
        assert started.data.system_prompt == "system"
    finally:
        integration.close()


def test_local_integrations_own_isolated_agent_event_streams(tmp_path: Path) -> None:
    first = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )
    second = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )
    try:
        first.attach(tmp_path / "first", run_id="run-1")
        second.attach(tmp_path / "second", run_id="run-2")

        first.agent_events.agent_output("first", agent_kind="judge")
        second.agent_events.agent_output("second", agent_kind="implementer")

        first_event = first.events.read()[0]
        second_event = second.events.read()[0]
        assert first_event.run_id == "run-1"
        assert first_event.agent_kind == "judge"
        assert first_event.data == AgentOutputChunkData(channel="assistant", content="first")
        assert second_event.run_id == "run-2"
        assert second_event.agent_kind == "implementer"
        assert second_event.data == AgentOutputChunkData(channel="assistant", content="second")
    finally:
        first.close()
        second.close()
