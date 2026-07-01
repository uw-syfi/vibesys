"""Tests for TodoDisplay rendering and todo extraction in run_agent/run_judge_agent."""

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from vibe_serve.agents.callbacks import TodoDisplay

# --- TodoDisplay rendering ---


def _make_todo(content: str, status: str) -> dict:
    return {"content": content, "status": status}


def test_todo_display_renders_box():
    """TodoDisplay.update() prints a bordered box with todo items."""
    buf = StringIO()
    td = TodoDisplay(file=buf)
    todos = [
        _make_todo("Set up project", "completed"),
        _make_todo("Implement handlers", "in_progress"),
        _make_todo("Add tests", "pending"),
    ]
    td.update(todos)
    output = buf.getvalue()
    assert "Set up project" in output
    assert "Implement handlers" in output
    assert "Add tests" in output


def test_todo_display_status_indicators():
    """Each status maps to the correct indicator symbol."""
    buf = StringIO()
    td = TodoDisplay(file=buf)
    todos = [
        _make_todo("done task", "completed"),
        _make_todo("active task", "in_progress"),
        _make_todo("waiting task", "pending"),
    ]
    td.update(todos)
    output = buf.getvalue()
    assert "✓" in output  # completed
    assert "▶" in output  # in_progress
    assert "○" in output  # pending


def test_todo_display_clears_previous_lines():
    """On second update, cursor-up + clear sequences should be emitted."""
    buf = StringIO()
    td = TodoDisplay(file=buf)
    todos1 = [_make_todo("task one", "pending")]
    todos2 = [_make_todo("task one", "completed"), _make_todo("task two", "pending")]

    td.update(todos1)
    first_output = buf.getvalue()
    buf.truncate(0)
    buf.seek(0)

    td.update(todos2)
    second_output = buf.getvalue()
    # Should contain ANSI cursor-up escape to overwrite previous block
    assert "\033[" in second_output  # contains escape sequences
    assert "A" in second_output  # cursor up uses \033[<n>A


def test_todo_display_empty_list():
    """Empty todo list should not crash, produces no box."""
    buf = StringIO()
    td = TodoDisplay(file=buf)
    td.update([])
    output = buf.getvalue()
    # With empty todos, nothing meaningful should be printed
    assert output == ""


def test_todo_display_with_dict_objects():
    """Todos as plain dicts (as they arrive from streaming) should work."""
    buf = StringIO()
    td = TodoDisplay(file=buf)
    td.update([{"content": "hello", "status": "completed"}])
    assert "hello" in buf.getvalue()


# --- Thread ID for state persistence ---


def test_run_agent_passes_thread_id():
    """run_agent must pass a thread_id in config so checkpointer can persist state."""
    from vibe_serve.agent_runner import run_agent

    agent = MagicMock()
    agent.stream.return_value = iter(
        [
            {"agent": {"messages": [MagicMock(type="ai", content="Done")]}},
        ]
    )

    with patch("vibe_serve.agent_runner.TodoDisplay"):
        run_agent(agent, "do stuff")

    config = agent.stream.call_args[1].get("config") or agent.stream.call_args[0][1]
    assert "configurable" in config
    assert "thread_id" in config["configurable"]


def test_run_judge_agent_passes_thread_id():
    """run_judge_agent must pass a thread_id in config so checkpointer can persist state."""
    from vibe_serve.agent_runner import run_judge_agent
    from vibe_serve.schemas import JudgeResponse, Verdict

    agent = MagicMock()
    agent.stream.return_value = iter(
        [
            {
                "agent": {
                    "messages": [],
                    "structured_response": JudgeResponse(
                        analysis="Good",
                        feedback="",
                        verdict=Verdict.PASS,
                    ),
                }
            },
        ]
    )

    with patch("vibe_serve.agent_runner.TodoDisplay"):
        run_judge_agent(agent, "review")

    config = agent.stream.call_args[1].get("config") or agent.stream.call_args[0][1]
    assert "configurable" in config
    assert "thread_id" in config["configurable"]


# --- Todo extraction in run_agent ---


def test_run_agent_extracts_todos_from_tools_node():
    """run_agent picks up todos from the tools node (where write_todos Command lands)."""
    from vibe_serve.agent_runner import run_agent

    todos_seen = []

    agent = MagicMock()
    agent.stream.return_value = iter(
        [
            # Todos arrive via the tools node (Command update from write_todos)
            {
                "tools": {
                    "todos": [{"content": "Setup", "status": "in_progress"}],
                    "messages": [],
                }
            },
            {
                "tools": {
                    "todos": [
                        {"content": "Setup", "status": "completed"},
                        {"content": "Code", "status": "in_progress"},
                    ],
                    "messages": [],
                }
            },
            {
                "agent": {
                    "messages": [MagicMock(type="ai", content="Done")],
                }
            },
        ]
    )

    with patch("vibe_serve.agent_runner.TodoDisplay") as MockTD:
        instance = MockTD.return_value
        instance.update.side_effect = lambda t: todos_seen.append(t)
        run_agent(agent, "do stuff")

    assert len(todos_seen) == 2
    assert todos_seen[0][0]["content"] == "Setup"
    assert len(todos_seen[1]) == 2


def test_run_agent_extracts_todos_from_any_node():
    """run_agent finds todos regardless of which node key they appear under."""
    from vibe_serve.agent_runner import run_agent

    todos_seen = []

    agent = MagicMock()
    agent.stream.return_value = iter(
        [
            {
                "some_other_node": {
                    "todos": [{"content": "Task A", "status": "pending"}],
                }
            },
        ]
    )

    with patch("vibe_serve.agent_runner.TodoDisplay") as MockTD:
        instance = MockTD.return_value
        instance.update.side_effect = lambda t: todos_seen.append(t)
        run_agent(agent, "do stuff")

    assert len(todos_seen) == 1
    assert todos_seen[0][0]["content"] == "Task A"


def test_run_judge_agent_extracts_todos():
    """run_judge_agent picks up todos from the tools node."""
    from vibe_serve.agent_runner import run_judge_agent
    from vibe_serve.schemas import JudgeResponse, Verdict

    todos_seen = []

    agent = MagicMock()
    agent.stream.return_value = iter(
        [
            {
                "tools": {
                    "todos": [{"content": "Review code", "status": "in_progress"}],
                    "messages": [],
                }
            },
            {
                "agent": {
                    "messages": [],
                    "structured_response": JudgeResponse(
                        analysis="Good",
                        feedback="",
                        verdict=Verdict.PASS,
                    ),
                }
            },
        ]
    )

    with patch("vibe_serve.agent_runner.TodoDisplay") as MockTD:
        instance = MockTD.return_value
        instance.update.side_effect = lambda t: todos_seen.append(t)
        run_judge_agent(agent, "review")

    assert len(todos_seen) == 1
    assert todos_seen[0][0]["content"] == "Review code"
