"""Experiment-chat handler lease and retained-resource lifecycle tests."""

from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import TYPE_CHECKING

from tests.server.support import build_server_parts

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from server.chat.manager import ChatAnswer, TerminalChatResource


def test_installing_handler_takes_over_from_fallback(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    assert parts.chat.default_agent_available() is False

    parts.chat.install_default_handler(
        lambda question: ChatAnswer(text=f"agent answered: {question}", invocation_id="exec-1")
    )

    assert parts.chat.default_agent_available() is True
    assert parts.chat.chat("why did round 3 fail?") == ("agent answered: why did round 3 fail?")
    parts.chat.install_default_handler(None)
    assert parts.chat.default_agent_available() is False
    assert "chat agent is not available" in parts.chat.chat("and round 4?")


def test_retained_resource_remains_available_until_explicit_close(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.chat.enable_terminal_retention()
    closed = threading.Event()
    resource = TerminalChatResource(
        handler=lambda _question: ChatAnswer(text="terminal agent answer", invocation_id="exec-1"),
        close=closed.set,
    )

    assert parts.chat.retain_terminal_resource(resource)
    parts.controller.finish()
    assert parts.chat.chat("what was the result?") == "terminal agent answer"

    parts.chat.close_terminal_resource()
    parts.chat.close_terminal_resource()
    assert closed.is_set()
    assert parts.chat.default_agent_available() is False


def test_terminal_cleanup_waits_for_in_flight_answer(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    parts.chat.enable_terminal_retention()
    handler_started = threading.Event()
    release_handler = threading.Event()
    cleanup_finished = threading.Event()

    def handler(_question: str) -> ChatAnswer:
        handler_started.set()
        release_handler.wait()
        return ChatAnswer(text="finished answer", invocation_id="exec-1")

    assert parts.chat.retain_terminal_resource(
        TerminalChatResource(handler=handler, close=cleanup_finished.set)
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        answer = pool.submit(parts.chat.chat, "what happened?")
        assert handler_started.wait(timeout=2)
        cleanup = pool.submit(parts.chat.close_terminal_resource)
        assert not cleanup_finished.wait(timeout=0.05)
        assert not cleanup.done()
        release_handler.set()
        assert answer.result(timeout=2) == "finished answer"
        cleanup.result(timeout=2)

    assert cleanup_finished.is_set()


def test_terminal_cleanup_bounds_wait_and_defers_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("server.chat.manager._CHAT_DRAIN_TIMEOUT_SECONDS", 0.01)
    parts = build_server_parts(tmp_path)
    parts.chat.enable_terminal_retention()
    handler_started = threading.Event()
    release_handler = threading.Event()
    resource_closed = threading.Event()

    def handler(_question: str) -> ChatAnswer:
        handler_started.set()
        release_handler.wait()
        return ChatAnswer(text="late answer", invocation_id="exec-1")

    assert parts.chat.retain_terminal_resource(
        TerminalChatResource(handler=handler, close=resource_closed.set)
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        answer = pool.submit(parts.chat.chat, "what happened?")
        assert handler_started.wait(timeout=2)
        started = time.monotonic()
        parts.chat.close_terminal_resource()
        assert time.monotonic() - started < 0.5
        assert not resource_closed.is_set()
        release_handler.set()
        assert answer.result(timeout=2) == "late answer"
        assert resource_closed.wait(timeout=2)
