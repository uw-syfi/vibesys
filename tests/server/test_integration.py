"""Tests for the adapter between core run ports and server components."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import pytest
from tests.server.support import build_server_parts

from server.api.protocol import ChatQuery, ChatThreadCreateQuery
from server.chat.factory import ChatAgentResources
from server.diagnostics import DiagnosticScope, DiagnosticSeverity
from server.events import EventStatus, EventType
from server.integration import _CORE_FAILURE_CONTEXTS
from server.journal import DIAGNOSTIC_FAILURE_EVENTS
from vibesys.agents.session_key import AgentSessionKey, SessionScope
from vibesys.run.events import (
    AgentExecutionFinishedData,
    AgentOutputChunkData,
    CoreEventType,
    FrameworkSource,
    FrameworkWarningData,
    GateFinishedData,
    GateKind,
    PhaseData,
    ToolCallData,
)
from vibesys.run.events import (
    EventStatus as CoreEventStatus,
)
from vibesys.run.integration import (
    AgentSelection,
    RunAttachment,
)
from vs_project import AgentRunConfiguration, Project, RunEnvironmentRecord

if TYPE_CHECKING:
    from pathlib import Path


class _ChatClient:
    """Record chat invocations and return a deterministic answer."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke_text(self, **kwargs: Any) -> str:  # noqa: ANN401
        self.calls.append(kwargs)
        return "It improved in round 2."

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Report no conversation, so every turn carries the full prompt."""
        del session_key
        return None

    def last_turn_provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Report no conversation, matching ``provider_session_id``."""
        del session_key
        return None


