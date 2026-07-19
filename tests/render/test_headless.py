"""Tests for the headless terminal renderer (synthetic events in, text out)."""

from io import StringIO

from vibesys.render import HeadlessRenderer, TodoDisplay
from vibesys.server.events import (
    AgentOutputChunkData,
    AgentStatusData,
    EventData,
    EventType,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
    make_event,
)


def _render(*payloads: tuple[EventType, EventData], **kwargs) -> str:
    out = StringIO()
    renderer = HeadlessRenderer(out=out, **kwargs)
    for event_type, data in payloads:
        renderer.handle(make_event(event_type, data=data))
    return out.getvalue()


def _chunk(content: str, channel: str = "assistant", status: AgentStatusData | None = None):
    return (
        EventType.AGENT_OUTPUT_CHUNK,
        AgentOutputChunkData(channel=channel, content=content, status=status),  # type: ignore[arg-type]
    )


_STATUS = AgentStatusData(
    agent_label="Implementer",
    elapsed_seconds=12.34,
    input_tokens=20_100,
    context_window=1_000_000,
)


class TestAssistantStreaming:
    def test_tokens_stream_without_newlines(self):
        assert _render(_chunk("hel"), _chunk("lo")) == "hello"

    def test_prefix_written_once_at_line_start(self):
        out = _render(_chunk("hel", status=_STATUS), _chunk("lo", status=_STATUS))
        assert out == "[Implementer | 12.3s | 20k/1.0M] hello"

    def test_no_prefix_for_anonymous_status(self):
        assert _render(_chunk("hi")) == "hi"

    def test_line_broken_before_next_surface(self):
        out = _render(_chunk("partial"), _chunk("diag\n", channel="diagnostic"))
        assert out == "partial\ndiag\n"


class TestAnalysisChannel:
    def test_rendered_as_prefixed_line(self):
        out = _render(_chunk("thinking hard", channel="analysis", status=_STATUS))
        assert out == "[Implementer | 12.3s | 20k/1.0M] thinking hard\n"


class TestBlockChannels:
    def test_diagnostic_rendered_verbatim(self):
        out = _render(_chunk("=== ROUND START ===\n", channel="diagnostic"))
        assert out == "=== ROUND START ===\n"

    def test_prompt_truncated_with_pointer_to_log(self):
        out = _render(_chunk("x" * 30 + "\n", channel="prompt"), max_text_len=20)
        assert out == "x" * 20 + "\n... [10 more chars, see log for full text]\n"

    def test_short_prompt_not_truncated(self):
        out = _render(_chunk("short\n", channel="prompt"), max_text_len=20)
        assert out == "short\n"


class TestToolEvents:
    def test_tool_channel_chunks_are_ignored(self):
        # Tool traffic renders from the typed events; the tool-channel chunks
        # exist only for wire compatibility and must not double-render.
        assert _render(_chunk("→ shell({})\n", channel="tool")) == ""

    def test_tool_call_line(self):
        out = _render(
            (EventType.TOOL_CALL, ToolCallData(tool="shell", args={"cmd": "ls"}, status=_STATUS))
        )
        assert out == '\n[Implementer | 12.3s | 20k/1.0M] → shell(cmd="ls")\n'

    def test_tool_call_args_truncated(self):
        long = "x" * 200
        out = _render((EventType.TOOL_CALL, ToolCallData(tool="shell", args={"cmd": long})))
        assert long not in out
        assert "x" * 80 + "..." in out

    def test_non_string_args_rendered_as_json(self):
        out = _render((EventType.TOOL_CALL, ToolCallData(tool="t", args={"n": 3})))
        assert "n=3" in out

    def test_tool_result_indented(self):
        out = _render((EventType.TOOL_RESULT, ToolResultData(tool="shell", content="a\nb")))
        assert out == "  a\n  b\n"

    def test_tool_result_truncated(self):
        out = _render(
            (EventType.TOOL_RESULT, ToolResultData(tool="shell", content="a" * 30)),
            max_result_len=10,
        )
        assert out == "  " + "a" * 10 + "...\n"


class TestTodoRendering:
    def _todos(self):
        return [
            TodoItemData(content="Set up project", status="completed"),
            TodoItemData(content="Implement handlers", status="in_progress"),
            TodoItemData(content="Add tests", status="pending"),
        ]

    def test_renders_box_with_items(self):
        out = _render((EventType.TODO_UPDATE, TodoUpdateData(todos=self._todos())))
        assert "┌─ Todo" in out
        assert "Set up project" in out
        assert "Implement handlers" in out
        assert "Add tests" in out

    def test_status_indicators(self):
        out = _render((EventType.TODO_UPDATE, TodoUpdateData(todos=self._todos())))
        assert "✓" in out  # completed
        assert "▶" in out  # in_progress
        assert "○" in out  # pending

    def test_unknown_status_degrades(self):
        todos = [TodoItemData(content="odd", status="unknown-state")]
        out = _render((EventType.TODO_UPDATE, TodoUpdateData(todos=todos)))
        assert "? odd" in out

    def test_plain_mode_has_no_ansi_colors(self):
        out = _render((EventType.TODO_UPDATE, TodoUpdateData(todos=self._todos())), color=False)
        assert "\033[3" not in out  # no color codes
        assert "✓" in out


class TestTodoDisplay:
    def test_clears_previous_lines(self):
        buf = StringIO()
        td = TodoDisplay(file=buf)
        td.update([TodoItemData(content="task one", status="pending")])
        buf.truncate(0)
        buf.seek(0)
        td.update(
            [
                TodoItemData(content="task one", status="completed"),
                TodoItemData(content="task two", status="pending"),
            ]
        )
        second_output = buf.getvalue()
        # Should contain ANSI cursor-up escape to overwrite previous block
        assert "\033[" in second_output
        assert "A" in second_output

    def test_empty_list_prints_nothing(self):
        buf = StringIO()
        TodoDisplay(file=buf).update([])
        assert buf.getvalue() == ""


class TestIgnoredEvents:
    def test_usage_update_produces_no_output(self):
        out = _render((EventType.USAGE_UPDATE, UsageUpdateData(input_tokens=5)))
        assert out == ""

    def test_event_without_data_produces_no_output(self):
        out = StringIO()
        renderer = HeadlessRenderer(out=out)
        renderer.handle(make_event(EventType.RUN_STARTED))
        assert out.getvalue() == ""
