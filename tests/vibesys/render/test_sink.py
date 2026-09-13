"""Tests for the process-global OutputSink emission point."""

import threading
from collections.abc import Callable
from pathlib import Path

from vibesys.agents.callbacks import AgentLogger
from vibesys.render.sink import OutputSink
from vibesys.run.event_journal import EventJournal
from vibesys.run.events import (
    AgentOutputChunkData,
    CommandResultPayload,
    CoreEvent,
    CoreEventType,
    EventStatus,
    FrameworkSource,
    FrameworkWarningData,
    JsonResultPayload,
    RunConfiguredData,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
)


def _collect(sink: OutputSink) -> tuple[list[CoreEvent], Callable[[], None]]:
    seen: list[CoreEvent] = []
    unsubscribe = sink.subscribe(seen.append)
    return seen, unsubscribe


class TestSubscription:
    def test_subscriber_receives_events(self):  # noqa: ANN201  # tracked: #288
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.agent_output("hello", channel="assistant")
        assert len(seen) == 1
        assert seen[0].type == CoreEventType.AGENT_OUTPUT_CHUNK
        data = seen[0].data
        assert isinstance(data, AgentOutputChunkData)
        assert data.content == "hello"
        assert data.channel == "assistant"

    def test_unsubscribe_stops_delivery(self):  # noqa: ANN201  # tracked: #288
        sink = OutputSink()
        seen, unsubscribe = _collect(sink)
        sink.agent_output("one")
        unsubscribe()
        sink.agent_output("two")
        assert [e.data.content for e in seen if isinstance(e.data, AgentOutputChunkData)] == ["one"]

    def test_empty_content_is_not_emitted(self):  # noqa: ANN201  # tracked: #288
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.agent_output("")
        sink.todo_update([])
        assert seen == []


class TestTypedEmitters:
    def test_tool_call_event(self):  # noqa: ANN201  # tracked: #288
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.tool_call("shell", {"cmd": "ls"}, call_id="call-1")
        assert seen[0].type == CoreEventType.TOOL_CALL
        data = seen[0].data
        assert isinstance(data, ToolCallData)
        assert data.tool == "shell"
        assert data.call_id == "call-1"
        assert data.args == {"cmd": "ls"}

    def test_tool_call_args_coerced_to_json_safe(self):  # noqa: ANN201  # tracked: #288
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.tool_call("write", {"path": Path("/tmp/x")})  # noqa: S108  # tracked: #288
        data = seen[0].data
        assert isinstance(data, ToolCallData)
        # Non-JSON values are repr()'d so the event always serializes.
        assert isinstance(data.args["path"], str)
        seen[0].model_dump_json()

    def test_tool_result_event(self):  # noqa: ANN201  # tracked: #288
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.tool_result("shell", "output text", call_id="call-1", is_error=True)
        data = seen[0].data
        assert isinstance(data, ToolResultData)
        assert data.tool == "shell"
        assert data.call_id == "call-1"
        assert data.content == "output text"
        assert data.is_error is True
        assert data.payload is None

    def test_tool_result_classifies_json_content_when_no_payload_given(self):  # noqa: ANN201  # tracked: #288
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.tool_result("query", '{"count": 3}')
        data = seen[0].data
        assert isinstance(data, ToolResultData)
        assert data.content == '{"count": 3}'
        assert isinstance(data.payload, JsonResultPayload)
        assert data.payload.value == {"count": 3}

    def test_tool_result_provided_payload_preempts_classifier(self):  # noqa: ANN201  # tracked: #288
        sink = OutputSink()
        seen, _ = _collect(sink)
        provided = CommandResultPayload(stdout='{"count": 3}', stderr="", exit_code=0, duration=0.2)
        # JSON-looking content must not be re-guessed when the producer
        # already attached real structure.
        sink.tool_result("shell", '{"count": 3}', payload=provided)
        data = seen[0].data
        assert isinstance(data, ToolResultData)
        assert data.payload == provided

    def test_todo_update_event(self):  # noqa: ANN201  # tracked: #288
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.todo_update([TodoItemData(content="a", status="pending")])
        data = seen[0].data
        assert isinstance(data, TodoUpdateData)
        assert data.todos[0].content == "a"

    def test_usage_update_event(self):  # noqa: ANN201  # tracked: #288
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.usage_update(12_345, context_window=200_000, model="claude-sonnet-4-6")
        data = seen[0].data
        assert isinstance(data, UsageUpdateData)
        assert data.input_tokens == 12_345
        assert data.context_window == 200_000
        assert data.model == "claude-sonnet-4-6"


