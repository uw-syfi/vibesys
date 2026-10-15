"""Bounded chat response tests under concurrent journal traffic."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.server.support import REQUEST_TIME, Task, build_server_parts

from server.api.protocol import ChatQuery, Response
from server.chat.manager import ChatAnswer
from server.events import EventType
from vs_sim.api import OsThreads
from vs_sim.api.testing import SimThreads, wait_or_fail

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_sim.api import ThreadLocal


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
        pending = Task(
            threads,
            lambda: parts.api.execute(ChatQuery(timestamp=REQUEST_TIME, text="what changed?")),
        )
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

    response = parts.api.execute(
        ChatQuery(timestamp=REQUEST_TIME, text="hello?", thread_id="missing")
    )

    assert response.chat is not None
    assert "Unknown experiment chat thread 'missing'" in response.chat.answer
    assert response.events == []


class _OneThreadLocals(OsThreads):
    """OS threads whose per-thread values this single-threaded test can read back.

    The test runs every request on its own thread, so one value per ``local`` is that
    thread's; keeping it lets the test inspect what a request left behind.
    """

    def __init__(self) -> None:
        self.values: list[object] = []

    def local[T](self, initial: Callable[[], T]) -> ThreadLocal[T]:
        value = initial()
        self.values.append(value)
        return _FixedLocal(value)


class _FixedLocal[T]:
    def __init__(self, value: T) -> None:
        self._value = value

    def get(self) -> T:
        return self._value


def test_chat_event_capture_is_cleared_after_handler_error(tmp_path: Path) -> None:
    threads = _OneThreadLocals()
    parts = build_server_parts(tmp_path, threads=threads)

    def fail(_question: str) -> ChatAnswer:
        raise RuntimeError

    parts.chat.install_default_handler(fail)
    with pytest.raises(RuntimeError):
        parts.chat.chat_with_event("will fail")

    # The failed request's capture frame is gone from the thread's response stack.
    stacks = [value.captures for value in threads.values if hasattr(value, "captures")]
    assert stacks == [[]]


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