def _project_run(root: Path) -> tuple[Project, str]:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Make the queue fast.\n", encoding="utf-8")
    project = Project.open(root)
    project.state.create_project("queue")
    manifest = project.state.new_run_manifest(
        "queue",
        run_id="queue-run",
        branch="vibesys/queue-run",
        vibesys_version="0.2.0-test",
        configuration=AgentRunConfiguration(
            outer_loop="agent",
            run_environment=RunEnvironmentRecord(name="local"),
            inner_loop="single-agent",
            interface="inprocess",
            agent_backend="cli",
            compute_backend="cpu",
            profiler="none",
            max_rounds=3,
            max_retries_per_round=1,
            judge_every=1,
            official_eval_every=1,
            memory_layout="files",
        ),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


def test_core_events_project_to_wire_journal_and_execution_activity(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    handle = parts.integration.invocations.start(
        "implementer", "round-1", "work", driver="agentshim", provider="codex"
    )
    assert handle.execution_id is not None

    parts.integration.events.emit(
        CoreEventType.TOOL_CALL,
        agent_kind="implementer",
        round_label="round-1",
        execution_id=handle.execution_id,
        data=ToolCallData(tool="Bash", args={}),
    )
    parts.integration.events.emit(
        CoreEventType.AGENT_OUTPUT_CHUNK,
        agent_kind="implementer",
        round_label="round-1",
        execution_id=handle.execution_id,
        data=AgentOutputChunkData(channel="assistant", content="working"),
    )

    assert [
        event.type
        for event in parts.journal.read()
        if event.type in {EventType.TOOL_CALL, EventType.AGENT_OUTPUT_CHUNK}
    ] == [EventType.TOOL_CALL, EventType.AGENT_OUTPUT_CHUNK]
    activity = parts.api.snapshot().active_executions[0].activity
    assert activity.mode == "tool"
    assert activity.tool == "Bash"
    assert (tmp_path / "core-events.jsonl").is_file()


def test_framework_events_bypass_execution_stamping_and_lift_warnings(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.integration.invocations.start("implementer", "round-1", "work")

    # All three events arrive without an agent_kind while an implementer
    # execution is active. Only the presentation event may inherit it.
    parts.integration.events.emit(
        CoreEventType.AGENT_OUTPUT_CHUNK,
        data=AgentOutputChunkData(channel="assistant", content="working"),
    )
    parts.integration.events.emit(
        CoreEventType.GATE_FINISHED,
        status=CoreEventStatus.FAILED,
        round_label="round-1",
        data=GateFinishedData(gate=GateKind.ACCURACY, output_tail="mismatch"),
    )
    parts.integration.events.emit(
        CoreEventType.FRAMEWORK_WARNING,
        data=FrameworkWarningData(
            summary="profiler failed",
            detail="boom",
            source=FrameworkSource.LOOP,
        ),
    )

    events = parts.journal.read()
    chunk = next(e for e in events if e.type is EventType.AGENT_OUTPUT_CHUNK)
    assert chunk.agent_kind == "implementer"
    gate = next(e for e in events if e.type is EventType.GATE_FINISHED)
    assert gate.agent_kind is None
    assert gate.round_label == "round-1"
    assert gate.status is EventStatus.FAILED
    # A failed gate is an expected outcome, not a fault: no diagnostic.
    assert gate.diagnostic is None
    warning = next(e for e in events if e.type is EventType.FRAMEWORK_WARNING)
    assert warning.agent_kind is None
    assert warning.diagnostic is not None
    assert warning.diagnostic.severity is DiagnosticSeverity.WARNING
    assert warning.diagnostic.summary == "profiler failed"
    assert warning.diagnostic.detail == "boom"
    assert warning.diagnostic.source == "loop"


def test_failed_core_events_project_with_synthesized_diagnostics(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    handle = parts.integration.invocations.start("implementer", "round-1", "work")

    parts.integration.events.emit(
        CoreEventType.AGENT_EXECUTION_FINISHED,
        status=CoreEventStatus.FAILED,
        agent_kind="implementer",
        round_label="round-1",
        execution_id=handle.execution_id,
        data=AgentExecutionFinishedData(error="RuntimeError: boom"),
    )
    parts.integration.events.emit(
        CoreEventType.PHASE_FINISHED,
        status=CoreEventStatus.FAILED,
        agent_kind="implementer",
        round_label="round-1",
        execution_id=handle.execution_id,
        data=PhaseData(phase="implementer"),
    )
    parts.integration.events.emit(
        CoreEventType.RUN_FAILED,
        "RuntimeError: boom",
        status=CoreEventStatus.FAILED,
    )

    events = parts.journal.read()
    execution = next(e for e in events if e.type is EventType.AGENT_EXECUTION_FINISHED)
    assert execution.diagnostic is not None
    assert execution.diagnostic.code == "core_failure"
    assert execution.diagnostic.summary == "RuntimeError: boom"
    assert execution.diagnostic.detail is None
    assert execution.diagnostic.scope is DiagnosticScope.INVOCATION
    assert execution.diagnostic.severity is DiagnosticSeverity.ERROR
    assert execution.diagnostic.source == "loop"
    phase = next(e for e in events if e.type is EventType.PHASE_FINISHED)
    assert phase.diagnostic is not None
    assert phase.diagnostic.summary == "Phase implementer failed"
    assert phase.diagnostic.scope is DiagnosticScope.PHASE
    assert phase.diagnostic.severity is DiagnosticSeverity.ERROR
    terminal = next(e for e in events if e.type is EventType.RUN_FAILED)
    assert terminal.diagnostic is not None
    assert terminal.diagnostic.summary == "RuntimeError: boom"
    assert terminal.diagnostic.scope is DiagnosticScope.RUN
    assert terminal.diagnostic.severity is DiagnosticSeverity.FATAL


def test_core_failure_synthesis_covers_the_journal_failure_invariant() -> None:
    # Every event the journal refuses without a diagnostic must have a
    # synthesis entry, or a diagnostic-less core failure would crash append.
    assert set(_CORE_FAILURE_CONTEXTS) == DIAGNOSTIC_FAILURE_EVENTS


def test_invocation_adapter_applies_steering_without_emitting_duplicate_lifecycle(
    tmp_path: Path,
) -> None:
    parts = build_server_parts(tmp_path)
    parts.controller.steer("measure latency first")

    handle = parts.integration.invocations.start("implementer", "round-1", "work")

    assert "measure latency first" in handle.user_prompt
    assert not any(
        event.type is EventType.AGENT_EXECUTION_STARTED for event in parts.journal.read()
    )
    assert len(parts.api.snapshot().active_executions) == 1
    parts.integration.invocations.finish(
        "implementer", "round-1", result="done", execution_id=handle.execution_id
    )
    assert parts.api.snapshot().active_executions == []


def test_attach_run_installs_chat_with_isolated_session_state(tmp_path):  # noqa: ANN001, ANN201
    project, run_id = _project_run(tmp_path / "project")
    client = _ChatClient()
    closed: list[str] = []

    def build_agent(
        _attachment: RunAttachment,
        selection: AgentSelection,
        thread_id: str | None,
        shared_state_dir: Path,
    ) -> ChatAgentResources:
        assert selection == AgentSelection(driver="agentshim", provider="codex", model="gpt-test")
        assert thread_id is None
        return ChatAgentResources(
            client=client,
            close=lambda: closed.append("closed"),
            log=lambda _message: None,
            flush_logs=lambda: None,
            environment=dict,
            progress=lambda: None,
            agent_shared_state_dir=str(shared_state_dir),
        )

    parts = build_server_parts(chat_agent_builder=build_agent)
    detach = parts.integration.attach_run(
        RunAttachment(
            project=project,
            run_id=run_id,
            workspace=project.root,
            log_dir=project.state.log_directory(run_id),
            agent_backend="cli",
            agent_defaults=AgentSelection(driver="agentshim", provider="codex", model="gpt-test"),
            agent_runtime=cast("Any", None),
        )
    )
    assert detach is not None

    response = parts.api.execute(ChatQuery(text="what improved?"))

    assert response.chat is not None
    assert response.chat.answer == "It improved in round 2."
    assert client.calls[0]["reuse_session"] is True
    assert client.calls[0]["session_key"] == AgentSessionKey(SessionScope.CHAT, "default")
    assert client.calls[0]["user_prompt"] == "what improved?"
    transcript = project.state.log_directory(run_id).parent / "server/chat/conversation.jsonl"
    assert json.loads(transcript.read_text()) == {
        "question": "what improved?",
        "answer": "It improved in round 2.",
    }
    assert not (project.root / ".vibesys/server").exists()
    assert str(transcript.parent) in client.calls[0]["system_prompt"]
    detach()
    assert closed == ["closed"]


def test_non_cli_run_rejects_new_chat_threads(tmp_path):  # noqa: ANN001, ANN201
    project, run_id = _project_run(tmp_path / "project")
    client = _ChatClient()

    def build_agent(
        _attachment: RunAttachment,
        _selection: AgentSelection,
        _thread_id: str | None,
        shared_state_dir: Path,
    ) -> ChatAgentResources:
        return ChatAgentResources(
            client=client,
            close=lambda: None,
            log=lambda _message: None,
            flush_logs=lambda: None,
            environment=dict,
            progress=lambda: None,
            agent_shared_state_dir=str(shared_state_dir),
        )

    parts = build_server_parts(chat_agent_builder=build_agent)
    parts.integration.attach_run(
        RunAttachment(
            project=project,
            run_id=run_id,
            workspace=project.root,
            log_dir=project.state.log_directory(run_id),
            agent_backend="stub",
            agent_defaults=AgentSelection(driver="agentshim", provider="codex", model="gpt-test"),
            agent_runtime=cast("Any", None),
        )
    )

    with pytest.raises(ValueError, match="require the CLI agent backend"):
        parts.api.execute(ChatThreadCreateQuery(provider="codex", model="gpt-test"))


def test_close_is_idempotent_and_stops_event_projection(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.integration.close()
    parts.integration.close()

    parts.integration.events.emit(
        CoreEventType.AGENT_OUTPUT_CHUNK,
        data=AgentOutputChunkData(channel="assistant", content="after close"),
    )

    assert not any(event.type is EventType.AGENT_OUTPUT_CHUNK for event in parts.journal.read())


def test_run_started_expected_roles_round_trip_through_the_wire_bridge() -> None:
    """The core payload bridges to the wire model with and without the field."""
    from server.events import RunStartedData  # noqa: PLC0415
    from server.integration import _EVENT_DATA_ADAPTER  # noqa: PLC0415
    from vibesys.run.events import RunStartedData as CoreRunStartedData  # noqa: PLC0415

    advertised = CoreRunStartedData(
        outer_loop="plain",
        input="objective",
        max_rounds=3,
        expected_roles=("implementer", "judge", "perf_eval"),
    )
    wire = _EVENT_DATA_ADAPTER.validate_python(advertised.model_dump(mode="python"))
    assert isinstance(wire, RunStartedData)
    assert wire.expected_roles == ("implementer", "judge", "perf_eval")

    # A recording that predates the field must still validate, as empty.
    legacy = _EVENT_DATA_ADAPTER.validate_python(
        {"kind": "run_started", "outer_loop": "plain", "input": "objective", "max_rounds": 3}
    )
    assert isinstance(legacy, RunStartedData)
    assert legacy.expected_roles == ()
