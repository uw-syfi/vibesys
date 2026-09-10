# the module-level skip below has to precede the imports it guards
from typing import TYPE_CHECKING

import pytest

# Guarded so type checkers still see the module body: the names below are
# imported by other test modules, and an unconditional skip makes a checker
# infer every one of them as unreachable.
if not TYPE_CHECKING:
    pytest.skip("superseded by the 0.6 driver rewrite", allow_module_level=True)

import json
from unittest.mock import MagicMock

import pytest

from vibesys._agent_cli.claude import (
    ClaudeCodeCodingAgent,
    ClaudeGenerationSession,
    StructuredOutputClaudeSession,
)

_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
    "required": ["a", "b"],
    "additionalProperties": False,
}

# Shape produced by a ``dict[str, float]`` field (e.g.
# ``ImplementerResponse.metrics``): arbitrary keys with a constrained value type.
_MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "metrics": {"type": "object", "additionalProperties": {"type": "number"}},
    },
    "required": ["metrics"],
    "additionalProperties": False,
}


class _MockCommandExecutor:
    """CommandExecutor that avoids real binary lookups."""

    def find_binary(self, binary_name: str, env: dict[str, str]) -> str:  # noqa: ARG002  # tracked: #288
        return f"/usr/local/bin/{binary_name}"

    def check_binary(
        self,
        binary_path: str,
        env: dict[str, str],
        *,
        timeout: int,
    ) -> None:
        pass

    def run(self, request, sink):  # noqa: ANN001, ANN202  # tracked: #288
        raise NotImplementedError("should not be called in these tests")


@pytest.fixture
def mock_binaries():  # noqa: ANN202  # tracked: #288
    """Provide a mock CommandExecutor that bypasses real binary discovery."""
    return _MockCommandExecutor()


@pytest.fixture
def agent(mock_binaries):  # noqa: ANN001, ANN202  # tracked: #288
    """Create a ClaudeCodeCodingAgent with mocked binaries."""
    return ClaudeCodeCodingAgent(model="test-model", executor=mock_binaries)


class TestClaudeCodeCodingAgentInit:
    """Tests for ClaudeCodeCodingAgent initialization."""

    def test_binary_name_is_claude(self, agent):  # noqa: ANN001, ANN202  # tracked: #288
        assert agent.binary_name == "claude"

    def test_binary_path_resolved(self, agent):  # noqa: ANN001, ANN202  # tracked: #288
        assert agent.binary_path == "/usr/local/bin/claude"

    def test_claude_path_property(self, agent):  # noqa: ANN001, ANN202  # tracked: #288
        """claude_path is a backward-compatible alias for binary_path."""
        assert agent.claude_path == agent.binary_path

    def test_model_stored(self, agent):  # noqa: ANN001, ANN202  # tracked: #288
        assert agent.model == "test-model"

    def test_default_model_is_none(self, mock_binaries):  # noqa: ANN001, ANN202  # tracked: #288
        agent = ClaudeCodeCodingAgent(executor=mock_binaries)
        assert agent.model is None

    def test_log_prefix(self, agent):  # noqa: ANN001, ANN202  # tracked: #288
        assert agent._log_prefix == "[Claude]"  # noqa: SLF001  # tracked: #288

    def test_binary_not_found_raises_runtime_error(self):  # noqa: ANN202  # tracked: #288
        class _FailExecutor(_MockCommandExecutor):
            def find_binary(self, binary_name, env):  # noqa: ANN001, ANN202, ARG002  # tracked: #288
                raise RuntimeError(f"{binary_name} binary not found in PATH.")  # noqa: TRY003  # tracked: #288

        with pytest.raises(RuntimeError, match="claude binary not found"):
            ClaudeCodeCodingAgent(executor=_FailExecutor())


