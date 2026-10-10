"""Experiment-chat handler lease and retained-resource lifecycle tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.server.support import Task, build_server_parts

from vs_sim.api.testing import SimThreads, wait_or_fail

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
    threads = SimThreads()
    parts = build_server_parts(tmp_path, threads=threads)
    parts.chat.enable_terminal_retention()
    closed = threads.event()
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
    threads = SimThreads()
    parts = build_server_parts(tmp_path, threads=threads)
    parts.chat.enable_terminal_retention()
    handler_started = threads.event()
    release_handler = threads.event()
    order: list[str] = []

    def handler(_question: str) -> ChatAnswer:
        handler_started.set()
        wait_or_fail(release_handler, "the test to release the handler")
        order.append("answer finished")
        return ChatAnswer(text="finished answer", invocation_id="exec-1")

    def scenario() -> str:
        assert parts.chat.retain_terminal_resource(
            TerminalChatResource(handler=handler, close=lambda: order.append("resource closed"))
        )
        answer = Task(threads, lambda: parts.chat.chat("what happened?"), name="answer")
        wait_or_fail(handler_started, "the handler to start")
        cleanup = Task(threads, parts.chat.close_terminal_resource, name="cleanup")
        release_handler.set()
        cleanup.result()
        return answer.result()

    assert threads.run(scenario) == "finished answer"
    # The resource closes only after the answer that was using it is done.
    assert order == ["answer finished", "resource closed"]


def test_terminal_cleanup_bounds_wait_and_defers_close(tmp_path: Path) -> None:
    # The drain bound elapses on the simulated clock: the cleanup gives up on the
    # in-flight answer, returns, and leaves the resource open until the answer ends.
    threads = SimThreads()
    parts = build_server_parts(tmp_path, threads=threads)
    parts.chat.enable_terminal_retention()
    handler_started = threads.event()
    release_handler = threads.event()
    resource_closed = threads.event()

    def handler(_question: str) -> ChatAnswer:
        handler_started.set()
        wait_or_fail(release_handler, "the test to release the handler")
        return ChatAnswer(text="late answer", invocation_id="exec-1")

    def scenario() -> str:
        assert parts.chat.retain_terminal_resource(
            TerminalChatResource(handler=handler, close=resource_closed.set)
        )
        answer = Task(threads, lambda: parts.chat.chat("what happened?"), name="answer")
        wait_or_fail(handler_started, "the handler to start")
        parts.chat.close_terminal_resource()
        assert not resource_closed.is_set()
        release_handler.set()
        late = answer.result()
        wait_or_fail(resource_closed, "the deferred close")
        return late

    assert threads.run(scenario) == "late answer"
