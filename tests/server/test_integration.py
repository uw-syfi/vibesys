"""Tests for the adapter between core run ports and server components."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any, cast

import pytest
from tests.server.support import build_server_parts

from server.api.protocol import ChatQuery, ChatThreadCreateQuery
from server.chat.factory import ChatAgentResources
from server.diagnostics import DiagnosticScope, DiagnosticSeverity
from server.events import EventStatus, EventType
from server.integration import _CORE_FAILURE_CONTEXTS
from server.journal import DIAGNOSTIC_FAILURE_EVENTS
from server.run_attachment import AgentSelection, RunAttachment
from vibesys.events import (
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    AgentOutputChunkData,
    CoreEventType,
    FrameworkSource,
    FrameworkWarningData,
    GateFinishedData,
    GateKind,
    InvocationFinishedData,
    PhaseData,
    ToolCallData,
)
from vibesys.events import (
    EventStatus as CoreEventStatus,
)
from vibesys.render import output_sink
from vs_agent.api import AgentSessionKey, SessionScope
from vs_agent.api.testing import FakeAgentClient
from vs_project import AgentRunConfiguration, Project, RunEnvironmentRecord

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import RunSession


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


def _emit_execution_started(  # noqa: PLR0913
    parts: Any,  # noqa: ANN401
    kind: str,
    round_label: str,
    user_prompt: str,
    *,
    driver: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    """Emit the core start event a real invocation boundary would produce."""
    execution_id = uuid.uuid4().hex
    parts.core_events.emit(
        CoreEventType.AGENT_EXECUTION_STARTED,
        status=CoreEventStatus.ACTIVE,
        agent_kind=kind,
        round_label=round_label,
        execution_id=execution_id,
        data=AgentExecutionStartedData(
            stage=kind,
            user_prompt=user_prompt,
            activity=AgentExecutionActivityData(mode="thinking", summary="Working"),
            driver=driver,
            provider=provider,
            model=model,
        ),
    )
    return execution_id


def test_core_events_project_to_wire_journal_and_execution_activity(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    execution_id = _emit_execution_started(
        parts, "implementer", "round-1", "work", driver="agentshim", provider="codex"
    )

    output_sink().emit(
        CoreEventType.TOOL_CALL,
        agent_kind="implementer",
        round_label="round-1",
        execution_id=execution_id,
        data=ToolCallData(tool="Bash", args={}),
    )
    output_sink().emit(
        CoreEventType.AGENT_OUTPUT_CHUNK,
        agent_kind="implementer",
        round_label="round-1",
        execution_id=execution_id,
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
    _emit_execution_started(parts, "implementer", "round-1", "work")

    # All three events arrive without an agent_kind while an implementer
    # execution is active. Only the presentation event may inherit it.
    output_sink().emit(
        CoreEventType.AGENT_OUTPUT_CHUNK,
        data=AgentOutputChunkData(channel="assistant", content="working"),
    )
    parts.core_events.emit(
        CoreEventType.GATE_FINISHED,
        status=CoreEventStatus.FAILED,
        round_label="round-1",
        data=GateFinishedData(gate=GateKind.ACCURACY, output_tail="mismatch"),
    )
    parts.core_events.emit(
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
    execution_id = _emit_execution_started(parts, "implementer", "round-1", "work")

    parts.core_events.emit(
        CoreEventType.AGENT_EXECUTION_FINISHED,
        status=CoreEventStatus.FAILED,
        agent_kind="implementer",
        round_label="round-1",
        execution_id=execution_id,
        data=AgentExecutionFinishedData(error="RuntimeError: boom"),
    )
    parts.core_events.emit(
        CoreEventType.PHASE_FINISHED,
        status=CoreEventStatus.FAILED,
        agent_kind="implementer",
        round_label="round-1",
        execution_id=execution_id,
        data=PhaseData(phase="implementer"),
    )
    parts.core_events.emit(
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
    # The phase failure shares the execution's id, so it folds onto the same
    # diagnostic (the invocation's error) rather than minting a distinct
    # phase-scoped report the frontend would count as a second failure.
    phase = next(e for e in events if e.type is EventType.PHASE_FINISHED)
    assert phase.diagnostic is not None
    assert phase.diagnostic.id == execution.diagnostic.id
    assert phase.diagnostic.summary == "RuntimeError: boom"
    assert phase.diagnostic.scope is DiagnosticScope.INVOCATION
    # The run-scoped terminal failure carries no execution_id and stays its own
    # fatal diagnostic.
    terminal = next(e for e in events if e.type is EventType.RUN_FAILED)
    assert terminal.diagnostic is not None
    assert terminal.diagnostic.id != execution.diagnostic.id
    assert terminal.diagnostic.summary == "RuntimeError: boom"
    assert terminal.diagnostic.scope is DiagnosticScope.RUN
    assert terminal.diagnostic.severity is DiagnosticSeverity.FATAL


def test_execution_failure_cascade_folds_to_one_diagnostic_per_execution(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    first_id = _emit_execution_started(parts, "implementer", "round-1", "work")
    for event_type, data in (
        (
            CoreEventType.AGENT_EXECUTION_FINISHED,
            AgentExecutionFinishedData(error="RuntimeError: boom"),
        ),
        (CoreEventType.INVOCATION_FINISHED, InvocationFinishedData(error="RuntimeError: boom")),
        (CoreEventType.PHASE_FINISHED, PhaseData(phase="implementer")),
    ):
        parts.core_events.emit(
            event_type,
            status=CoreEventStatus.FAILED,
            agent_kind="implementer",
            round_label="round-1",
            execution_id=first_id,
            data=data,
        )

    # A second execution failing must not inherit the first's cached diagnostic.
    second_id = _emit_execution_started(parts, "judge", "round-1", "review")
    parts.core_events.emit(
        CoreEventType.AGENT_EXECUTION_FINISHED,
        status=CoreEventStatus.FAILED,
        agent_kind="judge",
        round_label="round-1",
        execution_id=second_id,
        data=AgentExecutionFinishedData(error="ValueError: nope"),
    )

    events = parts.journal.read()
    first_diagnostics = [
        e.diagnostic for e in events if e.execution_id == first_id and e.diagnostic is not None
    ]
    # The journal folds the legacy invocation lifecycle onto the canonical one,
    # so the surviving cascade is the agent-execution and phase failures; both
    # must share one diagnostic identity carrying the invocation's error.
    assert len(first_diagnostics) >= 2
    ids = {d.id for d in first_diagnostics}
    assert len(ids) == 1
    assert all(d.summary == "RuntimeError: boom" for d in first_diagnostics)

    second_event = next(
        e
        for e in events
        if e.execution_id == second_id and e.type is EventType.AGENT_EXECUTION_FINISHED
    )
    assert second_event.diagnostic is not None
    assert second_event.diagnostic.id not in ids
    assert second_event.diagnostic.summary == "ValueError: nope"


def test_core_failure_synthesis_covers_the_journal_failure_invariant() -> None:
    # Every event the journal refuses without a diagnostic must have a
    # synthesis entry, or a diagnostic-less core failure would crash append.
    assert set(_CORE_FAILURE_CONTEXTS) == DIAGNOSTIC_FAILURE_EVENTS


def test_track_started_does_not_double_track_a_repeated_start_event(
    tmp_path: Path,
) -> None:
    """`ExecutionTracker.track_started` is idempotent for a repeated start event.

    Core now mints the execution id and emits `AGENT_EXECUTION_STARTED`
    itself, at the entry to `_RunContext.invoke`; `project_event` projects it
    onto both the wire journal and `ExecutionTracker`. Redelivering the same
    start event (e.g. from an at-least-once subscriber) must not double-track
    the execution or double-emit the wire event.
    """
    parts = build_server_parts(tmp_path)

    execution_id = _emit_execution_started(parts, "implementer", "round-1", "work")

    assert len(parts.api.snapshot().active_executions) == 1
    assert (
        sum(event.type is EventType.AGENT_EXECUTION_STARTED for event in parts.journal.read()) == 1
    )

    # Redelivering the identical start event must not double-track.
    parts.core_events.emit(
        CoreEventType.AGENT_EXECUTION_STARTED,
        status=CoreEventStatus.ACTIVE,
        agent_kind="implementer",
        round_label="round-1",
        execution_id=execution_id,
        data=AgentExecutionStartedData(
            stage="implementer",
            user_prompt="work",
            activity=AgentExecutionActivityData(mode="thinking", summary="Working"),
        ),
    )
    assert len(parts.api.snapshot().active_executions) == 1

    parts.core_events.emit(
        CoreEventType.AGENT_EXECUTION_FINISHED,
        status=CoreEventStatus.COMPLETED,
        agent_kind="implementer",
        round_label="round-1",
        execution_id=execution_id,
        data=AgentExecutionFinishedData(result="done"),
    )
    assert parts.api.snapshot().active_executions == []


def test_attach_run_installs_chat_with_isolated_session_state(tmp_path):  # noqa: ANN001, ANN201
    project, run_id = _project_run(tmp_path / "project")
    client = FakeAgentClient().set_text("chat", "It improved in round 2.")
    closed: list[str] = []

    def build_agent(
        _session: RunSession,
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
            mcp_servers=(),
        )

    parts = build_server_parts(chat_agent_builder=build_agent)
    detach = parts.integration._attach_run(  # noqa: SLF001
        RunAttachment(
            project=project,
            run_id=run_id,
            workspace=project.root,
            log_dir=project.state.log_directory(run_id),
            agent_backend="cli",
            agent_defaults=AgentSelection(driver="agentshim", provider="codex", model="gpt-test"),
        ),
        cast("Any", object()),
    )
    assert detach is not None

    response = parts.api.execute(ChatQuery(text="what improved?"))

    assert response.chat is not None
    assert response.chat.answer == "It improved in round 2."
    call = client.calls_for("chat")[0]
    assert call.reuse_session is True
    assert call.session_key == AgentSessionKey(SessionScope.CHAT, "default")
    assert call.user_prompt == "what improved?"
    transcript = project.state.log_directory(run_id).parent / "server/chat/conversation.jsonl"
    assert json.loads(transcript.read_text()) == {
        "question": "what improved?",
        "answer": "It improved in round 2.",
    }
    assert not (project.root / ".vibesys/server").exists()
    assert str(transcript.parent) in call.system_prompt
    detach()
    assert closed == ["closed"]


def test_non_cli_run_rejects_new_chat_threads(tmp_path):  # noqa: ANN001, ANN201
    project, run_id = _project_run(tmp_path / "project")
    client = FakeAgentClient()

    def build_agent(
        _session: RunSession,
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
            mcp_servers=(),
        )

    parts = build_server_parts(chat_agent_builder=build_agent)
    parts.integration._attach_run(  # noqa: SLF001
        RunAttachment(
            project=project,
            run_id=run_id,
            workspace=project.root,
            log_dir=project.state.log_directory(run_id),
            agent_backend="stub",
            agent_defaults=AgentSelection(driver="agentshim", provider="codex", model="gpt-test"),
        ),
        cast("Any", object()),
    )

    with pytest.raises(ValueError, match="require the CLI agent backend"):
        parts.api.execute(ChatThreadCreateQuery(provider="codex", model="gpt-test"))


def test_close_is_idempotent_and_stops_event_projection(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.integration.close()
    parts.integration.close()

    output_sink().emit(
        CoreEventType.AGENT_OUTPUT_CHUNK,
        data=AgentOutputChunkData(channel="assistant", content="after close"),
    )

    assert not any(event.type is EventType.AGENT_OUTPUT_CHUNK for event in parts.journal.read())


def test_run_started_expected_roles_round_trip_through_the_wire_bridge() -> None:
    """The core payload bridges to the wire model with and without the field."""
    from server.events import RunStartedData  # noqa: PLC0415
    from server.integration import _EVENT_DATA_ADAPTER  # noqa: PLC0415
    from vibesys.events import RunStartedData as CoreRunStartedData  # noqa: PLC0415

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
