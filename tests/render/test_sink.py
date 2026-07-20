"""Tests for the process-global OutputSink emission point."""

import threading
from pathlib import Path

from vibesys.agents.callbacks import AgentLogger
from vibesys.render.sink import OutputSink
from vibesys.server.events import (
    AgentOutputChunkData,
    EventType,
    RunEvent,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
)
from vibesys.server.registry import REGISTRY
from vibesys.server.supervisor import RunSupervisor


def _collect(sink: OutputSink) -> tuple[list[RunEvent], object]:
    seen: list[RunEvent] = []
    unsubscribe = sink.subscribe(seen.append)
    return seen, unsubscribe


class TestSubscription:
    def test_subscriber_receives_events(self):
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.agent_output("hello", channel="assistant")
        assert len(seen) == 1
        assert seen[0].type == EventType.AGENT_OUTPUT_CHUNK
        data = seen[0].data
        assert isinstance(data, AgentOutputChunkData)
        assert data.content == "hello"
        assert data.channel == "assistant"

    def test_unsubscribe_stops_delivery(self):
        sink = OutputSink()
        seen, unsubscribe = _collect(sink)
        sink.agent_output("one")
        unsubscribe()
        sink.agent_output("two")
        assert [e.data.content for e in seen if isinstance(e.data, AgentOutputChunkData)] == ["one"]

    def test_empty_content_is_not_emitted(self):
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.agent_output("")
        sink.todo_update([])
        assert seen == []


class TestTypedEmitters:
    def test_tool_call_event(self):
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.tool_call("shell", {"cmd": "ls"})
        assert seen[0].type == EventType.TOOL_CALL
        data = seen[0].data
        assert isinstance(data, ToolCallData)
        assert data.tool == "shell"
        assert data.args == {"cmd": "ls"}

    def test_tool_call_args_coerced_to_json_safe(self):
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.tool_call("write", {"path": Path("/tmp/x")})
        data = seen[0].data
        assert isinstance(data, ToolCallData)
        # Non-JSON values are repr()'d so the event always serializes.
        assert isinstance(data.args["path"], str)
        seen[0].model_dump_json()

    def test_tool_result_event(self):
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.tool_result("shell", "output text", is_error=True)
        data = seen[0].data
        assert isinstance(data, ToolResultData)
        assert data.tool == "shell"
        assert data.content == "output text"
        assert data.is_error is True

    def test_todo_update_event(self):
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.todo_update([TodoItemData(content="a", status="pending")])
        data = seen[0].data
        assert isinstance(data, TodoUpdateData)
        assert data.todos[0].content == "a"

    def test_usage_update_event(self):
        sink = OutputSink()
        seen, _ = _collect(sink)
        sink.usage_update(12_345, context_window=200_000, model="claude-sonnet-4-6")
        data = seen[0].data
        assert isinstance(data, UsageUpdateData)
        assert data.input_tokens == 12_345
        assert data.context_window == 200_000
        assert data.model == "claude-sonnet-4-6"


class TestSupervisorForwarding:
    def test_events_forwarded_to_active_supervisor(self, tmp_path):
        supervisor = RunSupervisor()
        supervisor.attach(tmp_path / "logs")
        REGISTRY.activate(supervisor)
        try:
            sink = OutputSink()
            sink.agent_output("streamed", channel="assistant")
            sink.tool_call("shell", {"cmd": "ls"})
        finally:
            REGISTRY.deactivate(supervisor)
        events = supervisor.read_events()
        types = [e.type for e in events]
        assert EventType.AGENT_OUTPUT_CHUNK in types
        assert EventType.TOOL_CALL in types

    def test_no_supervisor_no_error(self):
        sink = OutputSink()
        sink.agent_output("standalone")  # must not raise without a supervisor

    def test_logger_metadata_routes_subprocess_thread_events_to_chat(self, tmp_path):
        supervisor = RunSupervisor()
        supervisor.attach(tmp_path / "logs")
        logger = AgentLogger(
            agent_kind="chat",
            round_label="experiment-chat",
            invocation_id="chat-invocation",
        )
        REGISTRY.activate(supervisor)
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
            REGISTRY.deactivate(supervisor)

        events = [
            event
            for event in supervisor.read_events()
            if event.type in (EventType.TOOL_CALL, EventType.TOOL_RESULT)
        ]
        assert [event.agent_kind for event in events] == ["chat", "chat"]
        assert [event.round_label for event in events] == [
            "experiment-chat",
            "experiment-chat",
        ]
        assert [event.invocation_id for event in events] == [
            "chat-invocation",
            "chat-invocation",
        ]
