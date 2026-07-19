"""Tests for provider todo/plan tool-call extraction.

The TUI and headless renderers consume neutral ``todo_update`` events; these
tests pin the adapter that translates each CLI provider's tool convention
into that contract, and the ``AgentLogger`` hook that publishes it.
"""

from vibesys.agents.callbacks import AgentLogger
from vibesys.agents.todos import todos_from_tool_call
from vibesys.render.sink import output_sink
from vibesys.server.events import RunEvent, TodoItemData, TodoUpdateData


class TestProviderShapes:
    def test_claude_code_todo_write(self):
        items = todos_from_tool_call(
            "TodoWrite",
            {
                "todos": [
                    {"content": "Profile the hot loop", "status": "completed"},
                    {"content": "Vectorize", "status": "in_progress", "activeForm": "Vectorizing"},
                ]
            },
        )
        assert items == [
            TodoItemData(content="Profile the hot loop", status="completed"),
            TodoItemData(content="Vectorize", status="in_progress"),
        ]

    def test_opencode_and_gemini_share_the_todos_shape(self):
        for tool in ("todowrite", "write_todos"):
            items = todos_from_tool_call(tool, {"todos": [{"content": "A", "status": "pending"}]})
            assert items == [TodoItemData(content="A", status="pending")]

    def test_gemini_description_key(self):
        items = todos_from_tool_call(
            "write_todos", {"todos": [{"description": "Read the ABI contract"}]}
        )
        assert items == [TodoItemData(content="Read the ABI contract", status="pending")]

    def test_codex_update_plan(self):
        items = todos_from_tool_call(
            "update_plan",
            {
                "explanation": "next steps",
                "plan": [
                    {"step": "Run the benchmark", "status": "in_progress"},
                    {"step": "Tune the queue size", "status": "pending"},
                ],
            },
        )
        assert items == [
            TodoItemData(content="Run the benchmark", status="in_progress"),
            TodoItemData(content="Tune the queue size", status="pending"),
        ]

    def test_codex_todo_list_item_with_completed_booleans(self):
        items = todos_from_tool_call(
            "todo_list",
            {"items": [{"text": "Build", "completed": True}, {"text": "Test", "completed": False}]},
        )
        assert items == [
            TodoItemData(content="Build", status="completed"),
            TodoItemData(content="Test", status="pending"),
        ]


class TestDegradation:
    def test_unrelated_tools_are_not_todo_updates(self):
        assert todos_from_tool_call("Bash", {"command": "ls"}) is None
        assert todos_from_tool_call("Write", {"content": "todos: []"}) is None

    def test_payload_without_the_expected_list_is_no_update(self):
        assert todos_from_tool_call("TodoWrite", {}) is None
        assert todos_from_tool_call("TodoWrite", {"todos": "not-a-list"}) is None
        assert todos_from_tool_call("update_plan", {"plan": {"step": "x"}}) is None

    def test_malformed_entries_are_skipped_not_fatal(self):
        items = todos_from_tool_call(
            "TodoWrite",
            {
                "todos": [
                    "not-a-dict",
                    {"status": "pending"},  # no content
                    {"content": "   ", "status": "pending"},  # blank content
                    {"content": "Real item", "status": "pending"},
                ]
            },
        )
        assert items == [TodoItemData(content="Real item", status="pending")]

    def test_unknown_status_strings_pass_through(self):
        items = todos_from_tool_call(
            "TodoWrite", {"todos": [{"content": "Odd", "status": "deferred"}]}
        )
        assert items == [TodoItemData(content="Odd", status="deferred")]

    def test_missing_status_defaults_to_pending(self):
        items = todos_from_tool_call("TodoWrite", {"todos": [{"content": "Bare"}]})
        assert items == [TodoItemData(content="Bare", status="pending")]


class TestAgentLoggerPublishing:
    def test_cli_todo_tool_call_publishes_a_todo_update(self):
        seen: list[RunEvent] = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger()
            logger.on_tool_call("TodoWrite", {"todos": [{"content": "A", "status": "pending"}]})
        finally:
            unsubscribe()
        updates = [e.data for e in seen if isinstance(e.data, TodoUpdateData)]
        assert len(updates) == 1
        assert updates[0].todos == [TodoItemData(content="A", status="pending")]

    def test_non_todo_tool_call_publishes_no_todo_update(self):
        seen: list[RunEvent] = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger()
            logger.on_tool_call("Bash", {"command": "make"})
        finally:
            unsubscribe()
        assert not any(isinstance(e.data, TodoUpdateData) for e in seen)

    def test_empty_snapshot_is_extracted_but_not_published(self):
        # The sink drops empty lists, so a cleared plan is a no-op on the
        # wire rather than an event with no payload.
        seen: list[RunEvent] = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger()
            logger.on_tool_call("TodoWrite", {"todos": []})
        finally:
            unsubscribe()
        assert not any(isinstance(e.data, TodoUpdateData) for e in seen)
