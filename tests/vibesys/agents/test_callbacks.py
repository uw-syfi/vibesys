import io
import re
from unittest.mock import MagicMock

from vibesys.constants import DIM, RED
from vibesys.events import AgentOutputChunkData, ToolCallData, ToolResultData
from vibesys.render.sink import output_sink
from vs_agent.api import AgentEvent, AgentEventKind, RoundProgress
from vs_agent.api.testing import AgentLogger
from vs_agent.client import _LoggerObserver

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


class TestOnThinkingChannel:
    """Agent reasoning is published as analysis; plumbing is marked by drivers."""

    @staticmethod
    def _chunks(*events: AgentEvent) -> list[AgentOutputChunkData]:
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            observer = _LoggerObserver(AgentLogger(event_sink=output_sink()))
            for event in events:
                observer.on_event(event)
        finally:
            unsubscribe()
        return [event.data for event in seen if isinstance(event.data, AgentOutputChunkData)]

    def test_a_driver_marked_event_publishes_as_a_diagnostic(self) -> None:
        """A driver that knows an event is plumbing says so in the payload."""
        chunks = self._chunks(
            AgentEvent(
                kind=AgentEventKind.THINKING,
                text="[stderr] warning: slow filesystem",
                payload={"channel": "diagnostic"},
            )
        )

        assert [chunk.channel for chunk in chunks] == ["diagnostic"]
        # The marker text is published verbatim; only the channel changes.
        assert [chunk.content for chunk in chunks] == ["[stderr] warning: slow filesystem"]

    def test_an_unmarked_event_publishes_as_analysis(self) -> None:
        """Nothing inspects the text: a bracketed aside is still reasoning."""
        chunks = self._chunks(
            AgentEvent(kind=AgentEventKind.THINKING, text="The ring buffer is the hot path."),
            AgentEvent(kind=AgentEventKind.THINKING, text="[note] a bracketed aside"),
        )

        assert [chunk.channel for chunk in chunks] == ["analysis"] * 2

    def test_empty_text_publishes_nothing_directly(self) -> None:
        """``on_thinking`` and ``on_diagnostic`` share a no-op-on-empty helper."""
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            AgentLogger(event_sink=output_sink()).on_thinking("")
        finally:
            unsubscribe()

        assert [event.data for event in seen if isinstance(event.data, AgentOutputChunkData)] == []

    def test_writes_to_log_file(self) -> None:
        log = io.StringIO()
        AgentLogger(log_file=log, event_sink=output_sink()).on_thinking(
            "The ring buffer is the hot path."
        )

        assert "The ring buffer is the hot path." in log.getvalue()


class TestOnDiagnostic:
    """The explicit hook a driver-marked event routes to."""

    @staticmethod
    def _chunks(text: str) -> list[AgentOutputChunkData]:
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            AgentLogger(event_sink=output_sink()).on_diagnostic(text)
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

    def test_writes_to_log_file(self) -> None:
        log = io.StringIO()
        AgentLogger(log_file=log, event_sink=output_sink()).on_diagnostic(
            "agentshim: restarting the provider process"
        )

        assert "agentshim: restarting the provider process" in log.getvalue()


class TestToolCorrelation:
    def test_cli_events_get_stable_fifo_call_ids(self):  # noqa: ANN201  # tracked: #288
        seen = []
        unsubscribe = output_sink().subscribe(seen.append)
        try:
            logger = AgentLogger(event_sink=output_sink())
            logger.on_tool_call("Read", {"path": "a"})
            logger.on_tool_call("Read", {"path": "b"})
            logger.on_tool_result("Read", stdout="result a")
            logger.on_tool_result("Read", stdout="result b")
        finally:
            unsubscribe()

        calls = [event.data for event in seen if isinstance(event.data, ToolCallData)]
        results = [event.data for event in seen if isinstance(event.data, ToolResultData)]
        assert [result.call_id for result in results] == [call.call_id for call in calls]


