from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.events import (
    AgentExecutionStartedData,
    AgentOutputChunkData,
    CoreEventType,
    EventStatus,
)
from vibesys.run.event_journal import EventJournal
from vibesys.run.integration import LocalRunIntegration
from vs_runtime.api.infrastructure import (
    AgentExecutionFinished,
    AgentExecutionStarted,
    AgentExecutionStatus,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_journal_flushes_pending_events_and_continues_sequence(tmp_path: Path) -> None:
    journal = EventJournal()
    observed = []
    journal.subscribe(observed.append)

    pending = journal.emit(CoreEventType.RUN_STARTED, status=EventStatus.ACTIVE)
    assert pending.sequence == 0
    assert len(observed) == 1

    journal.attach(tmp_path, "run-1")
    finished = journal.emit(CoreEventType.RUN_FINISHED, status=EventStatus.COMPLETED)

    assert finished.sequence == 2
    assert [event.sequence for event in journal.read()] == [1, 2]
    assert [event.run_id for event in journal.read()] == ["run-1", "run-1"]
    assert len(observed) == 2
    assert (tmp_path / "core-events.jsonl").read_text().count("\n") == 2


def test_journal_replays_durable_history_to_new_subscriber(tmp_path: Path) -> None:
    first = EventJournal()
    first.attach(tmp_path, "run-1")
    first.emit(CoreEventType.RUN_STARTED)

    resumed = EventJournal()
    resumed.attach(tmp_path, "run-1")
    replayed = []
    resumed.subscribe(replayed.append, replay=True)
    resumed.emit(CoreEventType.RUN_FINISHED)

    assert [event.type for event in replayed] == [
        CoreEventType.RUN_STARTED,
        CoreEventType.RUN_FINISHED,
    ]
    assert resumed.latest_sequence == 2


def test_journal_repairs_malformed_final_record_before_appending(tmp_path: Path) -> None:
    first = EventJournal()
    first.attach(tmp_path, "run-1")
    first.emit(CoreEventType.RUN_STARTED)
    path = tmp_path / "core-events.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"sequence":')

    resumed = EventJournal()
    resumed.attach(tmp_path, "run-1")
    resumed.emit(CoreEventType.RUN_FINISHED)

    verified = EventJournal()
    verified.attach(tmp_path, "run-1")
    assert [event.type for event in verified.read()] == [
        CoreEventType.RUN_STARTED,
        CoreEventType.RUN_FINISHED,
    ]
    assert len(path.read_text().splitlines()) == 2


def test_journal_separates_valid_final_record_without_newline(tmp_path: Path) -> None:
    first = EventJournal()
    first.attach(tmp_path, "run-1")
    first.emit(CoreEventType.RUN_STARTED)
    path = tmp_path / "core-events.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))

    resumed = EventJournal()
    resumed.attach(tmp_path, "run-1")
    resumed.emit(CoreEventType.RUN_FINISHED)

    assert len(path.read_text().splitlines()) == 2
    assert [event.sequence for event in resumed.read()] == [1, 2]


def test_agent_lifecycle_adapter_records_complete_invocation(tmp_path: Path) -> None:
    integration = LocalRunIntegration()
    try:
        integration.attach(tmp_path, run_id="run-1")
        integration.agent_execution_event(
            AgentExecutionStarted(
                agent_id="implementer",
                label="round-1",
                execution_id="invocation-1",
                system_prompt="system",
                user_prompt="task",
                driver="mock",
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
    first = LocalRunIntegration()
    second = LocalRunIntegration()
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
