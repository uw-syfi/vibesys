from unittest.mock import MagicMock

import pytest

from libs.agent_cli.claude import ClaudeCodeCodingAgent, ClaudeGenerationSession


class _MockCommandExecutor:
    """CommandExecutor that avoids real binary lookups."""

    def find_binary(self, binary_name: str, env: dict[str, str]) -> str:
        return f"/usr/local/bin/{binary_name}"

    def check_binary(
        self,
        binary_path: str,
        env: dict[str, str],
        *,
        timeout: int,
    ) -> None:
        pass

    def run(self, request, sink):
        raise NotImplementedError("should not be called in these tests")


@pytest.fixture
def mock_binaries():
    """Provide a mock CommandExecutor that bypasses real binary discovery."""
    return _MockCommandExecutor()


@pytest.fixture
def agent(mock_binaries):
    """Create a ClaudeCodeCodingAgent with mocked binaries."""
    return ClaudeCodeCodingAgent(model="test-model", executor=mock_binaries)


class TestClaudeCodeCodingAgentInit:
    """Tests for ClaudeCodeCodingAgent initialization."""

    def test_binary_name_is_claude(self, agent):
        assert agent.binary_name == "claude"

    def test_binary_path_resolved(self, agent):
        assert agent.binary_path == "/usr/local/bin/claude"

    def test_claude_path_property(self, agent):
        """claude_path is a backward-compatible alias for binary_path."""
        assert agent.claude_path == agent.binary_path

    def test_model_stored(self, agent):
        assert agent.model == "test-model"

    def test_default_model_is_none(self, mock_binaries):
        agent = ClaudeCodeCodingAgent(executor=mock_binaries)
        assert agent.model is None

    def test_log_prefix(self, agent):
        assert agent._log_prefix == "[Claude]"

    def test_binary_not_found_raises_runtime_error(self):
        class _FailExecutor(_MockCommandExecutor):
            def find_binary(self, binary_name, env):
                raise RuntimeError(f"{binary_name} binary not found in PATH.")

        with pytest.raises(RuntimeError, match="claude binary not found"):
            ClaudeCodeCodingAgent(executor=_FailExecutor())


class TestClaudeCommandConstruction:
    """Tests for _get_command method."""

    def test_command_includes_required_flags(self, agent):
        cmd = agent._get_command("test prompt")
        assert agent.binary_path in cmd
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd

    def test_command_includes_model_when_set(self, agent):
        cmd = agent._get_command("test prompt")
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "test-model"

    def test_command_omits_model_when_none(self, mock_binaries):
        agent = ClaudeCodeCodingAgent(model=None, executor=mock_binaries)
        cmd = agent._get_command("test prompt")
        assert "--model" not in cmd

    def test_command_omits_prompt_from_argv(self, agent):
        """Prompt is piped via stdin, not embedded in argv.

        Keeping the prompt out of /proc/<pid>/cmdline matters because the
        implementer's Bash tool can run ``pkill -f`` patterns that would
        otherwise match the running ``claude`` process by command-line
        substring and SIGTERM it (see commit 794627c).
        """
        cmd = agent._get_command("deploy the app")
        assert "deploy the app" not in cmd