class TestFrameworkEmitters:
    def test_framework_warning_event(self):  # noqa: ANN201
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.framework_warning(
            "profiler failed",
            detail="boom",
            source=FrameworkSource.LOOP,
            round_label="round-2",
        )
        assert seen[0].type == CoreEventType.FRAMEWORK_WARNING
        data = seen[0].data
        assert isinstance(data, FrameworkWarningData)
        assert data.summary == "profiler failed"
        assert data.detail == "boom"
        assert data.source is FrameworkSource.LOOP
        assert seen[0].round_label == "round-2"

    def test_framework_warning_defaults(self):  # noqa: ANN201
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.framework_warning("skills catalog invalid", source_label="skills")
        data = seen[0].data
        assert isinstance(data, FrameworkWarningData)
        assert data.detail is None
        assert data.source is FrameworkSource.OTHER
        assert data.source_label == "skills"
        assert seen[0].round_label is None

    def test_run_configured_keeps_first_objective_line_only(self):  # noqa: ANN201
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.run_configured(
            run_log_path="/logs/run.log",
            project_root="/work/project",
            objective="\n  \nMake the queue fast.\nSecond paragraph.",
            search_policy="pareto-ucb",
            benchmark_contract=True,
            pareto_objectives="[latency(min)]",
        )
        assert seen[0].type == CoreEventType.RUN_CONFIGURED
        data = seen[0].data
        assert isinstance(data, RunConfiguredData)
        assert data.objective == "Make the queue fast."
        assert data.model is None
        assert data.search_policy == "pareto-ucb"
        assert data.benchmark_contract is True
        assert data.pareto_objectives == "[latency(min)]"

    def test_run_configured_without_objective(self):  # noqa: ANN201
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.run_configured(run_log_path="/logs/run.log", project_root="/p", model="m")
        data = seen[0].data
        assert isinstance(data, RunConfiguredData)
        assert data.objective is None
        assert data.model == "m"

    def test_gate_events_carry_envelope_status(self):  # noqa: ANN201
        from vibesys.run.events import GateFinishedData, GateKind, GateStartedData  # noqa: PLC0415

        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.emit(
            CoreEventType.GATE_STARTED,
            data=GateStartedData(gate=GateKind.ACCURACY, command="check"),
            status=EventStatus.ACTIVE,
        )
        sink.emit(
            CoreEventType.GATE_FINISHED,
            data=GateFinishedData(gate=GateKind.ACCURACY, output_tail="bad"),
            status=EventStatus.FAILED,
        )
        assert [e.status for e in seen] == [EventStatus.ACTIVE, EventStatus.FAILED]


class TestComposition:
    def test_events_can_be_recorded_by_the_core_journal(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        journal = EventJournal()
        journal.attach(tmp_path, "test-run")
        sink = OutputSink()
        unsubscribe = sink.subscribe(journal.record)
        sink.agent_output("streamed", channel="assistant")
        sink.tool_call("shell", {"cmd": "ls"})
        unsubscribe()

        events = journal.read()
        types = [e.type for e in events]
        assert CoreEventType.AGENT_OUTPUT_CHUNK in types
        assert CoreEventType.TOOL_CALL in types

    def test_no_subscriber_no_error(self):  # noqa: ANN201  # tracked: #288
        sink = OutputSink()
        sink.agent_output("standalone")

    def test_logger_metadata_survives_subprocess_thread_emission(self):  # noqa: ANN201  # tracked: #288
        logger = AgentLogger(
            agent_kind="chat",
            round_label="experiment-chat",
            invocation_id="chat-invocation",
        )
        from vibesys.render.sink import output_sink  # noqa: PLC0415  # tracked: #288

        events, unsubscribe = _collect(output_sink())
        try:
            worker = threading.Thread(
                target=lambda: (
                    logger.on_tool_call("execute", {"command": "rg throughput"}),
                    logger.on_tool_result("execute", stdout="round 2: 2400 tok/s"),
                )
            )
            worker.start()
            worker.join(timeout=2)
        finally:
            unsubscribe()

        events = [
            event
            for event in events
            if event.type in (CoreEventType.TOOL_CALL, CoreEventType.TOOL_RESULT)
        ]
        assert [event.agent_kind for event in events] == ["chat", "chat"]
        assert [event.round_label for event in events] == [
            "experiment-chat",
            "experiment-chat",
        ]
        assert [event.execution_id for event in events] == [
            "chat-invocation",
            "chat-invocation",
        ]