class TestToolResultOutput:
    def test_prints_result_with_name(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(event_sink=output_sink())
        logger.log_tool_result("shell", "file1.py\nfile2.py")
        out = capsys.readouterr().out
        assert "file1.py" in out

    def test_truncates_long_output(self, capsys, headless_renderer):  # noqa: ANN001, ANN201  # tracked: #288
        headless_renderer.max_result_len = 10
        logger = AgentLogger(event_sink=output_sink())
        logger.log_tool_result("shell", "a" * 20)
        out = capsys.readouterr().out
        assert "..." in out

    def test_exact_limit_no_truncation(self, capsys, headless_renderer):  # noqa: ANN001, ANN201  # tracked: #288
        headless_renderer.max_result_len = 10
        logger = AgentLogger(event_sink=output_sink())
        logger.log_tool_result("shell", "a" * 10)
        out = capsys.readouterr().out
        assert "..." not in out

    def test_custom_max_result_len(self, capsys, headless_renderer):  # noqa: ANN001, ANN201  # tracked: #288
        headless_renderer.max_result_len = 5
        logger = AgentLogger(event_sink=output_sink())
        logger.log_tool_result("test", "abcdefghij")
        out = capsys.readouterr().out
        assert "abcde..." in out

    def test_normal_output_is_plain_text(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(event_sink=output_sink())
        logger.log_tool_result("shell", "ok")
        out = capsys.readouterr().out
        assert "ok" in out
        assert DIM not in out
        assert RED not in out

    def test_error_status_is_plain_text(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(event_sink=output_sink())
        logger.log_tool_result("shell", "Error: file not found")
        out = capsys.readouterr().out
        assert "Error: file not found" in out
        assert RED not in out
        assert DIM not in out

    def test_command_failed_exit_code_is_plain_text(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(event_sink=output_sink())
        logger.log_tool_result(
            "execute",
            (
                "[stderr] fatal: not a git repository\n\n"
                "Exit code: 128\n"
                "[Command failed with exit code 128]"
            ),
        )
        out = capsys.readouterr().out
        assert "Exit code: 128" in out
        assert RED not in out
        assert DIM not in out

    def test_command_succeeded_exit_code_is_plain_text(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(event_sink=output_sink())
        logger.log_tool_result("execute", "hello world\n[Command succeeded with exit code 0]")
        out = capsys.readouterr().out
        assert "hello world" in out
        assert DIM not in out
        assert RED not in out


class TestLogFile:
    """AgentLogger with log_file writes full output to log while truncating stdout."""

    def test_tool_result_full_in_log_truncated_on_stdout(self, capsys, headless_renderer):  # noqa: ANN001, ANN201  # tracked: #288
        headless_renderer.max_result_len = 10
        log = io.StringIO()
        logger = AgentLogger(log_file=log, event_sink=output_sink())
        logger.log_tool_result("shell", "a" * 50)
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
        logger = AgentLogger(log_file=log, event_sink=output_sink())
        long_arg = "x" * 200
        logger.log_tool_call("shell", {"cmd": long_arg})
        stdout = capsys.readouterr().out
        log_text = log.getvalue()
        # stdout truncates args at 80 chars
        assert long_arg not in stdout
        assert "..." in stdout
        # log has full args
        assert long_arg in log_text

    def test_text_written_to_log(self, capsys):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        log = io.StringIO()
        logger = AgentLogger(log_file=log, event_sink=output_sink())
        logger.log_text("hello")
        log_text = log.getvalue()
        assert "hello" in log_text

    def test_thinking_written_to_log(self):  # noqa: ANN201  # tracked: #288
        log = io.StringIO()
        logger = AgentLogger(log_file=log, event_sink=output_sink())
        logger.on_thinking("deep thoughts")
        assert "deep thoughts" in log.getvalue()

    def test_no_log_file_works_normally(self, capsys, headless_renderer):  # noqa: ANN001, ANN201  # tracked: #288
        """AgentLogger without log_file still works as before."""
        headless_renderer.max_result_len = 10
        logger = AgentLogger(event_sink=output_sink())
        logger.log_tool_result("shell", "a" * 20)
        stdout = capsys.readouterr().out
        assert "..." in stdout

    def test_log_text_flushes_log_file(self):  # noqa: ANN201  # tracked: #288
        """Streamed events must hit disk immediately so ``tail -f`` on the run
        log shows codex output as it arrives, not in buffered bursts."""
        log = MagicMock()
        logger = AgentLogger(log_file=log, event_sink=output_sink())
        logger.log_text("streamed chunk")
        assert log.flush.called

    def test_tool_result_flushes_log_file(self):  # noqa: ANN201  # tracked: #288
        log = MagicMock()
        logger = AgentLogger(log_file=log, event_sink=output_sink())
        logger.log_tool_result("shell", "ok")
        assert log.flush.called


class TestDefaultContextWindowLookup:
    def test_claude_4_6_resolves_to_1m(self):  # noqa: ANN201  # tracked: #288
        from vs_agent.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("claude-opus-4-6") == 1_000_000
        assert _default_context_window_lookup("claude-sonnet-4-6") == 1_000_000

    def test_older_claude_falls_back_to_200k(self):  # noqa: ANN201  # tracked: #288
        # Regression guard: claude- fallback comes after the 4-6 entries
        from vs_agent.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("claude-haiku-4-5") == 200_000
        assert _default_context_window_lookup("claude-sonnet-4-5") == 200_000
        assert _default_context_window_lookup("claude-opus-4-1") == 200_000

    def test_gemini(self):  # noqa: ANN201  # tracked: #288
        from vs_agent.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("gemini-2.5-flash") == 1_048_576
        assert _default_context_window_lookup("gemini-3-pro") == 1_048_576

    def test_gemma(self):  # noqa: ANN201  # tracked: #288
        from vs_agent.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("gemma-2") == 8_192

    def test_gpt5_4_resolves_to_1m(self):  # noqa: ANN201  # tracked: #288
        # Regression guard: gpt-5.4 entry must come before gpt-5
        from vs_agent.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("gpt-5.4") == 1_050_000
        assert _default_context_window_lookup("gpt-5.4-pro") == 1_050_000

    def test_gpt5_family_falls_back_to_400k(self):  # noqa: ANN201  # tracked: #288
        from vs_agent.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("gpt-5") == 400_000
        assert _default_context_window_lookup("gpt-5-mini") == 400_000
        assert _default_context_window_lookup("gpt-5-nano") == 400_000
        assert _default_context_window_lookup("gpt-5.2") == 400_000

    def test_gpt4_and_o_series(self):  # noqa: ANN201  # tracked: #288
        from vs_agent.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("gpt-4o") == 128_000
        assert _default_context_window_lookup("gpt-4-turbo") == 128_000
        assert _default_context_window_lookup("o1") == 200_000
        assert _default_context_window_lookup("o3-mini") == 200_000
        assert _default_context_window_lookup("o4-mini") == 200_000

    def test_unknown_model_returns_none(self):  # noqa: ANN201  # tracked: #288
        from vs_agent.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup("unknown-model-xyz") is None

    def test_none_model_name_returns_none(self):  # noqa: ANN201  # tracked: #288
        from vs_agent.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        assert _default_context_window_lookup(None) is None


class TestPrefixFormat:
    def test_no_label_no_prefix_on_streaming(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        # Existing behavior preserved: AgentLogger without agent_label produces no prefix
        logger = AgentLogger(event_sink=output_sink())
        logger.log_text("hello")
        out = _strip_ansi(capsys.readouterr().out)
        assert "[" not in out
        assert "hello" in out

    def test_prefix_with_label_and_known_model(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(
            agent_label="Implementer", model_name="claude-sonnet-4-6", event_sink=output_sink()
        )
        logger.log_text("hi")
        out = _strip_ansi(capsys.readouterr().out)
        # Format: [Implementer | <float>s | 0/1.0M] hi
        assert re.search(r"\[Implementer \| \d+\.\ds \| 0/1\.0M\]", out), out

    def test_prefix_includes_progress(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(
            agent_label="Implementer",
            progress=RoundProgress(3, 24),
            model_name="claude-sonnet-4-6",
            event_sink=output_sink(),
        )
        logger.log_text("hi")
        out = _strip_ansi(capsys.readouterr().out)
        assert re.search(
            r"\[Round 3/24 \| Implementer \| \d+\.\ds \| 0/1\.0M\]",
            out,
        ), out

    def test_prefix_with_gpt5_4(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(agent_label="Judge", model_name="gpt-5.4", event_sink=output_sink())
        logger.log_text("hi")
        out = _strip_ansi(capsys.readouterr().out)
        # 1_050_000 -> "1.0M" or "1.1M" depending on float rounding; accept either
        assert re.search(r"\[Judge \| \d+\.\ds \| 0/1\.\dM\]", out), out

    def test_prefix_omits_max_when_model_unknown(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(
            agent_label="X", model_name="unknown-future-model", event_sink=output_sink()
        )
        logger.log_text("y")
        out = _strip_ansi(capsys.readouterr().out)
        # No "/" between count and bracket close
        match = re.search(r"\[X \| \d+\.\ds \| ([^\]]+)\]", out)
        assert match, out
        assert match.group(1) == "0"

    def test_prefix_omits_max_when_no_model_name(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(agent_label="X", event_sink=output_sink())
        logger.log_text("y")
        out = _strip_ansi(capsys.readouterr().out)
        match = re.search(r"\[X \| \d+\.\ds \| ([^\]]+)\]", out)
        assert match, out
        assert match.group(1) == "0"

    def test_prefix_updates_after_update_usage(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(
            agent_label="Implementer", model_name="claude-sonnet-4-6", event_sink=output_sink()
        )
        logger.update_usage({"input_tokens": 20_100, "output_tokens": 100})
        capsys.readouterr()  # discard
        logger.log_text("next")
        out = _strip_ansi(capsys.readouterr().out)
        assert "20k/1.0M" in out, out

    def test_tool_call_path_uses_dynamic_prefix(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(
            agent_label="Implementer", model_name="gpt-5.4", event_sink=output_sink()
        )
        logger.update_usage({"input_tokens": 5_000, "output_tokens": 50})
        logger.log_tool_call("shell", {"cmd": "ls"})
        out = _strip_ansi(capsys.readouterr().out)
        # Tool-call line should carry the dynamic prefix with elapsed and tokens
        assert re.search(r"\[Implementer \| \d+\.\ds \| 5k/1\.\dM\] → shell\(", out), out

    def test_lookup_injection_overrides_default(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        # The user explicitly asked for an abstraction so the lookup can be stubbed.
        logger = AgentLogger(
            agent_label="Test",
            model_name="anything",
            context_window_lookup=lambda _: 999_999,
            event_sink=output_sink(),
        )
        logger.log_text("x")
        out = _strip_ansi(capsys.readouterr().out)
        # Used tokens still 0 (no usage update yet); max is the injected 999_999 → "999k"
        assert "0/999k" in out, out

    def test_default_lookup_used_when_not_injected(self):  # noqa: ANN201  # tracked: #288
        from vs_agent.callbacks import (  # noqa: PLC0415  # tracked: #288
            _default_context_window_lookup,
        )

        logger = AgentLogger(agent_label="Test", model_name="gpt-5.4", event_sink=output_sink())
        assert logger._context_window_lookup is _default_context_window_lookup  # noqa: SLF001  # tracked: #288
        assert logger._context_window == 1_050_000  # noqa: SLF001  # tracked: #288

    def test_elapsed_time_advances(self, capsys, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
        # Fake time.monotonic so we can verify the elapsed value reaches the prefix
        from vs_agent import callbacks  # noqa: PLC0415  # tracked: #288

        ticks = iter([1000.0, 1308.2])
        monkeypatch.setattr(callbacks.time, "monotonic", lambda: next(ticks))
        logger = AgentLogger(
            agent_label="Implementer", model_name="claude-sonnet-4-6", event_sink=output_sink()
        )
        # First tick consumed in __init__; second tick consumed by _format_prefix
        logger.log_text("hi")
        out = _strip_ansi(capsys.readouterr().out)
        assert "308.2s" in out, out


class TestUpdateUsagePublicHook:
    """Tests for the CLI-backend ``update_usage`` hook.

    ``AgentLogger.on_usage`` routes a driver's per-turn usage dict into this
    method so the agent prefix stays in sync with the underlying CLI tool's
    token counts.
    """

    def test_update_usage_sets_input_tokens(self):  # noqa: ANN201  # tracked: #288
        logger = AgentLogger(
            agent_label="Implementer", model_name="claude-sonnet-4-6", event_sink=output_sink()
        )
        assert logger._input_tokens == 0  # noqa: SLF001  # tracked: #288
        logger.update_usage({"input_tokens": 12_345, "output_tokens": 42})
        assert logger._input_tokens == 12_345  # noqa: SLF001  # tracked: #288
        assert logger._latest_usage == {"input_tokens": 12_345, "output_tokens": 42}  # noqa: SLF001  # tracked: #288

    def test_update_usage_overwrites_previous_value(self):  # noqa: ANN201  # tracked: #288
        """The latest turn overwrites; usage does not accumulate."""
        logger = AgentLogger(
            agent_label="X", model_name="claude-sonnet-4-6", event_sink=output_sink()
        )
        logger.update_usage({"input_tokens": 1_000})
        logger.update_usage({"input_tokens": 5_000})
        assert logger._input_tokens == 5_000  # noqa: SLF001  # tracked: #288

    def test_update_usage_empty_dict_is_noop(self):  # noqa: ANN201  # tracked: #288
        logger = AgentLogger(
            agent_label="X", model_name="claude-sonnet-4-6", event_sink=output_sink()
        )
        logger._input_tokens = 500  # noqa: SLF001  # tracked: #288
        logger.update_usage({})
        assert logger._input_tokens == 500  # noqa: SLF001  # tracked: #288

    def test_update_usage_none_is_noop(self):  # noqa: ANN201  # tracked: #288
        logger = AgentLogger(
            agent_label="X", model_name="claude-sonnet-4-6", event_sink=output_sink()
        )
        logger._input_tokens = 500  # noqa: SLF001  # tracked: #288
        logger.update_usage(None)
        assert logger._input_tokens == 500  # noqa: SLF001  # tracked: #288

    def test_update_usage_zero_input_tokens_does_not_overwrite(self):  # noqa: ANN201  # tracked: #288
        """A zero-count usage dict should not clobber a real prior value."""
        logger = AgentLogger(
            agent_label="X", model_name="claude-sonnet-4-6", event_sink=output_sink()
        )
        logger._input_tokens = 2_500  # noqa: SLF001  # tracked: #288
        logger.update_usage({"input_tokens": 0, "output_tokens": 50})
        assert logger._input_tokens == 2_500  # noqa: SLF001  # tracked: #288

    def test_update_usage_reflected_in_prefix(self, capsys):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        """The formatted prefix includes the compact token count after an update."""
        logger = AgentLogger(
            agent_label="Implementer", model_name="claude-sonnet-4-6", event_sink=output_sink()
        )
        logger.update_usage({"input_tokens": 12_345})
        prefix = _strip_ansi(logger._format_prefix())  # noqa: SLF001  # tracked: #288
        assert "12k/1.0M" in prefix

    def test_update_usage_drives_tool_call_prefix(self, capsys):  # noqa: ANN001, ANN201  # tracked: #288
        logger = AgentLogger(
            agent_label="Implementer", model_name="claude-sonnet-4-6", event_sink=output_sink()
        )
        logger.update_usage({"input_tokens": 14_000})
        logger.log_tool_call("Bash", {"command": "ls"})
        out = _strip_ansi(capsys.readouterr().out)
        assert re.search(r"\[Implementer \| \d+\.\ds \| 14k/1\.0M\] → Bash\(", out), out
