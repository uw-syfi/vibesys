"""Tests for todo extraction/emission in run_agent/run_judge_agent.

Box rendering itself is covered by ``tests/render/test_headless.py``; here we
verify the runners publish structured ``TODO_UPDATE`` events from stream
updates.
"""

from unittest.mock import MagicMock, patch

from vibesys.server.events import TodoUpdateData

# --- Thread ID for state persistence ---


def test_run_agent_passes_thread_id():
    """run_agent must pass a thread_id in config so checkpointer can persist state."""
    from vibesys.agent_runner import run_agent

    agent = MagicMock()
    agent.stream.return_value = iter(
        [
            {"agent": {"messages": [MagicMock(type="ai", content="Done")]}},
        ]
    )

    run_agent(agent, "do stuff")

    config = agent.stream.call_args[1].get("config") or agent.stream.call_args[0][1]
    assert "configurable" in config
    assert "thread_id" in config["configurable"]


def test_run_judge_agent_passes_thread_id():
    """run_judge_agent must pass a thread_id in config so checkpointer can persist state."""
    from vibesys.agent_runner import run_judge_agent
    from vibesys.schemas import JudgeResponse, Verdict

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

    run_judge_agent(agent, "review")

    config = agent.stream.call_args[1].get("config") or agent.stream.call_args[0][1]
    assert "configurable" in config
    assert "thread_id" in config["configurable"]


# --- Todo extraction in run_agent ---


def _capture_todo_updates():
    """Patch the runner's todo publisher, recording each emitted snapshot."""
    updates: list[TodoUpdateData] = []

    def record(todos):
        from vibesys.agent_runner import TodoItemData

        updates.append(
            TodoUpdateData(
                todos=[
                    TodoItemData(content=str(t["content"]), status=str(t["status"])) for t in todos
                ]
            )
        )

    return updates, patch("vibesys.agent_runner.publish_todos", side_effect=record)


def test_run_agent_publishes_todos_from_tools_node():
    """run_agent picks up todos from the tools node (where write_todos Command lands)."""
    from vibesys.agent_runner import run_agent

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

    updates, patcher = _capture_todo_updates()
    with patcher:
        run_agent(agent, "do stuff")

    assert len(updates) == 2
    assert updates[0].todos[0].content == "Setup"
    assert len(updates[1].todos) == 2


def test_run_agent_publishes_todos_from_any_node():
    """run_agent finds todos regardless of which node key they appear under."""
    from vibesys.agent_runner import run_agent

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

    updates, patcher = _capture_todo_updates()
    with patcher:
        run_agent(agent, "do stuff")

    assert len(updates) == 1
    assert updates[0].todos[0].content == "Task A"


def test_run_judge_agent_publishes_todos():
    """run_judge_agent picks up todos from the tools node."""
    from vibesys.agent_runner import run_judge_agent
    from vibesys.schemas import JudgeResponse, Verdict

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

    updates, patcher = _capture_todo_updates()
    with patcher:
        run_judge_agent(agent, "review")

    assert len(updates) == 1
    assert updates[0].todos[0].content == "Review code"


def test_publish_todos_emits_structured_event():
    """publish_todos converts raw stream dicts into a typed TODO_UPDATE event."""
    from vibesys.agent_runner import publish_todos
    from vibesys.render import output_sink
    from vibesys.server.events import EventType, RunEvent

    seen: list[RunEvent] = []
    unsubscribe = output_sink().subscribe(seen.append)
    try:
        publish_todos([{"content": "hello", "status": "completed"}])
    finally:
        unsubscribe()

    assert len(seen) == 1
    event = seen[0]
    assert event.type == EventType.TODO_UPDATE
    assert isinstance(event.data, TodoUpdateData)
    assert event.data.todos[0].content == "hello"
    assert event.data.todos[0].status == "completed"
