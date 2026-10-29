import io
import re
from unittest.mock import MagicMock

import pytest

from headless.render import HeadlessRenderer
from vibesys.api import CoreAgentEventSink
from vibesys.events import AgentOutputChunkData, CoreEvent, ToolCallData, ToolResultData
from vs_agent.api import NULL_AGENT_EVENT_SINK, AgentEvent, AgentEventKind, RoundProgress
from vs_agent.callbacks import (
    AgentLogger,
)
from vs_agent.client import _LoggerObserver
from vs_sim.api.testing import ManualClock

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_DIM = "\033[2m"
_RED = "\033[31m"


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _rendering_sink(*, max_result_len: int | None = None) -> CoreAgentEventSink:
    renderer = HeadlessRenderer()
    if max_result_len is not None:
        renderer.max_result_len = max_result_len
    return CoreAgentEventSink(renderer.handle, clock=ManualClock())


class TestOnThinkingChannel:
    """Agent reasoning is published as analysis; plumbing is marked by drivers."""

    @staticmethod
    def _chunks(*events: AgentEvent) -> list[AgentOutputChunkData]:
        seen: list[CoreEvent] = []
        observer = _LoggerObserver(
            AgentLogger(
                event_sink=CoreAgentEventSink(seen.append, clock=ManualClock()), clock=ManualClock()
            )
        )
        for event in events:
            observer.on_event(event)
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
        seen: list[CoreEvent] = []
        AgentLogger(
            event_sink=CoreAgentEventSink(seen.append, clock=ManualClock()), clock=ManualClock()
        ).on_thinking("")

        assert [event.data for event in seen if isinstance(event.data, AgentOutputChunkData)] == []

    def test_writes_to_log_file(self) -> None:
        log = io.StringIO()
        AgentLogger(
            log_file=log, event_sink=NULL_AGENT_EVENT_SINK, clock=ManualClock()
        ).on_thinking("The ring buffer is the hot path.")

        assert "The ring buffer is the hot path." in log.getvalue()


class TestOnDiagnostic:
    """The explicit hook a driver-marked event routes to."""

    @staticmethod
    def _chunks(text: str) -> list[AgentOutputChunkData]:
        seen: list[CoreEvent] = []
        AgentLogger(
            event_sink=CoreAgentEventSink(seen.append, clock=ManualClock()), clock=ManualClock()
        ).on_diagnostic(text)
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
        AgentLogger(
            log_file=log, event_sink=NULL_AGENT_EVENT_SINK, clock=ManualClock()
        ).on_diagnostic("agentshim: restarting the provider process")

        assert "agentshim: restarting the provider process" in log.getvalue()


class TestToolCorrelation:
    def test_cli_events_get_stable_fifo_call_ids(self) -> None:
        seen: list[CoreEvent] = []
        logger = AgentLogger(
            event_sink=CoreAgentEventSink(seen.append, clock=ManualClock()), clock=ManualClock()
        )
        logger.on_tool_call("Read", {"path": "a"})
        logger.on_tool_call("Read", {"path": "b"})
        logger.on_tool_result("Read", stdout="result a")
        logger.on_tool_result("Read", stdout="result b")

        calls = [event.data for event in seen if isinstance(event.data, ToolCallData)]
        results = [event.data for event in seen if isinstance(event.data, ToolResultData)]
        assert [result.call_id for result in results] == [call.call_id for call in calls]


