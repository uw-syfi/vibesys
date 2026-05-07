"""Unit tests for the Codex provider's command construction and JSONL parser.

These tests exercise :mod:`libs.agent_cli.codex` without spawning the real
``codex`` binary. They cover the three correctness fixes:

1. ``_get_command`` / ``_get_resume_command`` include ``--skip-git-repo-check``
   so codex doesn't refuse to run outside a git repo.
2. ``_get_resume_command`` passes ``-`` as the prompt positional so the stdin
   write in :class:`CLIGenerationSession.run` is actually consumed.
3. ``CodexGenerationSession`` captures cumulative token usage from
   ``turn.completed`` events and forwards a ``reasoning`` item's text through
   the event handler.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from libs.agent_cli.codex import CodexCodingAgent, CodexGenerationSession


def _agent() -> CodexCodingAgent:
    """Build a CodexCodingAgent without running binary detection."""
    agent = CodexCodingAgent.__new__(CodexCodingAgent)
    agent.binary_path = "/usr/local/bin/codex"
    agent.model = None
    agent.base_config_args = []
    agent.extra_config_args = []
    return agent


def _session(event_handler=None) -> CodexGenerationSession:
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
    def test_initial_command_includes_skip_git_repo_check(self):
        cmd = _agent()._get_command("hello")
        assert "--skip-git-repo-check" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--json" in cmd
        # The subcommand must be ``exec`` (not ``exec resume``).
        assert cmd[1] == "exec"
        assert "resume" not in cmd

    def test_initial_command_includes_model_when_set(self):
        agent = _agent()
        agent.model = "gpt-5"
        cmd = agent._get_command("hello")
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gpt-5"

    def test_initial_command_appends_extra_config_args(self):
        agent = _agent()
        agent.extra_config_args = ["--config", "foo=1"]
        cmd = agent._get_command("hello")
        assert cmd[-2:] == ["--config", "foo=1"]


class TestGetResumeCommand:
    def test_resume_passes_dash_positional(self):
        """Without ``-``, codex exec resume silently ignores stdin."""
        cmd = _agent()._get_resume_command("prompt", "sess-123")
        # The positional args come right after the subcommand path:
        #   codex exec resume <session_id> <prompt>
        assert cmd[:5] == [
            "/usr/local/bin/codex",
            "exec",
            "resume",
            "sess-123",
            "-",
        ]

    def test_resume_command_includes_skip_git_repo_check(self):
        cmd = _agent()._get_resume_command("prompt", "sess-123")
        assert "--skip-git-repo-check" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--json" in cmd

    def test_resume_command_passes_model_and_extra_config(self):
        agent = _agent()
        agent.model = "gpt-5"
        agent.extra_config_args = ["--config", "mcp_servers.x.command=\"python\""]
        cmd = agent._get_resume_command("prompt", "sess-123")
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gpt-5"
        assert cmd[-2:] == ["--config", 'mcp_servers.x.command="python"']


# ---------------------------------------------------------------------------
# Stream parser
# ---------------------------------------------------------------------------


class TestProcessStdout:
    def test_thread_started_captures_thread_id(self):
        session = _session()
        session._process_stdout(json.dumps({"type": "thread.started", "thread_id": "t-1"}))
        assert session.session_id == "t-1"

    def test_agent_message_captures_last_text(self):
        session = _session()
        session._process_stdout(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "final answer"},
                }
            )
        )
        assert session.final_result == "final answer"

    def test_agent_message_streams_through_on_thinking(self):
        """Assistant text should land in the log as soon as it arrives."""
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "final answer"},
                }
            )
        )
        handler.on_thinking.assert_called_once_with("final answer")

    def test_multiple_agent_messages_keep_last(self):
        session = _session()
        for text in ["first", "second", "third"]:
            session._process_stdout(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": text},
                    }
                )
            )
        assert session.final_result == "third"

    def test_reasoning_forwards_to_on_thinking(self):
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "reasoning", "text": "I should grep for X"},
                }
            )
        )
        handler.on_thinking.assert_called_once_with("I should grep for X")

    def test_unknown_item_types_fall_back_to_tool_call(self):
        """file_change / mcp_tool_call / todo_list / web_search / error …
        all surface through ``on_tool_call`` so their payloads land in the log."""
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(
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

    def test_mcp_tool_call_falls_back_with_full_args(self):
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "vibeserve_issues",
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
                "server": "vibeserve_issues",
                "tool": "list_issues",
                "arguments": {"cap": 1},
                "result": "[]",
            },
        )

    def test_error_item_surfaces_message(self):
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "error", "message": "rate limited"},
                }
            )
        )
        handler.on_tool_call.assert_called_once_with(
            "error", {"message": "rate limited"}
        )

    def test_command_execution_forwards_tool_call_and_result(self):
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(
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

    def test_turn_completed_captures_usage_and_normalizes_fields(self):
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(
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

    def test_turn_completed_without_usage_leaves_none(self):
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(json.dumps({"type": "turn.completed"}))
        assert session.final_usage is None
        # Still emits a marker so the log shows the turn boundary.
        handler.on_thinking.assert_called_once_with("[codex turn complete]")

    def test_thread_started_emits_marker(self):
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(
            json.dumps({"type": "thread.started", "thread_id": "t-1"})
        )
        assert session.session_id == "t-1"
        handler.on_thinking.assert_called_once_with("[codex thread t-1 started]")

    def test_turn_started_emits_marker(self):
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout(json.dumps({"type": "turn.started"}))
        handler.on_thinking.assert_called_once_with("[codex turn started]")

    def test_unknown_event_types_forward_raw_line(self):
        """item.started / item.updated / future events pass through as thinking
        text so nothing codex emits is silently swallowed."""
        handler = MagicMock()
        session = _session(event_handler=handler)
        raw = json.dumps({"type": "item.updated", "item": {"type": "reasoning", "delta": "thinking..."}})
        session._process_stdout(raw)
        handler.on_thinking.assert_called_once_with(raw)

    def test_non_json_line_forwarded_to_event_handler(self):
        """Codex prints banners/warnings as plain text; these must hit the log
        even when ``silent=True`` (the legacy loguru path is gated on silent)."""
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stdout("starting codex 1.2.3\n")
        handler.on_thinking.assert_called_once_with("starting codex 1.2.3")
        assert "starting codex 1.2.3" in session.stdout_lines[0]

    def test_non_json_line_is_still_recorded(self):
        session = _session()
        session._process_stdout("not json at all\n")
        assert "not json at all" in session.stdout_lines[0]

    def test_blank_line_is_ignored(self):
        session = _session()
        session._process_stdout("   \n")
        assert session.stdout_lines == []


class TestProcessStderr:
    def test_stderr_forwarded_to_event_handler(self):
        """Stderr must surface in the log regardless of ``silent``; cli_runner
        always passes ``silent=True`` and the base class's stderr path is
        gated on it."""
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stderr("panic: index out of bounds\n")
        handler.on_thinking.assert_called_once_with(
            "[codex stderr] panic: index out of bounds"
        )
        assert session.stderr_lines == ["panic: index out of bounds\n"]

    def test_stderr_empty_line_ignored(self):
        handler = MagicMock()
        session = _session(event_handler=handler)
        session._process_stderr("\n")
        handler.on_thinking.assert_not_called()
