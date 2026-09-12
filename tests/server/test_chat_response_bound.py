"""Bounded chat response tests under concurrent journal traffic."""

from __future__ import annotations

import concurrent.futures
import threading
from typing import TYPE_CHECKING

import pytest
from tests.server.support import build_server_parts

from server.api.protocol import ChatQuery
from server.events import EventType

if TYPE_CHECKING:
    from pathlib import Path


def test_chat_response_excludes_concurrent_run_events(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    handler_started = threading.Event()
    release_handler = threading.Event()

    def handler(_question: str) -> str:
        handler_started.set()
        assert release_handler.wait(timeout=2)
        return "bounded answer"

    parts.chat.install_default_handler(handler)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(parts.api.execute, ChatQuery(text="what changed?"))
        assert handler_started.wait(timeout=2)
        for index in range(1_000):
            parts.journal.publish_output("stdout", f"optimizer output {index}\n")
        release_handler.set()
        response = pending.result(timeout=2)

    assert response.chat is not None
    assert response.chat.answer == "bounded answer"
    assert len(response.events) == 1
    assert response.events[0].type is EventType.CHAT
    assert response.events[0].text == "what changed?"

    history = parts.journal.read()
    assert sum(event.type is EventType.OUTPUT for event in history) == 1_000
    assert history[-1] == response.events[0]
    assert len(response.model_dump_json()) < 1_000


def test_unknown_thread_chat_response_has_no_events(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)

    response = parts.api.execute(ChatQuery(text="hello?", thread_id="missing"))

    assert response.chat is not None
    assert "Unknown experiment chat thread 'missing'" in response.chat.answer
    assert response.events == []


def test_chat_event_capture_is_cleared_after_handler_error(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)

    def fail(_question: str) -> str:
        raise RuntimeError

    parts.chat.install_default_handler(fail)
    with pytest.raises(RuntimeError):
        parts.chat.chat_with_event("will fail")

    assert parts.chat._chat_response_local.captures == []  # noqa: SLF001


def test_nested_chat_captures_keep_each_terminal_event_separate(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    nested_event_text: list[str] = []

    def handler(question: str) -> str:
        if question == "outer":
            _answer, nested_event = parts.chat.chat_with_event("inner")
            assert nested_event is not None
            nested_event_text.append(nested_event.text)
        return f"answer to {question}"

    parts.chat.install_default_handler(handler)
    answer, event = parts.chat.chat_with_event("outer")

    assert answer == "answer to outer"
    assert event is not None
    assert event.text == "outer"
    assert nested_event_text == ["inner"]
    assert parts.chat._chat_response_local.captures == []  # noqa: SLF001