class TestClaudeGenerationSession:
    """Tests for ClaudeGenerationSession event processing."""

    def _make_session(self, event_handler=None):
        return ClaudeGenerationSession(
            binary_name="claude",
            env={},
            log_prefix="[Claude]",
            cmd=["claude", "-p"],
            logger=MagicMock(),
            silent=True,
            event_handler=event_handler,
        )

    def test_process_stdout_parses_text_event(self):
        session = self._make_session()
        line = '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
        session._process_stdout(line)
        assert "hello" in session.stdout_lines

    def test_process_stdout_parses_tool_use_event(self):
        session = self._make_session()
        line = (
            '{"type":"assistant","message":{"content":'
            '[{"type":"tool_use","name":"Bash","id":"t1","input":{"cmd":"ls"}}]}}\n'
        )
        session._process_stdout(line)
        assert "t1" in session.tool_map
        assert session.tool_map["t1"] == "Bash"

    def test_process_stdout_parses_tool_result_event(self):
        session = self._make_session()
        # Set up tool map first
        session.tool_map["t1"] = "Bash"
        session.tool_start_times["t1"] = 1000.0
        session.tool_args["t1"] = {"cmd": "ls"}

        line = (
            '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"file1.txt"}]}}\n'
        )
        session._process_stdout(line)
        # Tool result was processed without raising.

    def test_process_stdout_parses_result_event(self):
        session = self._make_session()
        line = '{"type":"result","result":"all done"}\n'
        session._process_stdout(line)
        assert session.final_result == "all done"

    def test_process_stdout_handles_non_json(self):
        session = self._make_session()
        session._process_stdout("some plain text\n")
        assert "some plain text" in session.stdout_lines

    def test_process_stdout_skips_empty_lines(self):
        session = self._make_session()
        session._process_stdout("")
        assert session.stdout_lines == []

    def test_event_handler_on_thinking_called(self):
        handler = MagicMock()
        session = self._make_session(event_handler=handler)
        line = '{"type":"assistant","message":{"content":[{"type":"text","text":"thinking..."}]}}\n'
        session._process_stdout(line)
        handler.on_thinking.assert_called_once_with("thinking...")

    def test_event_handler_on_tool_call_called(self):
        handler = MagicMock()
        session = self._make_session(event_handler=handler)
        line = (
            '{"type":"assistant","message":{"content":'
            '[{"type":"tool_use","name":"Read","id":"t2","input":{"path":"/tmp"}}]}}\n'
        )
        session._process_stdout(line)
        handler.on_tool_call.assert_called_once_with("Read", {"path": "/tmp"})

    def test_create_session_returns_claude_session(self, agent):
        session = agent._create_session(cmd=["claude", "-p"])
        assert isinstance(session, ClaudeGenerationSession)

    def test_assistant_usage_forwarded_to_event_handler(self):
        """Per-turn ``message.usage`` is forwarded via ``on_usage`` so the
        agent prefix can refresh on every assistant event — including
        tool-only turns with no text blocks."""
        handler = MagicMock()
        session = self._make_session(event_handler=handler)
        line = (
            '{"type":"assistant","message":{"content":'
            '[{"type":"text","text":"ok"}],'
            '"usage":{"input_tokens":14000,"output_tokens":50,'
            '"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
        )
        session._process_stdout(line)
        handler.on_usage.assert_called_once_with({
            "input_tokens": 14000,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        })

    def test_assistant_without_usage_does_not_call_on_usage(self):
        handler = MagicMock()
        session = self._make_session(event_handler=handler)
        line = (
            '{"type":"assistant","message":{"content":'
            '[{"type":"text","text":"ok"}]}}\n'
        )
        session._process_stdout(line)
        handler.on_usage.assert_not_called()

    def test_result_event_captures_final_usage_and_cost(self):
        session = self._make_session()
        line = (
            '{"type":"result","result":"done",'
            '"usage":{"input_tokens":7000,"output_tokens":120},'
            '"total_cost_usd":0.0456,"duration_ms":12345}\n'
        )
        session._process_stdout(line)
        assert session.final_result == "done"
        assert session.final_usage == {"input_tokens": 7000, "output_tokens": 120}
        assert session.total_cost_usd == 0.0456
        assert session.duration_ms == 12345

    def test_legacy_event_handler_without_on_usage_still_works(self):
        """Handlers that don't implement ``on_usage`` must not crash the session.

        The plan calls out that Protocol is structural, so we guard the
        call site with ``getattr`` for back-compat with older handlers.
        """
        # A handler object that only implements the original three hooks.
        class LegacyHandler:
            def __init__(self):
                self.text_calls = []

            def on_thinking(self, text):
                self.text_calls.append(text)

            def on_tool_call(self, tool, args=None):
                pass

            def on_tool_result(self, tool, stdout="", stderr="", exit_code=None, duration=None):
                pass

        handler = LegacyHandler()
        session = self._make_session(event_handler=handler)
        line = (
            '{"type":"assistant","message":{"content":'
            '[{"type":"text","text":"hi"}],'
            '"usage":{"input_tokens":500,"output_tokens":10}}}\n'
        )
        # Must not raise even though LegacyHandler has no on_usage method.
        session._process_stdout(line)
        assert handler.text_calls == ["hi"]