class TestClaudeCommandConstruction:
    """Tests for _get_command method."""

    def test_command_includes_required_flags(self, agent):  # noqa: ANN001, ANN202  # tracked: #288
        cmd = agent._get_command("test prompt")  # noqa: SLF001  # tracked: #288
        assert agent.binary_path in cmd
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd

    def test_command_includes_model_when_set(self, agent):  # noqa: ANN001, ANN202  # tracked: #288
        cmd = agent._get_command("test prompt")  # noqa: SLF001  # tracked: #288
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "test-model"

    def test_command_omits_model_when_none(self, mock_binaries):  # noqa: ANN001, ANN202  # tracked: #288
        agent = ClaudeCodeCodingAgent(model=None, executor=mock_binaries)
        cmd = agent._get_command("test prompt")  # noqa: SLF001  # tracked: #288
        assert "--model" not in cmd

    def test_command_omits_prompt_from_argv(self, agent):  # noqa: ANN001, ANN202  # tracked: #288
        """Prompt is piped via stdin, not embedded in argv.

        Keeping the prompt out of /proc/<pid>/cmdline matters because the
        implementer's Bash tool can run ``pkill -f`` patterns that would
        otherwise match the running ``claude`` process by command-line
        substring and SIGTERM it (see commit 794627c).
        """
        cmd = agent._get_command("deploy the app")  # noqa: SLF001  # tracked: #288
        assert "deploy the app" not in cmd


class TestClaudeNativeOutputSchema:
    """Tests for native ``--json-schema`` structured output wiring."""

    def test_advertises_native_output_schema(self):  # noqa: ANN202  # tracked: #288
        assert ClaudeCodeCodingAgent.supports_native_output_schema is True

    def test_wants_absolute_schema_path(self):  # noqa: ANN202  # tracked: #288
        # ``--json-schema`` is read inline at build time, so the schema file
        # path must be resolvable independent of the subprocess cwd.
        assert ClaudeCodeCodingAgent.native_output_schema_wants_absolute_path is True

    def test_allows_arbitrary_object_keys(self):  # noqa: ANN202  # tracked: #288
        # ``--json-schema`` accepts open-ended maps (verified against Claude
        # Code 2.1.221), unlike Codex's strict ``--output-schema`` subset.
        assert ClaudeCodeCodingAgent.native_output_schema_allows_arbitrary_keys is True

    def test_mapping_schema_reaches_the_session_and_yields_structured_output(  # noqa: ANN202  # tracked: #288
        self,
        agent,  # noqa: ANN001  # tracked: #288
        tmp_path,  # noqa: ANN001  # tracked: #288
    ):
        """A ``dict[str, float]`` field stays native: inlined, then captured back."""
        schema_file = tmp_path / "schema.json"
        schema_file.write_text(json.dumps(_MAPPING_SCHEMA), encoding="utf-8")
        agent.set_output_schema_path(str(schema_file))
        payload = {"metrics": {"latency_ms": 12.5, "throughput": 3.0}}
        agent.executor = _ReplayExecutor(
            [json.dumps({"type": "result", "result": "done", "structured_output": payload}) + "\n"]
        )

        cmd = agent._get_command("test prompt")  # noqa: SLF001  # tracked: #288
        session = agent._create_session(cmd=cmd)  # noqa: SLF001  # tracked: #288
        result = session.run("test prompt")

        # The mapping survives into argv rather than being flattened away.
        inline = json.loads(cmd[cmd.index("--json-schema") + 1])
        assert inline == _MAPPING_SCHEMA
        assert inline["properties"]["metrics"]["additionalProperties"] == {"type": "number"}
        assert json.loads(result) == payload

    def test_command_omits_json_schema_when_unset(self, agent):  # noqa: ANN001, ANN202  # tracked: #288
        cmd = agent._get_command("test prompt")  # noqa: SLF001  # tracked: #288
        assert "--json-schema" not in cmd
        # No schema => the streaming envelope is unchanged.
        assert "stream-json" in cmd

    def test_resume_command_omits_json_schema_when_unset(self, agent):  # noqa: ANN001, ANN202  # tracked: #288
        cmd = agent._get_resume_command("test prompt", "sess-1")  # noqa: SLF001  # tracked: #288
        assert "--json-schema" not in cmd

    def test_set_output_schema_path_reads_and_inlines(self, agent, tmp_path):  # noqa: ANN001, ANN202  # tracked: #288
        schema_file = tmp_path / "schema.json"
        schema_file.write_text(json.dumps(_SCHEMA, indent=2), encoding="utf-8")

        agent.set_output_schema_path(str(schema_file))

        # Stored compactly (no spaces) and semantically equal to the file.
        assert agent.output_schema_json is not None
        assert " " not in agent.output_schema_json
        assert json.loads(agent.output_schema_json) == _SCHEMA

    def test_command_includes_json_schema_when_set(self, agent, tmp_path):  # noqa: ANN001, ANN202  # tracked: #288
        schema_file = tmp_path / "schema.json"
        schema_file.write_text(json.dumps(_SCHEMA), encoding="utf-8")
        agent.set_output_schema_path(str(schema_file))

        cmd = agent._get_command("test prompt")  # noqa: SLF001  # tracked: #288

        assert "--json-schema" in cmd
        inline = cmd[cmd.index("--json-schema") + 1]
        assert json.loads(inline) == _SCHEMA
        # The streaming transport is retained alongside the schema.
        assert "stream-json" in cmd

    def test_resume_command_includes_json_schema_when_set(self, agent, tmp_path):  # noqa: ANN001, ANN202  # tracked: #288
        schema_file = tmp_path / "schema.json"
        schema_file.write_text(json.dumps(_SCHEMA), encoding="utf-8")
        agent.set_output_schema_path(str(schema_file))

        cmd = agent._get_resume_command("test prompt", "sess-1")  # noqa: SLF001  # tracked: #288

        assert "--json-schema" in cmd
        assert json.loads(cmd[cmd.index("--json-schema") + 1]) == _SCHEMA

    def test_set_output_schema_path_none_clears_previous(self, agent, tmp_path):  # noqa: ANN001, ANN202  # tracked: #288
        schema_file = tmp_path / "schema.json"
        schema_file.write_text(json.dumps(_SCHEMA), encoding="utf-8")
        agent.set_output_schema_path(str(schema_file))
        agent.set_output_schema_path(None)

        assert agent.output_schema_json is None
        assert "--json-schema" not in agent._get_command("test prompt")  # noqa: SLF001  # tracked: #288

    def test_missing_schema_file_raises_actionable_error(self, agent, tmp_path):  # noqa: ANN001, ANN202  # tracked: #288
        missing = tmp_path / "does-not-exist.json"
        with pytest.raises(RuntimeError, match="unreadable"):
            agent.set_output_schema_path(str(missing))

    def test_invalid_schema_json_raises_actionable_error(self, agent, tmp_path):  # noqa: ANN001, ANN202  # tracked: #288
        schema_file = tmp_path / "schema.json"
        schema_file.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(RuntimeError, match="not valid JSON"):
            agent.set_output_schema_path(str(schema_file))


