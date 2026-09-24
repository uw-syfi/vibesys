from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

from vibesys.context import _RunContext
from vibesys.events import (
    AgentExecutionStartedData,
    AgentOutputChunkData,
    CoreEventType,
    EventStatus,
)
from vibesys.render import output_sink
from vibesys.run.event_journal import EventJournal
from vibesys.run.integration import LocalRunIntegration
from vibesys.run.paths import RunPaths
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path


class _Answer(BaseModel):
    value: str


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


def test_run_context_records_complete_invocation_lifecycle(tmp_path: Path) -> None:
    integration = LocalRunIntegration()
    try:
        integration.attach(tmp_path, run_id="run-1")
        context = object.__new__(_RunContext)
        uninitialized = cast("Any", context)
        uninitialized.integration = integration
        uninitialized.events = integration.events
        client = FakeAgentClient(driver_name="mock", provider="test")
        client.set_model_for_kind({"implementer": "model-for-implementer"})
        client.enqueue("implementer", _Answer(value="done"))
        uninitialized.agent_client = client
        uninitialized._paths = RunPaths(
            project_root=tmp_path,
            log_dir=tmp_path,
            run_log_path=tmp_path / "run.log",
        )
        uninitialized._progress_stack = []
        uninitialized.gpu_env = dict

        answer = context.invoke(
            kind="implementer",
            system_prompt="system",
            user_prompt="task",
            response_cls=_Answer,
            fallback_factory=lambda: _Answer(value="fallback"),
            round_label="round-1-retry-2",
        )

        assert answer == _Answer(value="done")
        assert client.calls_for("implementer")[0].invocation_id
        assert [event.type for event in integration.events.read()] == [
            CoreEventType.AGENT_EXECUTION_STARTED,
            CoreEventType.PHASE_STARTED,
            CoreEventType.INVOCATION_STARTED,
            CoreEventType.AGENT_EXECUTION_FINISHED,
            CoreEventType.INVOCATION_FINISHED,
            CoreEventType.PHASE_FINISHED,
        ]
        assert {event.execution_id for event in integration.events.read()} != {None}
        started = integration.events.read()[0]
        assert isinstance(started.data, AgentExecutionStartedData)
        assert started.data.attempt == 2
    finally:
        integration.close()


def test_local_integration_owns_output_subscription(tmp_path: Path) -> None:
    integration = LocalRunIntegration()
    integration.attach(tmp_path, run_id="run-1")
    output_sink().agent_output("before close", agent_kind="test")
    integration.close()
    output_sink().agent_output("after close", agent_kind="test")

    output_events = [
        event.data
        for event in integration.events.read()
        if isinstance(event.data, AgentOutputChunkData)
    ]
    assert [event.content for event in output_events] == ["before close"]
