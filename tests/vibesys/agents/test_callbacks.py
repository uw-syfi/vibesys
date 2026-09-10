import io
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from vibesys.agents.callbacks import AgentLogger
from vibesys.agents.progress import RoundProgress
from vibesys.constants import DIM, GREEN, RED, RESET
from vibesys.render.sink import output_sink
from vibesys.run.events import AgentOutputChunkData, ToolCallData, ToolResultData

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _make_response(text="", tool_calls=None, content=None, usage_metadata=None):  # noqa: ANN001, ANN202  # tracked: #288
    """Build a mock LLM response matching langchain structure."""
    msg = MagicMock()
    msg.text = text
    msg.tool_calls = tool_calls or []
    msg.content = content if content is not None else text
    # Set explicitly so MagicMock doesn't auto-create a truthy attribute.
    msg.usage_metadata = usage_metadata
    gen = MagicMock()
    gen.message = msg
    resp = MagicMock()
    resp.generations = [[gen]]
    return resp


class TestOnLlmNewToken:
    def test_prints_token_and_sets_streaming(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        logger.on_llm_new_token("hello")
        assert logger._streaming is True  # noqa: SLF001  # tracked: #288
        assert "hello" in capsys.readouterr().out

    def test_empty_token_no_output(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        logger.on_llm_new_token("")
        assert logger._streaming is False  # noqa: SLF001  # tracked: #288
        assert capsys.readouterr().out == ""

    def test_none_token_no_output(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        # LangChain can stream a None token, which the declared type rejects.
        none_token: dict[str, Any] = {"token": None}
        logger.on_llm_new_token(**none_token)  # tracked: #297
        assert logger._streaming is False  # noqa: SLF001  # tracked: #288
        assert capsys.readouterr().out == ""


class TestOnLlmEnd:
    def test_streaming_prints_newline_and_resets(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        logger._streaming = True  # noqa: SLF001  # tracked: #288
        logger.on_llm_end(_make_response("some text"))
        out = capsys.readouterr().out
        assert out.startswith("\n")
        assert logger._streaming is False  # noqa: SLF001  # tracked: #288

    def test_streaming_does_not_reprint_text(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        logger._streaming = True  # noqa: SLF001  # tracked: #288
        logger.on_llm_end(_make_response("some text"))
        out = capsys.readouterr().out
        # Only the newline, no green-wrapped text
        assert f"{GREEN}some text{RESET}" not in out

    def test_not_streaming_prints_text(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        logger.on_llm_end(_make_response("hello world"))
        out = capsys.readouterr().out
        assert "hello world" in out

    def test_not_streaming_prints_tool_calls(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        tc = [{"name": "shell", "args": {"cmd": "ls"}}]
        logger = AgentLogger()
        logger.on_llm_end(_make_response(tool_calls=tc))
        out = capsys.readouterr().out
        assert "shell(" in out

    def test_thinking_blocks_printed(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        content = [
            {"type": "thinking", "thinking": "let me think..."},
            {"type": "text", "text": "answer"},
        ]
        logger = AgentLogger()
        logger.on_llm_end(_make_response(content=content))
        out = capsys.readouterr().out
        assert "let me think..." in out

    def test_thinking_blocks_marked_in_log(self):  # noqa: ANN201  # tracked: #288
        log = io.StringIO()
        content = [
            {"type": "thinking", "thinking": "let me think..."},
            {"type": "text", "text": "answer"},
        ]
        logger = AgentLogger(log_file=log)
        logger.on_llm_end(_make_response(content=content))
        log_text = log.getvalue()
        assert "[thinking]" in log_text
        assert "let me think..." in log_text


class TestOnThinkingChannel:
    """Driver plumbing must not be published as the agent's chain of thought."""

    @staticmethod
    def _channels(*texts: str) -> list[str]:
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger()
            for text in texts:
                logger.on_thinking(text)
        finally:
            unsubscribe()
        return [
            event.data.channel for event in seen if isinstance(event.data, AgentOutputChunkData)
        ]

    def test_lifecycle_markers_publish_as_diagnostics(self) -> None:
        assert (
            self._channels(
                "[codex thread 01a0 started]",
                "[codex turn started]",
                "[codex turn complete: in=10 cached=0 out=2]",
                "[codex stderr] warning: slow filesystem",
                "[codex error] stream disconnected",
                "[copilot error] rate limited",
            )
            == ["diagnostic"] * 6
        )

    def test_reasoning_still_publishes_as_analysis(self) -> None:
        assert (
            self._channels(
                "The ring buffer is the hot path.",
                "[note] a bracketed aside is not a driver marker",
                "[codex thread-safety] neither is a hyphenated word",
            )
            == ["analysis"] * 3
        )

    def test_marker_text_is_published_verbatim(self) -> None:
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            AgentLogger().on_thinking("[codex stderr] permission denied")
        finally:
            unsubscribe()

        chunk = next(event.data for event in seen if isinstance(event.data, AgentOutputChunkData))
        assert chunk.content == "[codex stderr] permission denied"


class TestOnDiagnostic:
    """The explicit hook a driver-marked event routes to."""

    @staticmethod
    def _chunks(text: str) -> list[AgentOutputChunkData]:
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            AgentLogger().on_diagnostic(text)
        finally:
            unsubscribe()
        return [event.data for event in seen if isinstance(event.data, AgentOutputChunkData)]

    def test_text_publishes_verbatim_on_the_diagnostic_channel(self) -> None:
        chunks = self._chunks("agentshim: restarting the provider process")

        assert [chunk.channel for chunk in chunks] == ["diagnostic"]
        # Nothing inspects the text: the caller already classified it, so a
        # diagnostic that carries no lifecycle marker still lands here.
        assert [chunk.content for chunk in chunks] == ["agentshim: restarting the provider process"]

    def test_empty_text_publishes_nothing(self) -> None:
        assert self._chunks("") == []


class TestOnToolStart:
    def test_is_noop(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        serialized = {"name": "shell"}
        logger.on_tool_start(serialized, "", inputs={"cmd": "ls -la"})
        out = capsys.readouterr().out
        assert out == ""


class TestToolCorrelation:
    def test_provider_call_ids_correlate_parallel_results(self):  # noqa: ANN201  # tracked: #288
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger()
            logger.on_llm_end(
                _make_response(
                    tool_calls=[
                        {"id": "call-a", "name": "Read", "args": {"path": "a"}},
                        {"id": "call-b", "name": "Read", "args": {"path": "b"}},
                    ]
                )
            )
            logger.on_tool_end(
                SimpleNamespace(name="Read", tool_call_id="call-b", content="result b")
            )
            logger.on_tool_end(
                SimpleNamespace(name="Read", tool_call_id="call-a", content="result a")
            )
        finally:
            unsubscribe()

        calls = [event.data for event in seen if isinstance(event.data, ToolCallData)]
        results = [event.data for event in seen if isinstance(event.data, ToolResultData)]
        assert [call.call_id for call in calls] == ["call-a", "call-b"]
        assert [result.call_id for result in results] == ["call-b", "call-a"]

    def test_cli_events_get_stable_fifo_call_ids(self):  # noqa: ANN201  # tracked: #288
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger()
            logger.on_tool_call("Read", {"path": "a"})
            logger.on_tool_call("Read", {"path": "b"})
            logger.on_tool_result("Read", stdout="result a")
            logger.on_tool_result("Read", stdout="result b")
        finally:
            unsubscribe()

        calls = [event.data for event in seen if isinstance(event.data, ToolCallData)]
        results = [event.data for event in seen if isinstance(event.data, ToolResultData)]
        assert [result.call_id for result in results] == [call.call_id for call in calls]


class TestOnToolEnd:
    def test_prints_result_with_name(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        output = MagicMock()
        output.content = "file1.py\nfile2.py"
        output.name = "shell"
        logger.on_tool_end(output)
        out = capsys.readouterr().out
        assert "file1.py" in out

    def test_truncates_long_output(self, capsys, headless_renderer):  # noqa: ANN001, ANN201  # tracked: #288
        headless_renderer.max_result_len = 10
        logger = AgentLogger()
        output = MagicMock()
        output.content = "a" * 20
        output.name = "shell"
        logger.on_tool_end(output)
        out = capsys.readouterr().out
        assert "..." in out

    def test_exact_limit_no_truncation(self, capsys, headless_renderer):  # noqa: ANN001, ANN201  # tracked: #288
        headless_renderer.max_result_len = 10
        logger = AgentLogger()
        output = MagicMock()
        output.content = "a" * 10
        output.name = "shell"
        logger.on_tool_end(output)
        out = capsys.readouterr().out
        assert "..." not in out

    def test_custom_max_result_len(self, capsys, headless_renderer):  # noqa: ANN001, ANN201  # tracked: #288
        headless_renderer.max_result_len = 5
        logger = AgentLogger()
        output = MagicMock()
        output.content = "abcdefghij"
        output.name = "test"
        logger.on_tool_end(output)
        out = capsys.readouterr().out
        assert "abcde..." in out

    def test_plain_string_fallback(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        logger.on_tool_end("raw string output")
        out = capsys.readouterr().out
        assert "raw string output" in out

    def test_normal_output_is_plain_text(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        output = MagicMock()
        output.content = "ok"
        output.name = "shell"
        output.status = "success"
        logger.on_tool_end(output)
        out = capsys.readouterr().out
        assert "ok" in out
        assert DIM not in out
        assert RED not in out

    def test_error_status_is_plain_text(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        output = MagicMock()
        output.content = "Error: file not found"
        output.name = "shell"
        output.status = "error"
        logger.on_tool_end(output)
        out = capsys.readouterr().out
        assert "Error: file not found" in out
        assert RED not in out
        assert DIM not in out

    def test_command_failed_exit_code_is_plain_text(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        output = MagicMock()
        output.content = (
            "[stderr] fatal: not a git repository\n\n"
            "Exit code: 128\n"
            "[Command failed with exit code 128]"
        )
        output.name = "execute"
        output.status = "success"
        logger.on_tool_end(output)
        out = capsys.readouterr().out
        assert "Exit code: 128" in out
        assert RED not in out
        assert DIM not in out

    def test_command_succeeded_exit_code_is_plain_text(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        output = MagicMock()
        output.content = "hello world\n[Command succeeded with exit code 0]"
        output.name = "execute"
        output.status = "success"
        logger.on_tool_end(output)
        out = capsys.readouterr().out
        assert "hello world" in out
        assert DIM not in out
        assert RED not in out


class TestOnToolError:
    def test_prints_plain_error(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        from uuid import uuid4  # noqa: PLC0415  # tracked: #288

        logger.on_tool_error(Exception("something broke"), run_id=uuid4())
        out = capsys.readouterr().out
        assert RED not in out
        assert "something broke" in out
        assert "Tool error" in out

    def test_does_not_use_dim(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger()
        from uuid import uuid4  # noqa: PLC0415  # tracked: #288

        logger.on_tool_error(Exception("fail"), run_id=uuid4())
        out = capsys.readouterr().out
        assert DIM not in out


class TestOnToolErrorResult:
    """A tool failure must close the typed tool_call emitted from ``on_llm_end``."""

    def test_error_emits_failure_result_and_clears_pending(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger()
            logger.on_llm_end(
                _make_response(tool_calls=[{"id": "call-1", "name": "shell", "args": {"cmd": "x"}}])
            )
            run_id = uuid4()
            logger.on_tool_start({"name": "shell"}, "", run_id=run_id, tool_call_id="call-1")
            logger.on_tool_error(RuntimeError("boom"), run_id=run_id, tool_call_id="call-1")
        finally:
            unsubscribe()

        results = [event.data for event in seen if isinstance(event.data, ToolResultData)]
        assert len(results) == 1
        result = results[0]
        assert result.tool == "shell"
        assert result.call_id == "call-1"
        assert result.is_error is True
        assert "RuntimeError" in result.content
        assert "boom" in result.content
        assert not logger._pending_tool_calls["shell"]  # noqa: SLF001  # tracked: #288
        # The free-text diagnostic (with traceback detail) is still published.
        assert "Tool error" in capsys.readouterr().out

    def test_error_does_not_misattribute_next_same_tool_result(self) -> None:
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger()
            logger.on_llm_end(
                _make_response(tool_calls=[{"id": "call-1", "name": "shell", "args": {"cmd": "a"}}])
            )
            first_run = uuid4()
            logger.on_tool_start({"name": "shell"}, "", run_id=first_run, tool_call_id="call-1")
            logger.on_tool_error(RuntimeError("boom"), run_id=first_run, tool_call_id="call-1")

            logger.on_llm_end(
                _make_response(tool_calls=[{"id": "call-2", "name": "shell", "args": {"cmd": "b"}}])
            )
            second_run = uuid4()
            logger.on_tool_start({"name": "shell"}, "", run_id=second_run, tool_call_id="call-2")
            # No tool_call_id on the output: correlation falls back to the
            # FIFO head, which must be call-2 because the failure consumed
            # call-1.
            logger.on_tool_end(SimpleNamespace(name="shell", content="ok"), run_id=second_run)
        finally:
            unsubscribe()

        results = [event.data for event in seen if isinstance(event.data, ToolResultData)]
        assert [(r.call_id, r.is_error) for r in results] == [("call-1", True), ("call-2", False)]

    def test_tool_call_id_fallback_resolves_name_without_tool_start(self) -> None:
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger()
            logger.on_llm_end(
                _make_response(tool_calls=[{"id": "call-1", "name": "shell", "args": {"cmd": "x"}}])
            )
            logger.on_tool_error(RuntimeError("boom"), run_id=uuid4(), tool_call_id="call-1")
        finally:
            unsubscribe()

        results = [event.data for event in seen if isinstance(event.data, ToolResultData)]
        assert len(results) == 1
        assert results[0].tool == "shell"
        assert results[0].call_id == "call-1"
        assert results[0].is_error is True
        assert not logger._pending_tool_calls["shell"]  # noqa: SLF001  # tracked: #288

    def test_streamed_turn_error_emits_no_orphan_result(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger()
            # A streamed turn: ``on_llm_end`` returns before emitting typed
            # tool calls, so the tool run starts with nothing queued.
            logger.on_llm_new_token("tok")
            logger.on_llm_end(
                _make_response(tool_calls=[{"id": "call-1", "name": "shell", "args": {"cmd": "x"}}])
            )
            run_id = uuid4()
            logger.on_tool_start({"name": "shell"}, "", run_id=run_id, tool_call_id="call-1")
            logger.on_tool_error(RuntimeError("boom"), run_id=run_id, tool_call_id="call-1")
        finally:
            unsubscribe()

        calls = [event.data for event in seen if isinstance(event.data, ToolCallData)]
        results = [event.data for event in seen if isinstance(event.data, ToolResultData)]
        assert calls == []
        assert results == []
        assert "Tool error" in capsys.readouterr().out

    def test_empty_message_error_content_has_no_trailing_colon(self) -> None:
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger()
            logger.on_llm_end(
                _make_response(tool_calls=[{"id": "call-1", "name": "shell", "args": {"cmd": "x"}}])
            )
            run_id = uuid4()
            logger.on_tool_start({"name": "shell"}, "", run_id=run_id, tool_call_id="call-1")
            logger.on_tool_error(KeyboardInterrupt(), run_id=run_id, tool_call_id="call-1")
        finally:
            unsubscribe()

        results = [event.data for event in seen if isinstance(event.data, ToolResultData)]
        assert len(results) == 1
        assert results[0].content == "KeyboardInterrupt"
        assert results[0].is_error is True

    def test_error_without_tool_context_emits_no_result(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger()
            logger.on_tool_error(RuntimeError("boom"), run_id=uuid4())
        finally:
            unsubscribe()

        results = [event.data for event in seen if isinstance(event.data, ToolResultData)]
        assert results == []
        assert "Tool error" in capsys.readouterr().out


class TestStateManagement:
    def test_streaming_starts_false(self):  # noqa: ANN201  # tracked: #288
        assert AgentLogger()._streaming is False  # noqa: SLF001  # tracked: #288

    def test_streaming_lifecycle(self, capsys):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        logger = AgentLogger()
        assert logger._streaming is False  # noqa: SLF001  # tracked: #288
        logger.on_llm_new_token("tok")
        assert logger._streaming is True  # noqa: SLF001  # tracked: #288
        logger.on_llm_end(_make_response())
        assert logger._streaming is False  # noqa: SLF001  # tracked: #288


class TestLogFile:
    """AgentLogger with log_file writes full output to log while truncating stdout."""

    def test_tool_result_full_in_log_truncated_on_stdout(self, capsys, headless_renderer):  # noqa: ANN001, ANN201  # tracked: #288
        headless_renderer.max_result_len = 10
        log = io.StringIO()
        logger = AgentLogger(log_file=log)
        output = MagicMock()
        output.content = "a" * 50
        output.name = "shell"
        output.status = "success"
        logger.on_tool_end(output)
        stdout = capsys.readouterr().out
        log_text = log.getvalue()
        # stdout is truncated
        assert "..." in stdout
        assert len("a" * 50) > len(stdout)
        # log has full output
        assert "a" * 50 in log_text
        assert "..." not in log_text

    def test_tool_call_full_args_in_log(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        log = io.StringIO()
        logger = AgentLogger(log_file=log)
        long_arg = "x" * 200
        tc = [{"name": "shell", "args": {"cmd": long_arg}}]
        logger.on_llm_end(_make_response(tool_calls=tc))
        stdout = capsys.readouterr().out
        log_text = log.getvalue()
        # stdout truncates args at 80 chars
        assert long_arg not in stdout
        assert "..." in stdout
        # log has full args
        assert long_arg in log_text

    def test_tokens_written_to_log(self, capsys):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        log = io.StringIO()
        logger = AgentLogger(log_file=log)
        logger.on_llm_new_token("hello")
        log_text = log.getvalue()
        assert "hello" in log_text

    def test_thinking_written_to_log(self, capsys):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        log = io.StringIO()
        logger = AgentLogger(log_file=log)
        content = [
            {"type": "thinking", "thinking": "deep thoughts"},
            {"type": "text", "text": "answer"},
        ]
        logger.on_llm_end(_make_response(content=content))
        log_text = log.getvalue()
        assert "deep thoughts" in log_text

    def test_no_log_file_works_normally(self, capsys, headless_renderer):  # noqa: ANN001, ANN201  # tracked: #288
        """AgentLogger without log_file still works as before."""
        headless_renderer.max_result_len = 10
        logger = AgentLogger()
        output = MagicMock()
        output.content = "a" * 20
        output.name = "shell"
        output.status = "success"
        logger.on_tool_end(output)
        stdout = capsys.readouterr().out
        assert "..." in stdout

    def test_log_text_flushes_log_file(self):  # noqa: ANN201  # tracked: #288
        """Streamed events must hit disk immediately so ``tail -f`` on the run
        log shows codex output as it arrives, not in buffered bursts."""
        log = MagicMock()
        logger = AgentLogger(log_file=log)
        logger.log_text("streamed chunk")
        assert log.flush.called

    def test_tool_result_flushes_log_file(self):  # noqa: ANN201  # tracked: #288
        log = MagicMock()
        logger = AgentLogger(log_file=log)
        output = MagicMock()
        output.content = "ok"
        output.name = "shell"
        output.status = "success"
        logger.on_tool_end(output)
        assert log.flush.called


class TestOnChatModelStart:
    """Tests for on_chat_model_start callback that logs LLM call context to log_file."""

    def _make_system_message(self, content="You are a helpful assistant."):  # noqa: ANN001, ANN202  # tracked: #288
        msg = MagicMock()
        msg.type = "system"
        msg.content = content
        return msg

    def _make_human_message(self, content="Hello"):  # noqa: ANN001, ANN202  # tracked: #288
        msg = MagicMock()
        msg.type = "human"
        msg.content = content
        return msg

    def _make_ai_message(self, content="Hi there"):  # noqa: ANN001, ANN202  # tracked: #288
        msg = MagicMock()
        msg.type = "ai"
        msg.content = content
        return msg

    def _make_tool_message(self, content="result"):  # noqa: ANN001, ANN202  # tracked: #288
        msg = MagicMock()
        msg.type = "tool"
        msg.content = content
        return msg

    def test_first_call_logs_model_info(self, capsys):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        log = io.StringIO()
        logger = AgentLogger(log_file=log)
        serialized = {
            "id": ["langchain", "chat_models", "anthropic", "ChatAnthropic"],
            "kwargs": {"model": "claude-sonnet-4-6"},
        }
        messages = [[self._make_system_message(), self._make_human_message()]]
        logger.on_chat_model_start(serialized, messages)
        log_text = log.getvalue()
        assert "claude-sonnet-4-6" in log_text

    def test_first_call_logs_system_prompt(self, capsys):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        log = io.StringIO()
        logger = AgentLogger(log_file=log)
        serialized = {
            "id": ["langchain", "chat_models", "anthropic", "ChatAnthropic"],
            "kwargs": {"model": "claude-sonnet-4-6"},
        }
        system_prompt = "You are an expert ML engineer."
        messages = [[self._make_system_message(system_prompt), self._make_human_message()]]
        logger.on_chat_model_start(serialized, messages)
        log_text = log.getvalue()
        assert system_prompt in log_text

    def test_every_call_logs_message_type_summary(self, capsys):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        log = io.StringIO()
        logger = AgentLogger(log_file=log)
        serialized = {"id": ["langchain", "chat_models"], "kwargs": {}}
        messages = [
            [
                self._make_system_message(),
                self._make_human_message(),
                self._make_ai_message(),
                self._make_tool_message(),
                self._make_tool_message(),
                self._make_human_message("follow up"),
            ]
        ]
        logger.on_chat_model_start(serialized, messages)
        log_text = log.getvalue()
        assert "1 system" in log_text
        assert "2 human" in log_text
        assert "1 ai" in log_text
        assert "2 tool" in log_text

    def test_every_call_logs_separator_with_call_number(self, capsys):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        log = io.StringIO()
        logger = AgentLogger(log_file=log)
        serialized = {"id": ["langchain"], "kwargs": {}}
        messages = [[self._make_human_message()]]
        logger.on_chat_model_start(serialized, messages)
        logger.on_chat_model_start(serialized, messages)
        log_text = log.getvalue()
        assert "LLM call #1" in log_text
        assert "LLM call #2" in log_text

    def test_logs_last_human_message(self, capsys):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        log = io.StringIO()
        logger = AgentLogger(log_file=log)
        serialized = {"id": ["langchain"], "kwargs": {}}
        messages = [
            [
                self._make_system_message(),
                self._make_human_message("first question"),
                self._make_ai_message("first answer"),
                self._make_human_message("second question"),
            ]
        ]
        logger.on_chat_model_start(serialized, messages)
        log_text = log.getvalue()
        assert "second question" in log_text

    def test_no_log_file_is_noop(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        """Without log_file, on_chat_model_start should not write to stdout."""
        logger = AgentLogger()
        serialized = {"id": ["langchain"], "kwargs": {"model": "test-model"}}
        messages = [[self._make_human_message()]]
        logger.on_chat_model_start(serialized, messages)
        stdout = capsys.readouterr().out
        assert "test-model" not in stdout
        assert "LLM call" not in stdout

    def test_system_prompt_only_logged_on_first_call(self, capsys):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        log = io.StringIO()
        logger = AgentLogger(log_file=log)
        serialized = {"id": ["langchain"], "kwargs": {}}
        system_prompt = "You are an expert ML engineer."
        messages = [[self._make_system_message(system_prompt), self._make_human_message()]]
        logger.on_chat_model_start(serialized, messages)
        first_log = log.getvalue()
        assert system_prompt in first_log

        # Second call should NOT repeat system prompt
        log.truncate(0)
        log.seek(0)
        logger.on_chat_model_start(serialized, messages)
        second_log = log.getvalue()
        assert system_prompt not in second_log


class TestDefaultContextWindowLookup:
    def test_claude_4_6_resolves_to_1m(self):  # noqa: ANN201  # tracked: #288
        from vibesys.agents.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("claude-opus-4-6") == 1_000_000
        assert _default_context_window_lookup("claude-sonnet-4-6") == 1_000_000

    def test_older_claude_falls_back_to_200k(self):  # noqa: ANN201  # tracked: #288
        # Regression guard: claude- fallback comes after the 4-6 entries
        from vibesys.agents.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("claude-haiku-4-5") == 200_000
        assert _default_context_window_lookup("claude-sonnet-4-5") == 200_000
        assert _default_context_window_lookup("claude-opus-4-1") == 200_000

    def test_gemini(self):  # noqa: ANN201  # tracked: #288
        from vibesys.agents.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("gemini-2.5-flash") == 1_048_576
        assert _default_context_window_lookup("gemini-3-pro") == 1_048_576

    def test_gemma(self):  # noqa: ANN201  # tracked: #288
        from vibesys.agents.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("gemma-2") == 8_192

    def test_gpt5_4_resolves_to_1m(self):  # noqa: ANN201  # tracked: #288
        # Regression guard: gpt-5.4 entry must come before gpt-5
        from vibesys.agents.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("gpt-5.4") == 1_050_000
        assert _default_context_window_lookup("gpt-5.4-pro") == 1_050_000

    def test_gpt5_family_falls_back_to_400k(self):  # noqa: ANN201  # tracked: #288
        from vibesys.agents.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("gpt-5") == 400_000
        assert _default_context_window_lookup("gpt-5-mini") == 400_000
        assert _default_context_window_lookup("gpt-5-nano") == 400_000
        assert _default_context_window_lookup("gpt-5.2") == 400_000

    def test_gpt4_and_o_series(self):  # noqa: ANN201  # tracked: #288
        from vibesys.agents.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("gpt-4o") == 128_000
        assert _default_context_window_lookup("gpt-4-turbo") == 128_000
        assert _default_context_window_lookup("o1") == 200_000
        assert _default_context_window_lookup("o3-mini") == 200_000
        assert _default_context_window_lookup("o4-mini") == 200_000

    def test_unknown_model_returns_none(self):  # noqa: ANN201  # tracked: #288
        from vibesys.agents.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("unknown-model-xyz") is None

    def test_none_model_name_returns_none(self):  # noqa: ANN201  # tracked: #288
        from vibesys.agents.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup(None) is None


class TestPrefixFormat:
    def test_no_label_no_prefix_on_streaming(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        # Existing behavior preserved: AgentLogger without agent_label produces no prefix
        logger = AgentLogger()
        logger.on_llm_new_token("hello")
        out = _strip_ansi(capsys.readouterr().out)
        assert "[" not in out
        assert "hello" in out

    def test_prefix_with_label_and_known_model(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(agent_label="Implementer", model_name="claude-sonnet-4-6")
        logger.on_llm_new_token("hi")
        out = _strip_ansi(capsys.readouterr().out)
        # Format: [Implementer | <float>s | 0/1.0M] hi
        assert re.search(r"\[Implementer \| \d+\.\ds \| 0/1\.0M\]", out), out

    def test_prefix_includes_progress(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(
            agent_label="Implementer",
            progress=RoundProgress(3, 24),
            model_name="claude-sonnet-4-6",
        )
        logger.on_llm_new_token("hi")
        out = _strip_ansi(capsys.readouterr().out)
        assert re.search(
            r"\[Round 3/24 \| Implementer \| \d+\.\ds \| 0/1\.0M\]",
            out,
        ), out

    def test_prefix_with_gpt5_4(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(agent_label="Judge", model_name="gpt-5.4")
        logger.on_llm_new_token("hi")
        out = _strip_ansi(capsys.readouterr().out)
        # 1_050_000 -> "1.0M" or "1.1M" depending on float rounding; accept either
        assert re.search(r"\[Judge \| \d+\.\ds \| 0/1\.\dM\]", out), out

    def test_prefix_omits_max_when_model_unknown(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(agent_label="X", model_name="unknown-future-model")
        logger.on_llm_new_token("y")
        out = _strip_ansi(capsys.readouterr().out)
        # No "/" between count and bracket close
        match = re.search(r"\[X \| \d+\.\ds \| ([^\]]+)\]", out)
        assert match, out
        assert match.group(1) == "0"

    def test_prefix_omits_max_when_no_model_name(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(agent_label="X")
        logger.on_llm_new_token("y")
        out = _strip_ansi(capsys.readouterr().out)
        match = re.search(r"\[X \| \d+\.\ds \| ([^\]]+)\]", out)
        assert match, out
        assert match.group(1) == "0"

    def test_prefix_updates_after_on_llm_end(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(agent_label="Implementer", model_name="claude-sonnet-4-6")
        logger.on_llm_end(
            _make_response(
                usage_metadata={
                    "input_tokens": 20_100,
                    "output_tokens": 100,
                    "total_tokens": 20_200,
                }
            )
        )
        capsys.readouterr()  # discard
        logger.on_llm_new_token("next")
        out = _strip_ansi(capsys.readouterr().out)
        assert "20k/1.0M" in out, out

    def test_tool_call_path_uses_dynamic_prefix(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(agent_label="Implementer", model_name="gpt-5.4")
        tc = [{"name": "shell", "args": {"cmd": "ls"}}]
        logger.on_llm_end(
            _make_response(
                tool_calls=tc,
                usage_metadata={"input_tokens": 5_000, "output_tokens": 50, "total_tokens": 5_050},
            )
        )
        out = _strip_ansi(capsys.readouterr().out)
        # Tool-call line should carry the dynamic prefix with elapsed and tokens
        assert re.search(r"\[Implementer \| \d+\.\ds \| 5k/1\.\dM\] → shell\(", out), out

    def test_lookup_injection_overrides_default(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        # The user explicitly asked for an abstraction so the lookup can be stubbed.
        logger = AgentLogger(
            agent_label="Test",
            model_name="anything",
            context_window_lookup=lambda _: 999_999,
        )
        logger.on_llm_new_token("x")
        out = _strip_ansi(capsys.readouterr().out)
        # Used tokens still 0 (no on_llm_end yet); max is the injected 999_999 → "999k"
        assert "0/999k" in out, out

    def test_default_lookup_used_when_not_injected(self):  # noqa: ANN201  # tracked: #288
        from vibesys.agents.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        logger = AgentLogger(agent_label="Test", model_name="gpt-5.4")
        assert logger._context_window_lookup is _default_context_window_lookup  # noqa: SLF001  # tracked: #288
        assert logger._context_window == 1_050_000  # noqa: SLF001  # tracked: #288

    def test_elapsed_time_advances(self, capsys, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
        # Fake time.monotonic so we can verify the elapsed value reaches the prefix
        from vibesys.agents import callbacks  # noqa: PLC0415  # tracked: #288

        ticks = iter([1000.0, 1308.2])
        monkeypatch.setattr(callbacks.time, "monotonic", lambda: next(ticks))
        logger = AgentLogger(agent_label="Implementer", model_name="claude-sonnet-4-6")
        # First tick consumed in __init__; second tick consumed by _format_prefix
        logger.on_llm_new_token("hi")
        out = _strip_ansi(capsys.readouterr().out)
        assert "308.2s" in out, out


class TestUsageMetadataExtraction:
    def test_extracts_input_tokens(self):  # noqa: ANN201  # tracked: #288
        logger = AgentLogger(agent_label="X", model_name="claude-sonnet-4-6")
        assert logger._input_tokens == 0  # noqa: SLF001  # tracked: #288
        logger.on_llm_end(
            _make_response(
                usage_metadata={
                    "input_tokens": 12_345,
                    "output_tokens": 100,
                    "total_tokens": 12_445,
                }
            )
        )
        assert logger._input_tokens == 12_345  # noqa: SLF001  # tracked: #288

    def test_no_usage_metadata_keeps_zero(self):  # noqa: ANN201  # tracked: #288
        # vLLM / openai-compatible servers may omit usage entirely
        logger = AgentLogger(agent_label="X", model_name="gpt-5.4")
        logger.on_llm_end(_make_response(usage_metadata=None))
        assert logger._input_tokens == 0  # noqa: SLF001  # tracked: #288

    def test_streaming_path_still_extracts_usage(self):  # noqa: ANN201  # tracked: #288
        # on_llm_end during streaming must update tokens before its early-return
        logger = AgentLogger(agent_label="X", model_name="claude-sonnet-4-6")
        logger._streaming = True  # noqa: SLF001  # tracked: #288
        logger.on_llm_end(
            _make_response(
                usage_metadata={"input_tokens": 5_000, "output_tokens": 100, "total_tokens": 5_100}
            )
        )
        assert logger._input_tokens == 5_000  # noqa: SLF001  # tracked: #288

    def test_zero_input_tokens_does_not_overwrite(self):  # noqa: ANN201  # tracked: #288
        # Defensive: a usage block reporting 0 input tokens should not clobber a real prior value
        logger = AgentLogger(agent_label="X", model_name="claude-sonnet-4-6")
        logger._input_tokens = 1234  # noqa: SLF001  # tracked: #288
        logger.on_llm_end(
            _make_response(
                usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            )
        )
        assert logger._input_tokens == 1234  # noqa: SLF001  # tracked: #288


class TestUpdateUsagePublicHook:
    """Tests for the CLI-backend ``update_usage`` hook.

    ``AgentLogger.on_usage`` routes per-turn usage dicts from ``vibesys._agent_cli``
    into this method so the agent prefix stays in sync with the underlying CLI
    tool's token counts — mirroring how the deepagents path updates
    ``_input_tokens`` from ``on_llm_end``.
    """

    def test_update_usage_sets_input_tokens(self):  # noqa: ANN201  # tracked: #288
        logger = AgentLogger(agent_label="Implementer", model_name="claude-sonnet-4-6")
        assert logger._input_tokens == 0  # noqa: SLF001  # tracked: #288
        logger.update_usage({"input_tokens": 12_345, "output_tokens": 42})
        assert logger._input_tokens == 12_345  # noqa: SLF001  # tracked: #288
        assert logger._latest_usage == {"input_tokens": 12_345, "output_tokens": 42}  # noqa: SLF001  # tracked: #288

    def test_update_usage_overwrites_previous_value(self):  # noqa: ANN201  # tracked: #288
        """Semantics mirror ``on_llm_end``: latest turn overwrites — no accumulation."""
        logger = AgentLogger(agent_label="X", model_name="claude-sonnet-4-6")
        logger.update_usage({"input_tokens": 1_000})
        logger.update_usage({"input_tokens": 5_000})
        assert logger._input_tokens == 5_000  # noqa: SLF001  # tracked: #288

    def test_update_usage_empty_dict_is_noop(self):  # noqa: ANN201  # tracked: #288
        logger = AgentLogger(agent_label="X", model_name="claude-sonnet-4-6")
        logger._input_tokens = 500  # noqa: SLF001  # tracked: #288
        logger.update_usage({})
        assert logger._input_tokens == 500  # noqa: SLF001  # tracked: #288

    def test_update_usage_none_is_noop(self):  # noqa: ANN201  # tracked: #288
        logger = AgentLogger(agent_label="X", model_name="claude-sonnet-4-6")
        logger._input_tokens = 500  # noqa: SLF001  # tracked: #288
        logger.update_usage(None)
        assert logger._input_tokens == 500  # noqa: SLF001  # tracked: #288

    def test_update_usage_zero_input_tokens_does_not_overwrite(self):  # noqa: ANN201  # tracked: #288
        """A zero-count usage dict should not clobber a real prior value."""
        logger = AgentLogger(agent_label="X", model_name="claude-sonnet-4-6")
        logger._input_tokens = 2_500  # noqa: SLF001  # tracked: #288
        logger.update_usage({"input_tokens": 0, "output_tokens": 50})
        assert logger._input_tokens == 2_500  # noqa: SLF001  # tracked: #288

    def test_update_usage_reflected_in_prefix(self, capsys):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        """The formatted prefix includes the compact token count after an update."""
        logger = AgentLogger(agent_label="Implementer", model_name="claude-sonnet-4-6")
        logger.update_usage({"input_tokens": 12_345})
        prefix = _strip_ansi(logger._format_prefix())  # noqa: SLF001  # tracked: #288
        assert "12k/1.0M" in prefix

    def test_update_usage_drives_tool_call_prefix(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(agent_label="Implementer", model_name="claude-sonnet-4-6")
        logger.update_usage({"input_tokens": 14_000})
        logger.log_tool_call("Bash", {"command": "ls"})
        out = _strip_ansi(capsys.readouterr().out)
        assert re.search(r"\[Implementer \| \d+\.\ds \| 14k/1\.0M\] → Bash\(", out), out
