"""Bounded chat response tests under concurrent journal traffic."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.server.support import Task, build_server_parts

from server.api.protocol import ChatQuery, Response
from server.chat.manager import ChatAnswer
from server.events import EventType
from vs_sim.api.testing import SimThreads, wait_or_fail

if TYPE_CHECKING:
    from pathlib import Path


def test_chat_response_excludes_concurrent_run_events(tmp_path: Path) -> None:
    threads = SimThreads()
    parts = build_server_parts(tmp_path, threads=threads)
    handler_started = threads.event()
    release_handler = threads.event()

    def handler(_question: str) -> ChatAnswer:
        handler_started.set()
        wait_or_fail(release_handler, "the test to release the handler")
        return ChatAnswer(text="bounded answer", invocation_id="exec-bounded")

    def scenario() -> Response:
        pending = Task(threads, lambda: parts.api.execute(ChatQuery(text="what changed?")))
        try:
            # The handler is parked until released, so the output below is
            # guaranteed to land while the chat request is in flight.
            wait_or_fail(handler_started, "the chat handler to start")
            for index in range(1_000):
                parts.journal.publish_output("stdout", f"optimizer output {index}\n")
        finally:
            release_handler.set()
        return pending.result()

    parts.chat.install_default_handler(handler)
    response = threads.run(scenario)

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

    def fail(_question: str) -> ChatAnswer:
        raise RuntimeError

    parts.chat.install_default_handler(fail)
    with pytest.raises(RuntimeError):
        parts.chat.chat_with_event("will fail")

    # lint-waiver: LW-010045 [SLF001]; the thread-local response stack has no public depth accessor, so inspect it to detect a request frame retained after failure.
    assert parts.chat._chat_response_local.captures == []  # noqa: SLF001


def test_nested_chat_captures_keep_each_terminal_event_separate(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    nested_event_text: list[str] = []

    def handler(question: str) -> ChatAnswer:
        if question == "outer":
            _answer, nested_event = parts.chat.chat_with_event("inner")
            assert nested_event is not None
            nested_event_text.append(nested_event.text)
        return ChatAnswer(text=f"answer to {question}", invocation_id=f"exec-{question}")

    parts.chat.install_default_handler(handler)
    answer, event = parts.chat.chat_with_event("outer")

    assert answer == "answer to outer"
    assert event is not None
    assert event.text == "outer"
    assert nested_event_text == ["inner"]
