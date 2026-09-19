"""Tests for the run-log helpers in vibesys.render.log."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

from vibesys.render import log as log_module
from vibesys.render.log import (
    log_agent_config,
    log_and_print,
    log_json_and_print,
    log_markdown_and_print,
    log_prompt_markdown_and_print,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


def _patched_sink() -> tuple[MagicMock, AbstractContextManager[Any]]:
    sink = MagicMock()
    return sink, patch.object(log_module, "output_sink", return_value=sink)


def test_log_agent_config_without_log_file_is_a_noop() -> None:
    log_agent_config(object(), "label", None)


def test_log_agent_config_lists_sorted_tools_with_first_description_line() -> None:
    tools = {
        "zeta": SimpleNamespace(description="Last tool\nsecond line"),
        "alpha": SimpleNamespace(description=""),
    }
    agent = SimpleNamespace(
        builder=SimpleNamespace(
            nodes={"tools": SimpleNamespace(runnable=SimpleNamespace(tools_by_name=tools))}
        )
    )
    log = StringIO()

    log_agent_config(agent, "Judge", log)

    text = log.getvalue()
    assert "Agent Configuration: Judge" in text
    assert "Tools (2):" in text
    assert text.index("- alpha: ") < text.index("- zeta: Last tool\n")
    assert "second line" not in text


def test_log_agent_config_reports_unextractable_tools() -> None:
    log = StringIO()

    log_agent_config(object(), "Judge", log)

    assert "Tools: <unable to extract>" in log.getvalue()


def test_log_markdown_and_print_emits_channel_and_writes_log() -> None:
    sink, ctx = _patched_sink()
    log = StringIO()
    with ctx:
        log_markdown_and_print("# hi", log, channel="prompt")
    sink.agent_output.assert_called_once_with("# hi\n", channel="prompt")
    assert log.getvalue() == "# hi\n"


def test_log_markdown_and_print_defaults_to_assistant_without_log_file() -> None:
    sink, ctx = _patched_sink()
    with ctx:
        log_markdown_and_print("x")
    sink.agent_output.assert_called_once_with("x\n", channel="assistant")


def test_log_and_print_uses_diagnostic_channel() -> None:
    sink, ctx = _patched_sink()
    log = StringIO()
    with ctx:
        log_and_print("note", log)
    sink.agent_output.assert_called_once_with("note\n", channel="diagnostic")
    assert log.getvalue() == "note\n"


def test_log_prompt_markdown_and_print_uses_prompt_channel() -> None:
    sink, ctx = _patched_sink()
    with ctx:
        log_prompt_markdown_and_print("p")
    sink.agent_output.assert_called_once_with("p\n", channel="prompt")


def test_log_json_and_print_uses_diagnostic_channel() -> None:
    sink, ctx = _patched_sink()
    log = StringIO()
    with ctx:
        log_json_and_print('{"a":1}', log)
    sink.agent_output.assert_called_once_with('{"a":1}\n', channel="diagnostic")
    assert log.getvalue() == '{"a":1}\n'