class TestClaudeGenerationSession:
    """Tests for ClaudeGenerationSession event processing."""

    def _make_session(self, event_handler=None):  # noqa: ANN001, ANN202  # tracked: #288
        return ClaudeGenerationSession(
            binary_name="claude",
            env={},
            log_prefix="[Claude]",
            cmd=["claude", "-p"],
            logger=MagicMock(),
            silent=True,
            event_handler=event_handler,
        )

    def test_process_stdout_parses_text_event(self):  # noqa: ANN202  # tracked: #288
        session = self._make_session()
        line = '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
        session._process_stdout(line)  # noqa: SLF001  # tracked: #288
        assert "hello" in session.stdout_lines

    def test_process_stdout_parses_tool_use_event(self):  # noqa: ANN202  # tracked: #288
        session = self._make_session()
        line = (
            '{"type":"assistant","message":{"content":'
            '[{"type":"tool_use","name":"Bash","id":"t1","input":{"cmd":"ls"}}]}}\n'
        )
        session._process_stdout(line)  # noqa: SLF001  # tracked: #288
        assert "t1" in session.tool_map
        assert session.tool_map["t1"] == "Bash"

    def test_process_stdout_parses_tool_result_event(self):  # noqa: ANN202  # tracked: #288
        session = self._make_session()
        # Set up tool map first
        session.tool_map["t1"] = "Bash"
        session.tool_start_times["t1"] = 1000.0
        session.tool_args["t1"] = {"cmd": "ls"}

        line = '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"file1.txt"}]}}\n'
        session._process_stdout(line)  # noqa: SLF001  # tracked: #288
        # Tool result was processed without raising.

    def test_process_stdout_parses_result_event(self):  # noqa: ANN202  # tracked: #288
        session = self._make_session()
        line = '{"type":"result","result":"all done"}\n'
        session._process_stdout(line)  # noqa: SLF001  # tracked: #288
        assert session.final_result == "all done"

    def test_process_stdout_handles_non_json(self):  # noqa: ANN202  # tracked: #288
        session = self._make_session()
        session._process_stdout("some plain text\n")  # noqa: SLF001  # tracked: #288
        assert "some plain text" in session.stdout_lines

    def test_process_stdout_skips_empty_lines(self):  # noqa: ANN202  # tracked: #288
        session = self._make_session()
        session._process_stdout("")  # noqa: SLF001  # tracked: #288
        assert session.stdout_lines == []

    def test_event_handler_on_thinking_called(self):  # noqa: ANN202  # tracked: #288
        handler = MagicMock()
        session = self._make_session(event_handler=handler)
        line = '{"type":"assistant","message":{"content":[{"type":"text","text":"thinking..."}]}}\n'
        session._process_stdout(line)  # noqa: SLF001  # tracked: #288
        handler.on_thinking.assert_called_once_with("thinking...")

    def test_event_handler_on_tool_call_called(self):  # noqa: ANN202  # tracked: #288
        handler = MagicMock()
        session = self._make_session(event_handler=handler)
        line = (
            '{"type":"assistant","message":{"content":'
            '[{"type":"tool_use","name":"Read","id":"t2","input":{"path":"/tmp"}}]}}\n'
        )
        session._process_stdout(line)  # noqa: SLF001  # tracked: #288
        handler.on_tool_call.assert_called_once_with("Read", {"path": "/tmp"})  # noqa: S108  # tracked: #288

    def test_create_session_returns_claude_session(self, agent):  # noqa: ANN001, ANN202  # tracked: #288
        session = agent._create_session(cmd=["claude", "-p"])  # noqa: SLF001  # tracked: #288
        assert isinstance(session, ClaudeGenerationSession)
        # The structured-output-aware subclass is used for every turn.
        assert isinstance(session, StructuredOutputClaudeSession)


