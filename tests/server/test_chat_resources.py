"""Experiment-chat handler lease and retained-resource lifecycle tests."""

from __future__ import annotations

import concurrent.futures
import threading
from typing import TYPE_CHECKING

from tests.server.support import DEADLOCK_GUARD_S, build_server_parts

if TYPE_CHECKING:
    from pathlib import Path

from server.chat.manager import ChatAnswer, TerminalChatResource


def test_installing_handler_takes_over_from_fallback(tmp_path: Path) -> None:
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


def test_retained_resource_remains_available_until_explicit_close(tmp_path: Path) -> None:
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


def test_terminal_cleanup_waits_for_in_flight_answer(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    parts.chat.enable_terminal_retention()
    handler_started = threading.Event()
    release_handler = threading.Event()
    order: list[str] = []

    def handler(_question: str) -> ChatAnswer:
        handler_started.set()
        release_handler.wait()
        order.append("answer finished")
        return ChatAnswer(text="finished answer", invocation_id="exec-1")

    assert parts.chat.retain_terminal_resource(
        TerminalChatResource(handler=handler, close=lambda: order.append("resource closed"))
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        answer = pool.submit(parts.chat.chat, "what happened?")
        assert handler_started.wait(timeout=DEADLOCK_GUARD_S)
        cleanup = pool.submit(parts.chat.close_terminal_resource)
        release_handler.set()
        assert answer.result(timeout=DEADLOCK_GUARD_S) == "finished answer"
        cleanup.result(timeout=DEADLOCK_GUARD_S)

    # The resource closes only after the answer that was using it is done.
    assert order == ["answer finished", "resource closed"]


def test_terminal_cleanup_bounds_wait_and_defers_close(tmp_path: Path) -> None:
    # A zero drain bound: the cleanup gives up on the in-flight answer at once.
    parts = build_server_parts(tmp_path, chat_drain_timeout_seconds=0.0)
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
        assert handler_started.wait(timeout=DEADLOCK_GUARD_S)
        # Returns without the answer finishing, and leaves the resource open.
        parts.chat.close_terminal_resource()
        assert not resource_closed.is_set()
        release_handler.set()
        assert answer.result(timeout=DEADLOCK_GUARD_S) == "late answer"
        assert resource_closed.wait(timeout=DEADLOCK_GUARD_S)