class TestToolResultOutput:
    def test_prints_result_with_name(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(event_sink=_rendering_sink(), clock=ManualClock())
        logger.log_tool_result("shell", "file1.py\nfile2.py")
        out = capsys.readouterr().out
        assert "file1.py" in out

    def test_truncates_long_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(event_sink=_rendering_sink(max_result_len=10), clock=ManualClock())
        logger.log_tool_result("shell", "a" * 20)
        out = capsys.readouterr().out
        assert "..." in out

    def test_exact_limit_no_truncation(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(event_sink=_rendering_sink(max_result_len=10), clock=ManualClock())
        logger.log_tool_result("shell", "a" * 10)
        out = capsys.readouterr().out
        assert "..." not in out

    def test_custom_max_result_len(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(event_sink=_rendering_sink(max_result_len=5), clock=ManualClock())
        logger.log_tool_result("test", "abcdefghij")
        out = capsys.readouterr().out
        assert "abcde..." in out

    def test_normal_output_is_plain_text(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(event_sink=_rendering_sink(), clock=ManualClock())
        logger.log_tool_result("shell", "ok")
        out = capsys.readouterr().out
        assert "ok" in out
        assert _DIM not in out
        assert _RED not in out

    def test_error_status_is_plain_text(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(event_sink=_rendering_sink(), clock=ManualClock())
        logger.log_tool_result("shell", "Error: file not found")
        out = capsys.readouterr().out
        assert "Error: file not found" in out
        assert _RED not in out
        assert _DIM not in out

    def test_command_failed_exit_code_is_plain_text(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        logger = AgentLogger(event_sink=_rendering_sink(), clock=ManualClock())
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
        assert _RED not in out
        assert _DIM not in out

    def test_command_succeeded_exit_code_is_plain_text(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        logger = AgentLogger(event_sink=_rendering_sink(), clock=ManualClock())
        logger.log_tool_result("execute", "hello world\n[Command succeeded with exit code 0]")
        out = capsys.readouterr().out
        assert "hello world" in out
        assert _DIM not in out
        assert _RED not in out


class TestLogFile:
    """AgentLogger with log_file writes full output to log while truncating stdout."""

    def test_tool_result_full_in_log_truncated_on_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log = io.StringIO()
        logger = AgentLogger(
            log_file=log, event_sink=_rendering_sink(max_result_len=10), clock=ManualClock()
        )
        logger.log_tool_result("shell", "a" * 50)
        stdout = capsys.readouterr().out
        log_text = log.getvalue()
        # stdout is truncated
        assert "..." in stdout
        assert len("a" * 50) > len(stdout)
        # log has full output
        assert "a" * 50 in log_text
        assert "..." not in log_text

    def test_tool_call_full_args_in_log(self, capsys: pytest.CaptureFixture[str]) -> None:
        log = io.StringIO()
        logger = AgentLogger(log_file=log, event_sink=_rendering_sink(), clock=ManualClock())
        long_arg = "x" * 200
        logger.log_tool_call("shell", {"cmd": long_arg})
        stdout = capsys.readouterr().out
        log_text = log.getvalue()
        # stdout truncates args at 80 chars
        assert long_arg not in stdout
        assert "..." in stdout
        # log has full args
        assert long_arg in log_text

    def test_text_written_to_log(self) -> None:
        log = io.StringIO()
        logger = AgentLogger(log_file=log, event_sink=NULL_AGENT_EVENT_SINK, clock=ManualClock())
        logger.log_text("hello")
        log_text = log.getvalue()
        assert "hello" in log_text

    def test_thinking_written_to_log(self) -> None:
        log = io.StringIO()
        logger = AgentLogger(log_file=log, event_sink=NULL_AGENT_EVENT_SINK, clock=ManualClock())
        logger.on_thinking("deep thoughts")
        assert "deep thoughts" in log.getvalue()

    def test_no_log_file_works_normally(self, capsys: pytest.CaptureFixture[str]) -> None:
        """AgentLogger without log_file still works as before."""
        logger = AgentLogger(event_sink=_rendering_sink(max_result_len=10), clock=ManualClock())
        logger.log_tool_result("shell", "a" * 20)
        stdout = capsys.readouterr().out
        assert "..." in stdout

    def test_log_text_flushes_log_file(self) -> None:
        """Streamed events must hit disk immediately so ``tail -f`` on the run
        log shows codex output as it arrives, not in buffered bursts."""
        log = MagicMock()
        logger = AgentLogger(log_file=log, event_sink=NULL_AGENT_EVENT_SINK, clock=ManualClock())
        logger.log_text("streamed chunk")
        assert log.flush.called

    def test_tool_result_flushes_log_file(self) -> None:
        log = MagicMock()
        logger = AgentLogger(log_file=log, event_sink=NULL_AGENT_EVENT_SINK, clock=ManualClock())
        logger.log_tool_result("shell", "ok")
        assert log.flush.called


class TestPrefixFormat:
    def test_no_label_no_prefix_on_streaming(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Existing behavior preserved: AgentLogger without agent_label produces no prefix
        logger = AgentLogger(event_sink=_rendering_sink(), clock=ManualClock())
        logger.log_text("hello")
        out = _strip_ansi(capsys.readouterr().out)
        assert "[" not in out
        assert "hello" in out

    def test_prefix_with_label_and_a_known_window(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(
            agent_label="Implementer",
            context_window=1_000_000,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.log_text("hi")
        out = _strip_ansi(capsys.readouterr().out)
        # Format: [Implementer | <float>s | 0/1.0M] hi
        assert re.search(r"\[Implementer \| \d+\.\ds \| 0/1\.0M\]", out), out

    def test_prefix_includes_progress(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(
            agent_label="Implementer",
            progress=RoundProgress(3, 24),
            context_window=1_000_000,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.log_text("hi")
        out = _strip_ansi(capsys.readouterr().out)
        assert re.search(
            r"\[Round 3/24 \| Implementer \| \d+\.\ds \| 0/1\.0M\]",
            out,
        ), out

    def test_prefix_with_a_1_05m_window(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(
            agent_label="Judge",
            context_window=1_050_000,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.log_text("hi")
        out = _strip_ansi(capsys.readouterr().out)
        # 1_050_000 -> "1.0M" or "1.1M" depending on float rounding; accept either
        assert re.search(r"\[Judge \| \d+\.\ds \| 0/1\.\dM\]", out), out

    def test_prefix_omits_max_when_the_window_is_unknown(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        logger = AgentLogger(
            agent_label="X",
            model_name="unknown-future-model",
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.log_text("y")
        out = _strip_ansi(capsys.readouterr().out)
        # No "/" between count and bracket close
        match = re.search(r"\[X \| \d+\.\ds \| ([^\]]+)\]", out)
        assert match, out
        assert match.group(1) == "0"

    def test_prefix_omits_max_when_no_model_name(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(agent_label="X", event_sink=_rendering_sink(), clock=ManualClock())
        logger.log_text("y")
        out = _strip_ansi(capsys.readouterr().out)
        match = re.search(r"\[X \| \d+\.\ds \| ([^\]]+)\]", out)
        assert match, out
        assert match.group(1) == "0"

    def test_prefix_updates_after_update_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(
            agent_label="Implementer",
            context_window=1_000_000,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.update_usage({"input_tokens": 20_100, "output_tokens": 100})
        capsys.readouterr()  # discard
        logger.log_text("next")
        out = _strip_ansi(capsys.readouterr().out)
        assert "20k/1.0M" in out, out

    def test_tool_call_path_uses_dynamic_prefix(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(
            agent_label="Implementer",
            context_window=1_050_000,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.update_usage({"input_tokens": 5_000, "output_tokens": 50})
        logger.log_tool_call("shell", {"cmd": "ls"})
        out = _strip_ansi(capsys.readouterr().out)
        # Tool-call line should carry the dynamic prefix with elapsed and tokens
        assert re.search(r"\[Implementer \| \d+\.\ds \| 5k/1\.\dM\] → shell\(", out), out

    def test_the_given_window_is_the_limit_shown(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(
            agent_label="Test",
            context_window=999_999,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.log_text("x")
        out = _strip_ansi(capsys.readouterr().out)
        # Used tokens still 0 (no usage update yet); max is the given 999_999 → "999k"
        assert "0/999k" in out, out

    def test_elapsed_time_advances(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The logger reads the injected clock, so the elapsed value in the prefix is exact.
        clock = ManualClock(1000.0)
        logger = AgentLogger(
            agent_label="Implementer",
            context_window=1_000_000,
            event_sink=_rendering_sink(),
            clock=clock,
        )
        clock.advance(308.2)
        logger.log_text("hi")
        out = _strip_ansi(capsys.readouterr().out)
        assert "308.2s" in out, out


class TestUpdateUsagePublicHook:
    """Tests for the CLI-backend ``update_usage`` hook.

    ``AgentLogger.update_usage`` takes a driver's per-turn usage dict so
    the agent prefix stays in sync with the underlying CLI tool's token counts.
    """

    def test_update_usage_sets_input_tokens(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(
            agent_label="Implementer",
            context_window=1_000_000,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.update_usage({"input_tokens": 12_345, "output_tokens": 42})
        logger.log_text("status")
        out = _strip_ansi(capsys.readouterr().out)
        assert "12k/1.0M" in out

    def test_update_usage_overwrites_previous_value(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The latest turn overwrites; usage does not accumulate."""
        logger = AgentLogger(
            agent_label="X",
            context_window=1_000_000,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.update_usage({"input_tokens": 1_000})
        logger.update_usage({"input_tokens": 5_000})
        logger.log_text("status")
        out = _strip_ansi(capsys.readouterr().out)
        assert "5k/1.0M" in out

    def test_update_usage_empty_dict_is_noop(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(
            agent_label="X",
            context_window=1_000_000,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.update_usage({"input_tokens": 500})
        logger.update_usage({})
        logger.log_text("status")
        out = _strip_ansi(capsys.readouterr().out)
        assert "500/1.0M" in out

    def test_update_usage_none_is_noop(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(
            agent_label="X",
            context_window=1_000_000,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.update_usage({"input_tokens": 500})
        logger.update_usage(None)
        logger.log_text("status")
        out = _strip_ansi(capsys.readouterr().out)
        assert "500/1.0M" in out

    def test_update_usage_zero_input_tokens_does_not_overwrite(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A zero-count usage dict should not clobber a real prior value."""
        logger = AgentLogger(
            agent_label="X",
            context_window=1_000_000,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.update_usage({"input_tokens": 25_000})
        logger.update_usage({"input_tokens": 0, "output_tokens": 50})
        logger.log_text("status")
        out = _strip_ansi(capsys.readouterr().out)
        assert "25k/1.0M" in out

    def test_update_usage_reflected_in_prefix(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The formatted prefix includes the compact token count after an update."""
        logger = AgentLogger(
            agent_label="Implementer",
            context_window=1_000_000,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.update_usage({"input_tokens": 12_345})
        logger.log_text("status")
        out = _strip_ansi(capsys.readouterr().out)
        assert "12k/1.0M" in out

    def test_update_usage_drives_tool_call_prefix(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = AgentLogger(
            agent_label="Implementer",
            context_window=1_000_000,
            event_sink=_rendering_sink(),
            clock=ManualClock(),
        )
        logger.update_usage({"input_tokens": 14_000})
        logger.log_tool_call("Bash", {"command": "ls"})
        out = _strip_ansi(capsys.readouterr().out)
        assert re.search(r"\[Implementer \| \d+\.\ds \| 14k/1\.0M\] → Bash\(", out), out
