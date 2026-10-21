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
from vibesys.run import EventJournal
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import Git, Project
from vs_proto.api import API_SCHEMA_HASH, RUN_LOG_MAX_RECORD_BYTES, STORE_SCHEMA_HASH
from vs_runlog.api import RunLogReader
from vs_runtime.api.infrastructure import (
    AgentExecutionFinished,
    AgentExecutionStarted,
    AgentExecutionStatus,
)
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api.testing import FakeProcesses, ManualClock, ProcessScript, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import ProcessSpec


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
                reasoning_effort="high",
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
        assert (started.data.provider, started.data.model, started.data.reasoning_effort) == (
            "test",
            "model-for-implementer",
            "high",
        )
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


@pytest.mark.parametrize("clock_start", [-0.5, 1234.25])
def test_live_tee_includes_buffered_events_and_increments_epochs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock_start: float
) -> None:

    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    project = Project.open(root)
    clock = ManualClock(start=clock_start)
    journal = EventJournal(clock, SimThreads())
    journal.emit(CoreEventType.RUN_STARTED, "buffered")
    journal.enable_runlog(project, "run-1")
    journal.attach(Project.log_directory_for(project.root, "run-1"), "run-1")
    journal.emit(CoreEventType.RUN_FINISHED, "live")
    journal.enable_runlog(project, "run-1")
    journal.close()
    restarted = EventJournal(clock, SimThreads())
    restarted.enable_runlog(project, "run-1")
    restarted.attach(Project.log_directory_for(project.root, "run-1"), "run-1")
    restarted.emit(CoreEventType.RUN_STARTED, "restarted")
    restarted.close()
    with Project.run_log_path_for(project.root, "run-1").open("rb") as stream:
        records = list(RunLogReader(stream))
    assert [record.seq for record in records] == [1, 2, 3, 4, 5]
    assert all(
        record.at is not None and record.at.to_datetime() == journal.read()[0].timestamp
        for record in records
    )
    assert [record.epoch for record in records] == [1, 1, 1, 2, 2]
    headers = [
        record.body.value
        for record in records
        if record.body is not None and record.body.field == "epoch_started"
    ]
    assert [header.epoch for header in headers] == [1, 2]
    assert all(header.run_id == "run-1" for header in headers)
    assert all(header.api_schema_hash == API_SCHEMA_HASH for header in headers)
    assert all(header.store_schema_hash == STORE_SCHEMA_HASH for header in headers)
    lines = Project.event_journal_path_for(project.root, "run-1").read_text().splitlines()
    assert [
        record.body.value.json
        for record in records
        if record.body is not None and record.body.field == "legacy_event"
    ] == lines


def test_tee_failure_logs_once_and_keeps_journal_and_subscribers_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:

    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    project = Project.open(root)
    journal = EventJournal(ManualClock(), SimThreads())
    journal.enable_runlog(project, "run-1")
    journal.attach(Project.log_directory_for(project.root, "run-1"), "run-1")
    seen = []
    journal.subscribe(seen.append)
    journal.emit(CoreEventType.RUN_STARTED, "x" * RUN_LOG_MAX_RECORD_BYTES)
    for _ in range(2):
        journal.emit(CoreEventType.RUN_STARTED)
    journal.close()
    assert len(journal.read()) == len(seen) == 3
    assert len(Project.event_journal_path_for(project.root, "run-1").read_text().splitlines()) == 3
    assert sum("tee disabled" in record.message for record in caplog.records) == 1


def test_tee_initialization_failure_is_additive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:

    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    project = Project.open(root)
    path = Project.run_log_path_for(project.root, "run-1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"corrupt!")
    journal = EventJournal(ManualClock(), SimThreads())
    journal.enable_runlog(project, "run-1")
    journal.enable_runlog(project, "run-1")
    journal.attach(path.parent, "run-1")
    journal.emit(CoreEventType.RUN_STARTED)
    assert len(journal.read()) == 1
    assert path.read_bytes() == b"corrupt!"
    assert sum("tee disabled" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("dirty", [False, True])
def test_epoch_records_checkout_provenance_at_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dirty: bool,
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    project = Project.open(root)
    tree_hash = "ab" * 20

    def script(spec: ProcessSpec) -> ProcessScript:
        if spec.argv[1:] == ("rev-parse", "HEAD^{tree}"):
            return ProcessScript(stdout=f"{tree_hash}\n".encode())
        assert spec.argv[1:] == ("status", "--porcelain", "--untracked-files=normal")
        return ProcessScript(stdout=b"?? new.py\n" if dirty else b"")

    clock = ManualClock()
    processes = FakeProcesses(script, pass_time=clock.advance)
    journal = EventJournal(clock, SimThreads())
    journal.enable_runlog(project, "run-1", git=Git(processes), checkout_root=checkout)
    journal.enable_runlog(project, "run-1", git=Git(processes), checkout_root=checkout)
    journal.close()
    with Project.run_log_path_for(root, "run-1").open("rb") as stream:
        header = next(iter(RunLogReader(stream))).body
    assert header is not None
    assert header.field == "epoch_started"
    assert header.value.tree_hash == tree_hash
    assert header.value.dirty is dirty
    assert len(processes.ran) == 2


@pytest.mark.parametrize("failure_stage", ["tree", "status"])
@pytest.mark.parametrize("script", [ProcessScript(returncode=1), ProcessScript(duration_s=1)])
def test_unavailable_checkout_metadata_does_not_disable_the_tee(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: ProcessScript,
    failure_stage: str,
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    project = Project.open(root)
    clock = ManualClock()

    def probe(spec: ProcessSpec) -> ProcessScript:
        if failure_stage == "status" and spec.argv[1:] == ("rev-parse", "HEAD^{tree}"):
            return ProcessScript(stdout=("ab" * 20).encode())
        return script

    processes = FakeProcesses(probe, pass_time=clock.advance)
    journal = EventJournal(clock, SimThreads())
    journal.enable_runlog(project, "run-1", git=Git(processes), checkout_root=checkout)
    journal.attach(Project.log_directory_for(root, "run-1"), "run-1")
    journal.emit(CoreEventType.RUN_STARTED)
    journal.close()
    with Project.run_log_path_for(root, "run-1").open("rb") as stream:
        records = list(RunLogReader(stream))
    assert len(records) == 2
    header = records[0].body
    assert header is not None
    assert header.field == "epoch_started"
    assert header.value.tree_hash == ""
    assert header.value.dirty is False
