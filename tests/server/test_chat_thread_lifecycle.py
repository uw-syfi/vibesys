"""Concurrent experiment-chat thread restoration and teardown tests."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import pytest
from tests.server.support import (
    ServerParts,
    Task,
    auxiliary_agent_providers,
    build_server_parts,
)

from server.chat.factory import (
    ChatAgentBuilder,
    ChatAgentBuildRequest,
    ExperimentChatFactory,
)
from server.chat.manager import ChatAnswer, ChatThreadHandle
from server.chat.options import ChatRunSettings
from server.events import ChatThreadCreatedData, EventType, make_event
from server.run_attachment import AgentSelection, RunAttachment
from vs_sim.api.testing import SimThreads, wait_or_fail

if TYPE_CHECKING:
    from pathlib import Path

_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)


def _remember_thread(parts: ServerParts, thread_id: str = "thread-1") -> ChatThreadCreatedData:
    spec = ChatThreadCreatedData(
        thread_id=thread_id,
        provider="codex",
        model="gpt-test",
        created_at=_TIMESTAMP,
    )
    parts.chat.apply_replayed_event(
        make_event(
            EventType.CHAT_THREAD_CREATED,
            chat_thread_id=thread_id,
            agent_kind="chat",
            data=spec,
        )
    )
    return spec


def _wait_for_active_calls(parts: ServerParts, count: int) -> None:
    with parts.condition:
        assert parts.condition.wait_for(
            lambda: vars(parts.chat)["_active_thread_calls"] == count,
            timeout=2,
        )


def test_concurrent_restore_is_single_flight(tmp_path: Path) -> None:
    threads = SimThreads()
    parts = build_server_parts(tmp_path, threads=threads)
    spec = _remember_thread(parts)
    construction_started = threads.event()
    release_construction = threads.event()
    calls: list[str] = []

    def factory(
        thread_id: str,
        _provider: str | None,
        _model: str | None,
    ) -> ChatThreadHandle:
        calls.append(thread_id)
        construction_started.set()
        wait_or_fail(release_construction, "release construction")
        return ChatThreadHandle(
            spec=spec,
            handler=lambda question: ChatAnswer(
                text=f"answer: {question}", invocation_id=f"exec-{question}"
            ),
        )

    parts.chat.set_thread_factory(factory)

    def scenario() -> None:
        first = Task(threads, partial(parts.chat.chat, "first", spec.thread_id))
        wait_or_fail(construction_started, "construction started")
        second = Task(threads, partial(parts.chat.chat, "second", spec.thread_id))
        _wait_for_active_calls(parts, 2)
        release_construction.set()

        assert first.result() == "answer: first"
        assert second.result() == "answer: second"

    threads.run(scenario)

    assert calls == [spec.thread_id]


class _RestoreFailureError(ValueError):
    """Stable construction failure used by the shared-result test."""

    def __init__(self) -> None:
        super().__init__("restore failed")


def test_concurrent_restore_shares_factory_failure(tmp_path: Path) -> None:
    threads = SimThreads()
    parts = build_server_parts(tmp_path, threads=threads)
    spec = _remember_thread(parts)
    construction_started = threads.event()
    release_construction = threads.event()
    calls = 0

    def factory(
        _thread_id: str,
        _provider: str | None,
        _model: str | None,
    ) -> ChatThreadHandle:
        nonlocal calls
        calls += 1
        construction_started.set()
        wait_or_fail(release_construction, "release construction")
        raise _RestoreFailureError

    parts.chat.set_thread_factory(factory)

    def scenario() -> tuple[str, str]:
        first = Task(threads, partial(parts.chat.chat, "first", spec.thread_id))
        wait_or_fail(construction_started, "construction started")
        second = Task(threads, partial(parts.chat.chat, "second", spec.thread_id))
        _wait_for_active_calls(parts, 2)
        release_construction.set()
        return first.result(), second.result()

    first_answer, second_answer = threads.run(scenario)

    assert first_answer == second_answer
    assert "_RestoreFailureError: restore failed" in first_answer
    assert calls == 1


class _RestoreCancelled(BaseException):
    """Deterministic stand-in for cancellation during construction."""


def test_restore_cancellation_wakes_every_waiter(tmp_path: Path) -> None:
    threads = SimThreads()
    parts = build_server_parts(tmp_path, threads=threads)
    spec = _remember_thread(parts)
    construction_started = threads.event()
    release_construction = threads.event()
    calls = 0

    def factory(
        _thread_id: str,
        _provider: str | None,
        _model: str | None,
    ) -> ChatThreadHandle:
        nonlocal calls
        calls += 1
        construction_started.set()
        wait_or_fail(release_construction, "release construction")
        raise _RestoreCancelled

    parts.chat.set_thread_factory(factory)

    def scenario() -> None:
        first = Task(threads, partial(parts.chat.chat, "first", spec.thread_id))
        wait_or_fail(construction_started, "construction started")
        second = Task(threads, partial(parts.chat.chat, "second", spec.thread_id))
        _wait_for_active_calls(parts, 2)
        release_construction.set()
        with pytest.raises(_RestoreCancelled):
            first.result()
        with pytest.raises(_RestoreCancelled):
            second.result()

    threads.run(scenario)

    _wait_for_active_calls(parts, 0)
    assert calls == 1


def test_thread_turns_serialize_and_shutdown_drains_queued_borrowers(tmp_path: Path) -> None:
    threads = SimThreads()
    parts = build_server_parts(tmp_path, threads=threads)
    first_started = threads.event()
    release_first = threads.event()
    resource_closed = threads.event()
    invocations: list[str] = []

    def handler(question: str) -> ChatAnswer:
        assert not resource_closed.is_set()
        invocations.append(question)
        if question == "first":
            first_started.set()
            wait_or_fail(release_first, "release first")
        assert not resource_closed.is_set()
        return ChatAnswer(text=f"answer: {question}", invocation_id=f"exec-{question}")

    def factory(
        thread_id: str,
        provider: str | None,
        model: str | None,
    ) -> ChatThreadHandle:
        return ChatThreadHandle(
            spec=ChatThreadCreatedData(
                thread_id=thread_id,
                provider=provider or "codex",
                model=model or "gpt-test",
                created_at=_TIMESTAMP,
            ),
            handler=handler,
            close=resource_closed.set,
        )

    parts.chat.set_thread_factory(factory)
    spec = parts.chat.create_thread()

    def shutdown() -> None:
        parts.chat.clear_threads_and_drain()
        resource_closed.set()

    def scenario() -> None:
        first = Task(threads, partial(parts.chat.chat, "first", spec.thread_id))
        wait_or_fail(first_started, "first started")
        second = Task(threads, partial(parts.chat.chat, "second", spec.thread_id))
        _wait_for_active_calls(parts, 2)
        closing = Task(threads, shutdown)

        # The second turn is queued behind the first, which is still running.
        assert invocations == ["first"]
        # Closing before both turns finish would trip the handler's own check
        # that the resource is still open, so no timed negative wait is needed.
        release_first.set()

        assert first.result() == "answer: first"
        assert second.result() == "answer: second"
        closing.result()

    threads.run(scenario)

    assert invocations == ["first", "second"]
    assert resource_closed.is_set()


def test_restoration_finishing_during_shutdown_is_closed_without_invocation(
    tmp_path: Path,
) -> None:
    threads = SimThreads()
    parts = build_server_parts(tmp_path, threads=threads)
    spec = _remember_thread(parts)
    construction_started = threads.event()
    release_construction = threads.event()
    resource_closed = 0
    invocations = 0

    def close() -> None:
        nonlocal resource_closed
        resource_closed += 1

    def handler(_question: str) -> ChatAnswer:
        nonlocal invocations
        invocations += 1
        return ChatAnswer(text="should not run", invocation_id="exec-unused")

    def factory(
        _thread_id: str,
        _provider: str | None,
        _model: str | None,
    ) -> ChatThreadHandle:
        construction_started.set()
        wait_or_fail(release_construction, "release construction")
        return ChatThreadHandle(spec=spec, handler=handler, close=close)

    parts.chat.set_thread_factory(factory)

    def scenario() -> None:
        answer = Task(threads, partial(parts.chat.chat, "question", spec.thread_id))
        wait_or_fail(construction_started, "construction started")
        closing = Task(threads, parts.chat.clear_threads_and_drain)
        with parts.condition:
            assert parts.condition.wait_for(
                lambda: vars(parts.chat)["_thread_factory"] is None,
                timeout=2,
            )
        release_construction.set()

        assert "cannot answer right now" in answer.result()
        closing.result()

    threads.run(scenario)

    assert invocations == 0
    assert resource_closed == 1


def test_thread_creation_finishing_during_shutdown_is_closed_without_publish(
    tmp_path: Path,
) -> None:
    threads = SimThreads()
    parts = build_server_parts(tmp_path, threads=threads)
    construction_started = threads.event()
    release_construction = threads.event()
    resource_closed = 0

    def close() -> None:
        nonlocal resource_closed
        resource_closed += 1

    def factory(
        thread_id: str,
        provider: str | None,
        model: str | None,
    ) -> ChatThreadHandle:
        construction_started.set()
        wait_or_fail(release_construction, "release construction")
        return ChatThreadHandle(
            spec=ChatThreadCreatedData(
                thread_id=thread_id,
                provider=provider or "codex",
                model=model or "gpt-test",
                created_at=_TIMESTAMP,
            ),
            handler=lambda _question: ChatAnswer(text="unused", invocation_id="exec-unused"),
            close=close,
        )

    parts.chat.set_thread_factory(factory)

    def scenario() -> None:
        creation = Task(threads, parts.chat.create_thread)
        wait_or_fail(construction_started, "construction started")
        closing = Task(threads, parts.chat.clear_threads_and_drain)
        with parts.condition:
            assert parts.condition.wait_for(
                lambda: vars(parts.chat)["_thread_factory"] is None,
                timeout=2,
            )
        release_construction.set()

        with pytest.raises(RuntimeError, match="cannot answer right now"):
            creation.result()
        closing.result()

    threads.run(scenario)

    assert parts.chat.threads() == []
    assert resource_closed == 1


def _factory_for_test(
    parts: ServerParts,
    tmp_path: Path,
    build_agent: ChatAgentBuilder,
) -> ExperimentChatFactory:
    defaults = ChatRunSettings(
        provider="codex",
        model="gpt-test",
        agent_providers=auxiliary_agent_providers(),
    )

    return ExperimentChatFactory(
        manager=parts.chat,
        controller=cast("Any", object()),
        executions=cast("Any", object()),
        session=cast("Any", object()),
        attachment=RunAttachment(
            chat_state_dir=tmp_path / "chat",
            agent_defaults=AgentSelection(
                provider=defaults.provider,
                model=defaults.model,
            ),
            agent_providers=auxiliary_agent_providers(),
        ),
        build_agent=build_agent,
        fallback=lambda _question: "fallback",
    )


def test_factory_closes_session_finishing_after_close_once(tmp_path: Path) -> None:
    threads = SimThreads()
    parts = build_server_parts(tmp_path, threads=threads)
    construction_started = threads.event()
    release_construction = threads.event()
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1

    class FakeManagedAgent:
        def turn(self, message: str, *, invocation_id: str | None = None) -> str:
            del message
            del invocation_id
            return "answer"

        def close(self) -> None:
            close()

    def build_agent(request: ChatAgentBuildRequest) -> FakeManagedAgent:
        construction_started.set()
        wait_or_fail(release_construction, "release construction")
        del request
        return FakeManagedAgent()

    factory = _factory_for_test(parts, tmp_path, build_agent)

    def scenario() -> None:
        construction = Task(threads, factory.start)
        wait_or_fail(construction_started, "construction started")
        Task(threads, factory.close).result()
        release_construction.set()
        with pytest.raises(RuntimeError, match="factory is closed"):
            construction.result()

    threads.run(scenario)

    factory.close()
    with pytest.raises(RuntimeError, match="factory is closed"):
        factory.start()
    assert close_calls == 1