class _ReplayExecutor:
    """CommandExecutor that replays canned stdout lines through the sink."""

    def __init__(self, lines):  # noqa: ANN001, ANN204  # tracked: #288
        self._lines = list(lines)

    def run(self, request, sink):  # noqa: ANN001, ANN202, ARG002  # tracked: #288
        from agentshim.executor import CommandResult  # noqa: PLC0415  # tracked: #288

        for line in self._lines:
            sink.stdout(line)
        return CommandResult(returncode=0, stdout="".join(self._lines), stderr="")


class TestStructuredOutputClaudeSession:
    """Tests for capturing and preferring the ``structured_output`` field."""

    def _make_session(self, event_handler=None, executor=None):  # noqa: ANN001, ANN202  # tracked: #288
        return StructuredOutputClaudeSession(
            binary_name="claude",
            env={},
            log_prefix="[Claude]",
            cmd=["claude", "-p"],
            logger=MagicMock(),
            silent=True,
            event_handler=event_handler,
            executor=executor,
        )

    def test_captures_structured_output_from_result_event(self):  # noqa: ANN202  # tracked: #288
        session = self._make_session()
        line = (
            '{"type":"result","result":"{\\"a\\":3,\\"b\\":5}","structured_output":{"a":3,"b":5}}\n'
        )
        session._process_stdout(line)  # noqa: SLF001  # tracked: #288
        # agentshim still records the freeform ``result`` text ...
        assert session.final_result == '{"a":3,"b":5}'
        # ... and the schema-enforced payload is captured separately.
        assert session.structured_output == {"a": 3, "b": 5}

    def test_run_prefers_serialized_structured_output(self):  # noqa: ANN202  # tracked: #288
        # Even if ``result`` held prose, ``run`` returns the schema payload.
        line = (
            '{"type":"result","result":"here is your answer","structured_output":{"a":3,"b":5}}\n'
        )
        session = self._make_session(executor=_ReplayExecutor([line]))
        result = session.run("prompt")
        assert json.loads(result) == {"a": 3, "b": 5}

    def test_run_falls_back_to_result_without_structured_output(self):  # noqa: ANN202  # tracked: #288
        line = '{"type":"result","result":"plain text answer"}\n'
        session = self._make_session(executor=_ReplayExecutor([line]))
        assert session.run("prompt") == "plain text answer"

    def test_absent_structured_output_leaves_none(self):  # noqa: ANN202  # tracked: #288
        session = self._make_session()
        session._process_stdout('{"type":"result","result":"plain text answer"}\n')  # noqa: SLF001  # tracked: #288
        assert session.structured_output is None
        assert session.final_result == "plain text answer"

    def test_null_structured_output_is_ignored(self):  # noqa: ANN202  # tracked: #288
        session = self._make_session()
        session._process_stdout('{"type":"result","result":"done","structured_output":null}\n')  # noqa: SLF001  # tracked: #288
        assert session.structured_output is None

    def test_non_json_line_does_not_raise(self):  # noqa: ANN202  # tracked: #288
        session = self._make_session()
        session._process_stdout("starting claude...\n")  # noqa: SLF001  # tracked: #288
        assert session.structured_output is None
        assert "starting claude..." in session.stdout_lines

    def test_assistant_usage_forwarded_to_event_handler(self):  # noqa: ANN202  # tracked: #288
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
        session._process_stdout(line)  # noqa: SLF001  # tracked: #288
        handler.on_usage.assert_called_once_with(
            {
                "input_tokens": 14000,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
        )

    def test_assistant_without_usage_does_not_call_on_usage(self):  # noqa: ANN202  # tracked: #288
        handler = MagicMock()
        session = self._make_session(event_handler=handler)
        line = '{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}]}}\n'
        session._process_stdout(line)  # noqa: SLF001  # tracked: #288
        handler.on_usage.assert_not_called()

    def test_result_event_captures_final_usage_and_cost(self):  # noqa: ANN202  # tracked: #288
        session = self._make_session()
        line = (
            '{"type":"result","result":"done",'
            '"usage":{"input_tokens":7000,"output_tokens":120},'
            '"total_cost_usd":0.0456,"duration_ms":12345}\n'
        )
        session._process_stdout(line)  # noqa: SLF001  # tracked: #288
        assert session.final_result == "done"
        assert session.final_usage == {"input_tokens": 7000, "output_tokens": 120}
        assert session.total_cost_usd == 0.0456
        assert session.duration_ms == 12345

    def test_legacy_event_handler_without_on_usage_still_works(self):  # noqa: ANN202  # tracked: #288
        """Handlers that don't implement ``on_usage`` must not crash the session.

        The plan calls out that Protocol is structural, so we guard the
        call site with ``getattr`` for back-compat with older handlers.
        """

        # A handler object that only implements the original three hooks.
        class LegacyHandler:
            def __init__(self):  # noqa: ANN204  # tracked: #288
                self.text_calls = []

            def on_thinking(self, text):  # noqa: ANN001, ANN202  # tracked: #288
                self.text_calls.append(text)

            def on_tool_call(self, tool, args=None):  # noqa: ANN001, ANN202  # tracked: #288
                pass

            def on_tool_result(self, tool, stdout="", stderr="", exit_code=None, duration=None):  # noqa: ANN001, ANN202  # tracked: #288
                pass

        handler = LegacyHandler()
        session = self._make_session(event_handler=handler)
        line = (
            '{"type":"assistant","message":{"content":'
            '[{"type":"text","text":"hi"}],'
            '"usage":{"input_tokens":500,"output_tokens":10}}}\n'
        )
        # Must not raise even though LegacyHandler has no on_usage method.
        session._process_stdout(line)  # noqa: SLF001  # tracked: #288
        assert handler.text_calls == ["hi"]
