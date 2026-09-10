"""Unit tests for the Codex provider's command construction and JSONL parser.

These tests exercise :mod:`vibesys._agent_cli.codex` without spawning the real
``codex`` binary. They cover the three correctness fixes:

1. ``_get_command`` / ``_get_resume_command`` include ``--skip-git-repo-check``
   so codex doesn't refuse to run outside a git repo.
2. ``_get_resume_command`` passes ``-`` as the prompt positional so the stdin
   write in :class:`CLIGenerationSession.run` is actually consumed.
3. ``CodexGenerationSession`` captures cumulative token usage from
   ``turn.completed`` events and forwards a ``reasoning`` item's text through
   the event handler.
4. ``_PairedCodexSession`` pairs a generic item's ``item.started`` and
   ``item.completed`` (both mapped to tool-use upstream) by item id into one
   ``on_tool_call`` / ``on_tool_result`` pair.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from agentshim.codex.events import ToolResultEvent

from vibesys._agent_cli.codex import (
    CodexCodingAgent,
    CodexGenerationSession,
    _shell_path_config_args,
)
from vibesys._agent_cli.gemini import GeminiCodingAgent

if TYPE_CHECKING:
    from collections.abc import Mapping


def _agent() -> CodexCodingAgent:
    """Build a CodexCodingAgent without running binary detection."""
    agent = CodexCodingAgent.__new__(CodexCodingAgent)
    agent.binary_path = "/usr/local/bin/codex"
    agent.model = None
    agent.base_config_args = []
    agent.extra_config_args = []
    agent.output_schema_path = None
    return agent


def _session(event_handler=None) -> CodexGenerationSession:  # noqa: ANN001  # tracked: #288
    """Build a CodexGenerationSession without opening pipes."""
    return CodexGenerationSession(
        binary_name="codex",
        env={},
        log_prefix="[Codex]",
        cmd=["codex", "exec", "--json", "-"],
        logger=MagicMock(),
        silent=True,
        event_handler=event_handler,
    )


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


class TestGetCommand:
    def test_shell_path_config_preserves_launcher_path(self):  # noqa: ANN202  # tracked: #288
        assert _shell_path_config_args({"PATH": '/opt/go/bin:/path/with"quote'}) == [
            "--config",
            'shell_environment_policy.set.PATH="/opt/go/bin:/path/with\\"quote"',
        ]

    def test_shell_path_config_omits_missing_path(self):  # noqa: ANN202  # tracked: #288
        assert _shell_path_config_args({}) == []

    def test_initial_command_includes_skip_git_repo_check(self):  # noqa: ANN202  # tracked: #288
        cmd = _agent()._get_command("hello")  # noqa: SLF001  # tracked: #288
        assert "--skip-git-repo-check" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--json" in cmd
        # The subcommand must be ``exec`` (not ``exec resume``).
        assert cmd[1] == "exec"
        assert "resume" not in cmd

    def test_initial_command_includes_model_when_set(self):  # noqa: ANN202  # tracked: #288
        agent = _agent()
        agent.model = "gpt-5"
        cmd = agent._get_command("hello")  # noqa: SLF001  # tracked: #288
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gpt-5"

    def test_initial_command_appends_extra_config_args(self):  # noqa: ANN202  # tracked: #288
        agent = _agent()
        agent.extra_config_args = ["--config", "foo=1"]
        cmd = agent._get_command("hello")  # noqa: SLF001  # tracked: #288
        assert cmd[-2:] == ["--config", "foo=1"]

    def test_reasoning_effort_is_passed_as_codex_config(self):  # noqa: ANN202  # tracked: #288
        agent = _agent()
        agent.set_reasoning_effort("xhigh")
        cmd = agent._get_command("hello")  # noqa: SLF001  # tracked: #288
        assert 'model_reasoning_effort="xhigh"' in cmd

    def test_initial_command_includes_native_output_schema(self):  # noqa: ANN202  # tracked: #288
        agent = _agent()
        agent.set_output_schema_path(".cache/vibesys/response-schemas/judge.json")

        cmd = agent._get_command("hello")  # noqa: SLF001  # tracked: #288

        assert cmd[cmd.index("--output-schema") + 1] == (
            ".cache/vibesys/response-schemas/judge.json"
        )


class TestGetResumeCommand:
    def test_resume_passes_dash_positional(self):  # noqa: ANN202  # tracked: #288
        """Without ``-``, codex exec resume silently ignores stdin."""
        cmd = _agent()._get_resume_command("prompt", "sess-123")  # noqa: SLF001  # tracked: #288
        # The positional args come right after the subcommand path:
        #   codex exec resume <session_id> <prompt>
        assert cmd[:5] == [
            "/usr/local/bin/codex",
            "exec",
            "resume",
            "sess-123",
            "-",
        ]

    def test_resume_command_includes_skip_git_repo_check(self):  # noqa: ANN202  # tracked: #288
        cmd = _agent()._get_resume_command("prompt", "sess-123")  # noqa: SLF001  # tracked: #288
        assert "--skip-git-repo-check" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--json" in cmd

    def test_resume_command_passes_model_and_extra_config(self):  # noqa: ANN202  # tracked: #288
        agent = _agent()
        agent.model = "gpt-5"
        agent.extra_config_args = ["--config", 'mcp_servers.x.command="python"']
        cmd = agent._get_resume_command("prompt", "sess-123")  # noqa: SLF001  # tracked: #288
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gpt-5"  # noqa: PT018  # tracked: #288
        assert cmd[-2:] == ["--config", 'mcp_servers.x.command="python"']

    def test_resume_command_includes_native_output_schema(self):  # noqa: ANN202  # tracked: #288
        agent = _agent()
        agent.set_output_schema_path(".cache/vibesys/response-schemas/implementer.json")

        cmd = agent._get_resume_command("prompt", "sess-123")  # noqa: SLF001  # tracked: #288

        assert cmd[cmd.index("--output-schema") + 1] == (
            ".cache/vibesys/response-schemas/implementer.json"
        )


# ---------------------------------------------------------------------------
# Stream parser
# ---------------------------------------------------------------------------


class TestProcessStdout:
    def test_thread_started_captures_thread_id(self):  # noqa: ANN202  # tracked: #288
        session = _session()
        session._process_stdout(json.dumps({"type": "thread.started", "thread_id": "t-1"}))  # noqa: SLF001  # tracked: #288
        assert session.session_id == "t-1"

    def test_agent_message_captures_last_text(self):  # noqa: ANN202  # tracked: #288
        session = _session()
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "final answer"},
                }
            )
        )
        assert session.final_result == "final answer"

    def test_agent_message_streams_through_on_thinking(self):  # noqa: ANN202  # tracked: #288
        """Assistant text should land in the log as soon as it arrives."""
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "final answer"},
                }
            )
        )
        handler.on_thinking.assert_called_once_with("final answer")

    def test_multiple_agent_messages_keep_last(self):  # noqa: ANN202  # tracked: #288
        session = _session()
        for text in ["first", "second", "third"]:
            session._process_stdout(  # noqa: SLF001  # tracked: #288
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": text},
                    }
                )
            )
        assert session.final_result == "third"

    def test_reasoning_forwards_to_on_thinking(self):  # noqa: ANN202  # tracked: #288
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "reasoning", "text": "I should grep for X"},
                }
            )
        )
        handler.on_thinking.assert_called_once_with("I should grep for X")

    def test_unknown_item_types_fall_back_to_tool_call(self):  # noqa: ANN202  # tracked: #288
        """file_change / mcp_tool_call / todo_list / web_search / error …
        all surface through ``on_tool_call`` so their payloads land in the log."""
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "file_change",
                        "path": "engine.py",
                        "kind": "update",
                    },
                }
            )
        )
        handler.on_tool_call.assert_called_once_with(
            "file_change", {"path": "engine.py", "kind": "update"}
        )

    def test_mcp_tool_call_falls_back_with_full_args(self):  # noqa: ANN202  # tracked: #288
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "vibesys_issues",
                        "tool": "list_issues",
                        "arguments": {"cap": 1},
                        "result": "[]",
                    },
                }
            )
        )
        handler.on_tool_call.assert_called_once_with(
            "mcp_tool_call",
            {
                "server": "vibesys_issues",
                "tool": "list_issues",
                "arguments": {"cap": 1},
                "result": "[]",
            },
        )

    def test_error_item_surfaces_message(self):  # noqa: ANN202  # tracked: #288
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "error", "message": "rate limited"},
                }
            )
        )
        handler.on_tool_call.assert_called_once_with("error", {"message": "rate limited"})

    def test_command_execution_forwards_tool_call_and_result(self):  # noqa: ANN202  # tracked: #288
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "ls -la",
                        "aggregated_output": "file.txt\n",
                    },
                }
            )
        )
        handler.on_tool_call.assert_called_once_with("execute", {"command": "ls -la"})
        handler.on_tool_result.assert_called_once_with(
            tool="execute", stdout="file.txt\n", exit_code=None, duration=None
        )

    def test_turn_completed_captures_usage_and_normalizes_fields(self):  # noqa: ANN202  # tracked: #288
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 800,
                        "output_tokens": 150,
                    },
                }
            )
        )
        assert session.final_usage == {
            "input_tokens": 1200,
            "output_tokens": 150,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 0,
        }
        handler.on_usage.assert_called_once_with(session.final_usage)
        # Also emits a visible marker line so a tail on the log shows the
        # turn completing, not just a silent usage update.
        thinking_calls = [c.args[0] for c in handler.on_thinking.call_args_list]
        assert any("turn complete" in t for t in thinking_calls)

    def test_turn_completed_without_usage_leaves_none(self):  # noqa: ANN202  # tracked: #288
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(json.dumps({"type": "turn.completed"}))  # noqa: SLF001  # tracked: #288
        assert session.final_usage is None
        # Still emits a marker so the log shows the turn boundary.
        handler.on_thinking.assert_called_once_with("[codex turn complete]")

    def test_thread_started_emits_marker(self):  # noqa: ANN202  # tracked: #288
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(json.dumps({"type": "thread.started", "thread_id": "t-1"}))  # noqa: SLF001  # tracked: #288
        assert session.session_id == "t-1"
        handler.on_thinking.assert_called_once_with("[codex thread t-1 started]")

    def test_turn_started_emits_marker(self):  # noqa: ANN202  # tracked: #288
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(json.dumps({"type": "turn.started"}))  # noqa: SLF001  # tracked: #288
        handler.on_thinking.assert_called_once_with("[codex turn started]")

    def test_unknown_event_types_forward_raw_line(self):  # noqa: ANN202  # tracked: #288
        """item.started / item.updated / future events pass through as thinking
        text so nothing codex emits is silently swallowed."""
        handler = MagicMock()
        session = _session(event_handler=handler)
        raw = json.dumps(
            {"type": "item.updated", "item": {"type": "reasoning", "delta": "thinking..."}}
        )
        session._process_stdout(raw)  # noqa: SLF001  # tracked: #288
        handler.on_thinking.assert_called_once_with(raw)

    def test_non_json_line_forwarded_to_event_handler(self):  # noqa: ANN202  # tracked: #288
        """Codex prints banners/warnings as plain text; these must hit the log
        even when ``silent=True`` (the legacy loguru path is gated on silent)."""
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout("starting codex 1.2.3\n")  # noqa: SLF001  # tracked: #288
        handler.on_thinking.assert_called_once_with("starting codex 1.2.3")
        assert "starting codex 1.2.3" in session.stdout_lines[0]

    def test_non_json_line_is_still_recorded(self):  # noqa: ANN202  # tracked: #288
        session = _session()
        session._process_stdout("not json at all\n")  # noqa: SLF001  # tracked: #288
        assert "not json at all" in session.stdout_lines[0]

    def test_blank_line_is_ignored(self):  # noqa: ANN202  # tracked: #288
        session = _session()
        session._process_stdout("   \n")  # noqa: SLF001  # tracked: #288
        assert session.stdout_lines == []


# ---------------------------------------------------------------------------
# Tool-event pairing
# ---------------------------------------------------------------------------


def _agent_session(handler) -> CodexGenerationSession:  # noqa: ANN001  # tracked: #288
    """Build a session through the agent, so the pairing subclass is wired in."""
    agent = _agent()
    agent.binary_name = "codex"
    agent.env = {}
    agent.logger = MagicMock()
    agent.executor = None
    agent.event_handler = handler
    return agent._create_session(["codex", "exec", "--json", "-"], silent=True)  # noqa: SLF001  # tracked: #288


def _item_line(event_type: str, item: Mapping[str, object]) -> str:
    """Serialize one raw codex item lifecycle line."""
    return json.dumps({"type": event_type, "item": dict(item)})


def _tool_events(handler: MagicMock) -> list[str]:
    """Ordered names of the tool callbacks the handler received."""
    return [name for name, *_ in handler.method_calls if name.startswith("on_tool")]


class TestToolEventPairing:
    """Pairing of generic codex items into call/result pairs by item id.

    agentshim maps a generic item's ``item.started`` and ``item.completed``
    both to tool-use events, and the completion payload differs from the
    start (``result`` / ``error`` get populated), so the session must pair
    the two by the stable item id, not by argument equality.
    """

    def test_mcp_lifecycle_emits_one_call_and_one_result(self) -> None:
        """The reviewed defect: completion parameters differ from the start."""
        handler = MagicMock()
        session = _agent_session(handler)
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            _item_line(
                "item.started",
                {
                    "id": "item_5",
                    "type": "mcp_tool_call",
                    "server": "docs",
                    "tool": "search",
                    "arguments": {"query": "x"},
                    "result": None,
                    "error": None,
                    "status": "in_progress",
                },
            )
        )
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            _item_line(
                "item.completed",
                {
                    "id": "item_5",
                    "type": "mcp_tool_call",
                    "server": "docs",
                    "tool": "search",
                    "arguments": {"query": "x"},
                    "result": {"content": [{"type": "text", "text": "hits"}]},
                    "error": None,
                    "status": "completed",
                },
            )
        )
        handler.on_tool_call.assert_called_once_with(
            "mcp_tool_call",
            {
                "server": "docs",
                "tool": "search",
                "arguments": {"query": "x"},
                "result": None,
                "error": None,
            },
        )
        handler.on_tool_result.assert_called_once()
        result = handler.on_tool_result.call_args.kwargs
        assert result["tool"] == "mcp_tool_call"
        assert "hits" in result["stdout"]
        assert result["exit_code"] is None
        assert result["duration"] is not None
        assert result["duration"] >= 0
        assert _tool_events(handler) == ["on_tool_call", "on_tool_result"]

    def test_identical_parameters_with_distinct_ids_stay_two_items(self) -> None:
        """Byte-identical payloads must pair by id, not by argument equality."""
        handler = MagicMock()
        session = _agent_session(handler)
        for item_id in ("item_a", "item_b"):
            for event_type, status in (
                ("item.started", "in_progress"),
                ("item.completed", "completed"),
            ):
                session._process_stdout(  # noqa: SLF001  # tracked: #288
                    _item_line(
                        event_type,
                        {
                            "id": item_id,
                            "type": "file_change",
                            "status": status,
                            "path": "engine.py",
                            "kind": "update",
                        },
                    )
                )
        assert handler.on_tool_call.call_count == 2
        assert handler.on_tool_result.call_count == 2
        assert _tool_events(handler) == [
            "on_tool_call",
            "on_tool_result",
            "on_tool_call",
            "on_tool_result",
        ]
        # file_change has no output payload; results carry empty stdout.
        assert [c.kwargs["stdout"] for c in handler.on_tool_result.call_args_list] == ["", ""]

    def test_interleaved_items_pair_by_id(self) -> None:
        handler = MagicMock()
        session = _agent_session(handler)
        item_a = {"id": "a", "type": "file_change", "path": "a.py"}
        item_b = {"id": "b", "type": "web_search", "query": "docs"}
        lines = [
            ("item.started", {**item_a, "status": "in_progress"}),
            ("item.started", {**item_b, "status": "in_progress"}),
            ("item.completed", {**item_b, "status": "completed", "result": "found"}),
            ("item.completed", {**item_a, "status": "completed"}),
        ]
        for event_type, item in lines:
            session._process_stdout(_item_line(event_type, item))  # noqa: SLF001  # tracked: #288
        assert [c.args[0] for c in handler.on_tool_call.call_args_list] == [
            "file_change",
            "web_search",
        ]
        assert [c.kwargs["tool"] for c in handler.on_tool_result.call_args_list] == [
            "web_search",
            "file_change",
        ]

    def test_identical_overlapping_items_keep_both_starts(self) -> None:
        handler = MagicMock()
        session = _agent_session(handler)
        for event_type, item_id in (
            ("item.started", "a"),
            ("item.started", "b"),
            ("item.completed", "b"),
            ("item.completed", "a"),
        ):
            session._process_stdout(  # noqa: SLF001  # tracked: #288
                _item_line(event_type, {"id": item_id, "type": "file_change", "changes": []})
            )
        assert _tool_events(handler) == [
            "on_tool_call",
            "on_tool_call",
            "on_tool_result",
            "on_tool_result",
        ]

    def test_identical_completion_only_items_each_get_a_pair(self) -> None:
        handler = MagicMock()
        session = _agent_session(handler)
        for item_id in ("a", "b"):
            session._process_stdout(  # noqa: SLF001  # tracked: #288
                _item_line("item.completed", {"id": item_id, "type": "file_change", "changes": []})
            )
        assert _tool_events(handler) == [
            "on_tool_call",
            "on_tool_result",
            "on_tool_call",
            "on_tool_result",
        ]

    def test_completion_without_observed_start_synthesizes_the_call(self) -> None:
        handler = MagicMock()
        session = _agent_session(handler)
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            _item_line(
                "item.completed",
                {
                    "id": "w1",
                    "type": "web_search",
                    "status": "completed",
                    "query": "q",
                    "result": "r",
                },
            )
        )
        handler.on_tool_call.assert_called_once_with("web_search", {"query": "q", "result": "r"})
        handler.on_tool_result.assert_called_once()
        result = handler.on_tool_result.call_args.kwargs
        assert result["tool"] == "web_search"
        assert result["stdout"] == "r"
        assert result["duration"] is None
        assert _tool_events(handler) == ["on_tool_call", "on_tool_result"]

    def test_command_execution_keeps_base_pairing(self) -> None:
        """``execute`` items pair upstream; the id pairing must not interfere."""
        handler = MagicMock()
        session = _agent_session(handler)
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            _item_line(
                "item.started",
                {
                    "id": "c1",
                    "type": "command_execution",
                    "status": "in_progress",
                    "command": "ls -la",
                },
            )
        )
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            _item_line(
                "item.completed",
                {
                    "id": "c1",
                    "type": "command_execution",
                    "status": "completed",
                    "command": "ls -la",
                    "aggregated_output": "file.txt\n",
                    "exit_code": 0,
                },
            )
        )
        handler.on_tool_call.assert_called_once_with("execute", {"command": "ls -la"})
        handler.on_tool_result.assert_called_once()
        result = handler.on_tool_result.call_args.kwargs
        assert result["tool"] == "execute"
        assert result["stdout"] == "file.txt\n"
        assert result["exit_code"] == 0
        assert result["duration"] is not None

    def test_real_result_event_closes_started_item_without_duplicate_call(self) -> None:
        """A fixed adapter emitting a real ToolResultEvent must resolve cleanly."""
        handler = MagicMock()
        session = _agent_session(handler)
        session._process_stdout(  # noqa: SLF001  # tracked: #288
            _item_line(
                "item.started",
                {
                    "id": "m1",
                    "type": "mcp_tool_call",
                    "status": "in_progress",
                    "server": "docs",
                    "tool": "search",
                    "arguments": {"query": "x"},
                },
            )
        )
        session._handle_event(  # noqa: SLF001  # tracked: #288
            ToolResultEvent(
                tool_id="m1",
                output="done",
                tool_name="mcp_tool_call",
                parameters={"server": "docs"},
            )
        )
        handler.on_tool_call.assert_called_once()
        handler.on_tool_result.assert_called_once()
        result = handler.on_tool_result.call_args.kwargs
        assert result["tool"] == "mcp_tool_call"
        assert result["stdout"] == "done"
        assert result["duration"] is not None

    def test_failed_completion_surfaces_the_error(self) -> None:
        handler = MagicMock()
        session = _agent_session(handler)
        for event_type, extra in (
            ("item.started", {"result": None, "error": None, "status": "in_progress"}),
            ("item.completed", {"result": None, "error": {"message": "boom"}, "status": "failed"}),
        ):
            session._process_stdout(  # noqa: SLF001  # tracked: #288
                _item_line(
                    event_type,
                    {
                        "id": "m2",
                        "type": "mcp_tool_call",
                        "server": "docs",
                        "tool": "search",
                        "arguments": {},
                        **extra,
                    },
                )
            )
        handler.on_tool_call.assert_called_once()
        handler.on_tool_result.assert_called_once()
        assert "boom" in handler.on_tool_result.call_args.kwargs["stdout"]

    def test_item_updated_and_non_json_lines_emit_no_tool_events(self) -> None:
        handler = MagicMock()
        session = _agent_session(handler)
        item = {"id": "f1", "type": "file_change", "status": "in_progress", "path": "a.py"}
        session._process_stdout(_item_line("item.started", item))  # noqa: SLF001  # tracked: #288
        session._process_stdout(_item_line("item.updated", item))  # noqa: SLF001  # tracked: #288
        session._process_stdout("starting codex 1.2.3\n")  # noqa: SLF001  # tracked: #288
        assert handler.on_tool_call.call_count == 1
        handler.on_tool_result.assert_not_called()

    def test_session_without_handler_processes_a_full_lifecycle(self) -> None:
        session = _agent_session(None)
        for event_type, status in (
            ("item.started", "in_progress"),
            ("item.completed", "completed"),
        ):
            session._process_stdout(  # noqa: SLF001  # tracked: #288
                _item_line(
                    event_type,
                    {"id": "f2", "type": "file_change", "status": status, "path": "a.py"},
                )
            )
        assert session.tool_map == {}
        assert session.tool_start_times == {}
        assert session.tool_args == {}


class TestProcessStderr:
    def test_stderr_forwarded_to_event_handler(self):  # noqa: ANN202  # tracked: #288
        """Stderr must surface in the log regardless of ``silent``; the driver
        always passes ``silent=True`` and the base class's stderr path is
        gated on it."""
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stderr("panic: index out of bounds\n")  # noqa: SLF001  # tracked: #288
        handler.on_thinking.assert_called_once_with("[codex stderr] panic: index out of bounds")
        assert session.stderr_lines == ["panic: index out of bounds\n"]

    def test_stderr_empty_line_ignored(self):  # noqa: ANN202  # tracked: #288
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stderr("\n")  # noqa: SLF001  # tracked: #288
        handler.on_thinking.assert_not_called()


def test_resume_from_adopts_a_checkpoint_when_no_conversation_is_live() -> None:
    agent = _agent()
    agent.session_id = None

    assert agent.resume_from("thread-checkpoint") is True
    assert agent.session_id == "thread-checkpoint"
    assert agent._get_resume_command("prompt", "thread-checkpoint")[:4] == [  # noqa: SLF001
        "/usr/local/bin/codex",
        "exec",
        "resume",
        "thread-checkpoint",
    ]


def test_resume_from_refuses_to_replace_a_live_conversation() -> None:
    agent = _agent()
    agent.session_id = "thread-live"

    assert agent.resume_from("thread-checkpoint") is False
    assert agent.session_id == "thread-live"


def test_forget_session_starts_the_next_turn_fresh() -> None:
    agent = _agent()
    agent.session_id = "thread-live"

    agent.forget_session()
    agent.forget_session()  # idempotent

    assert agent.session_id is None


def test_a_provider_without_a_resume_flag_never_adopts_a_checkpoint() -> None:
    agent = GeminiCodingAgent.__new__(GeminiCodingAgent)
    agent.session_id = None

    assert agent.supports_session_resume is False
    assert agent.resume_from("thread-checkpoint") is False
    assert agent.session_id is None
