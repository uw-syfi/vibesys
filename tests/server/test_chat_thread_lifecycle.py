"""Concurrent experiment-chat thread restoration and teardown tests."""

from __future__ import annotations

import concurrent.futures
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from tests.server.support import ServerParts, build_server_parts

from server.chat.factory import ChatAgentResources, ExperimentChatFactory
from server.chat.manager import ChatThreadHandle
from server.chat.options import ChatRunSettings
from server.events import ChatThreadCreatedData, EventType, make_event
from vibesys.run.integration import AgentSelection

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.run.integration import RunAttachment


def _remember_thread(parts: ServerParts, thread_id: str = "thread-1") -> ChatThreadCreatedData:
    spec = ChatThreadCreatedData(
        thread_id=thread_id,
        driver="agentshim",
        provider="codex",
        model="gpt-test",
        created_at=datetime.now(UTC),
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
    parts = build_server_parts(tmp_path)
    spec = _remember_thread(parts)
    construction_started = threading.Event()
    release_construction = threading.Event()
    calls: list[str] = []

    def factory(
        thread_id: str,
        _driver: str | None,
        _provider: str | None,
        _model: str | None,
    ) -> ChatThreadHandle:
        calls.append(thread_id)
        construction_started.set()
        assert release_construction.wait(timeout=2)
        return ChatThreadHandle(spec=spec, handler=lambda question: f"answer: {question}")

    parts.chat.set_thread_factory(factory)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(parts.chat.chat, "first", spec.thread_id)
        assert construction_started.wait(timeout=2)
        second = pool.submit(parts.chat.chat, "second", spec.thread_id)
        _wait_for_active_calls(parts, 2)
        release_construction.set()

        assert first.result(timeout=2) == "answer: first"
        assert second.result(timeout=2) == "answer: second"

    assert calls == [spec.thread_id]


class _RestoreFailureError(ValueError):
    """Stable construction failure used by the shared-result test."""

    def __init__(self) -> None:
        super().__init__("restore failed")


def test_concurrent_restore_shares_factory_failure(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    spec = _remember_thread(parts)
    construction_started = threading.Event()
    release_construction = threading.Event()
    calls = 0

    def factory(
        _thread_id: str,
        _driver: str | None,
        _provider: str | None,
        _model: str | None,
    ) -> ChatThreadHandle:
        nonlocal calls
        calls += 1
        construction_started.set()
        assert release_construction.wait(timeout=2)
        raise _RestoreFailureError

    parts.chat.set_thread_factory(factory)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(parts.chat.chat, "first", spec.thread_id)
        assert construction_started.wait(timeout=2)
        second = pool.submit(parts.chat.chat, "second", spec.thread_id)
        _wait_for_active_calls(parts, 2)
        release_construction.set()
        first_answer = first.result(timeout=2)
        second_answer = second.result(timeout=2)

    assert first_answer == second_answer
    assert "_RestoreFailureError: restore failed" in first_answer
    assert calls == 1


class _RestoreCancelled(BaseException):
    """Deterministic stand-in for cancellation during construction."""


def test_restore_cancellation_wakes_every_waiter(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    spec = _remember_thread(parts)
    construction_started = threading.Event()
    release_construction = threading.Event()
    calls = 0

    def factory(
        _thread_id: str,
        _driver: str | None,
        _provider: str | None,
        _model: str | None,
    ) -> ChatThreadHandle:
        nonlocal calls
        calls += 1
        construction_started.set()
        assert release_construction.wait(timeout=2)
        raise _RestoreCancelled

    parts.chat.set_thread_factory(factory)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(parts.chat.chat, "first", spec.thread_id)
        assert construction_started.wait(timeout=2)
        second = pool.submit(parts.chat.chat, "second", spec.thread_id)
        _wait_for_active_calls(parts, 2)
        release_construction.set()
        with pytest.raises(_RestoreCancelled):
            first.result(timeout=2)
        with pytest.raises(_RestoreCancelled):
            second.result(timeout=2)

    _wait_for_active_calls(parts, 0)
    assert calls == 1


def test_thread_turns_serialize_and_shutdown_drains_queued_borrowers(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    first_started = threading.Event()
    release_first = threading.Event()
    resource_closed = threading.Event()
    invocations: list[str] = []

    def handler(question: str) -> str:
        assert not resource_closed.is_set()
        invocations.append(question)
        if question == "first":
            first_started.set()
            assert release_first.wait(timeout=2)
        assert not resource_closed.is_set()
        return f"answer: {question}"

    def factory(
        thread_id: str,
        driver: str | None,
        provider: str | None,
        model: str | None,
    ) -> ChatThreadHandle:
        return ChatThreadHandle(
            spec=ChatThreadCreatedData(
                thread_id=thread_id,
                driver=driver or "agentshim",
                provider=provider or "codex",
                model=model or "gpt-test",
                created_at=datetime.now(UTC),
            ),
            handler=handler,
            close=resource_closed.set,
        )

    parts.chat.set_thread_factory(factory)
    spec = parts.chat.create_thread()

    def shutdown() -> None:
        parts.chat.clear_threads_and_drain()
        resource_closed.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(parts.chat.chat, "first", spec.thread_id)
        assert first_started.wait(timeout=2)
        second = pool.submit(parts.chat.chat, "second", spec.thread_id)
        _wait_for_active_calls(parts, 2)
        closing = pool.submit(shutdown)

        assert invocations == ["first"]
        assert not resource_closed.wait(timeout=0.05)
        assert not closing.done()
        release_first.set()

        assert first.result(timeout=2) == "answer: first"
        assert second.result(timeout=2) == "answer: second"
        closing.result(timeout=2)

    assert invocations == ["first", "second"]
    assert resource_closed.is_set()


def test_restoration_finishing_during_shutdown_is_closed_without_invocation(
    tmp_path: Path,
) -> None:
    parts = build_server_parts(tmp_path)
    spec = _remember_thread(parts)
    construction_started = threading.Event()
    release_construction = threading.Event()
    resource_closed = 0
    invocations = 0

    def close() -> None:
        nonlocal resource_closed
        resource_closed += 1

    def handler(_question: str) -> str:
        nonlocal invocations
        invocations += 1
        return "should not run"

    def factory(
        _thread_id: str,
        _driver: str | None,
        _provider: str | None,
        _model: str | None,
    ) -> ChatThreadHandle:
        construction_started.set()
        assert release_construction.wait(timeout=2)
        return ChatThreadHandle(spec=spec, handler=handler, close=close)

    parts.chat.set_thread_factory(factory)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        answer = pool.submit(parts.chat.chat, "question", spec.thread_id)
        assert construction_started.wait(timeout=2)
        closing = pool.submit(parts.chat.clear_threads_and_drain)
        with parts.condition:
            assert parts.condition.wait_for(
                lambda: vars(parts.chat)["_thread_factory"] is None,
                timeout=2,
            )
        release_construction.set()

        assert "cannot answer right now" in answer.result(timeout=2)
        closing.result(timeout=2)

    assert invocations == 0
    assert resource_closed == 1


def test_thread_creation_finishing_during_shutdown_is_closed_without_publish(
    tmp_path: Path,
) -> None:
    parts = build_server_parts(tmp_path)
    construction_started = threading.Event()
    release_construction = threading.Event()
    resource_closed = 0

    def close() -> None:
        nonlocal resource_closed
        resource_closed += 1

    def factory(
        thread_id: str,
        driver: str | None,
        provider: str | None,
        model: str | None,
    ) -> ChatThreadHandle:
        construction_started.set()
        assert release_construction.wait(timeout=2)
        return ChatThreadHandle(
            spec=ChatThreadCreatedData(
                thread_id=thread_id,
                driver=driver or "agentshim",
                provider=provider or "codex",
                model=model or "gpt-test",
                created_at=datetime.now(UTC),
            ),
            handler=lambda _question: "unused",
            close=close,
        )

    parts.chat.set_thread_factory(factory)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        creation = pool.submit(parts.chat.create_thread)
        assert construction_started.wait(timeout=2)
        closing = pool.submit(parts.chat.clear_threads_and_drain)
        with parts.condition:
            assert parts.condition.wait_for(
                lambda: vars(parts.chat)["_thread_factory"] is None,
                timeout=2,
            )
        release_construction.set()

        with pytest.raises(RuntimeError, match="cannot answer right now"):
            creation.result(timeout=2)
        closing.result(timeout=2)

    assert parts.chat.threads() == []
    assert resource_closed == 1


@dataclass(frozen=True)
class _ExternalDirectory:
    path: Path

    def external_directory(self, _name: str) -> Path:
        return self.path


@dataclass(frozen=True)
class _ProjectState:
    path: Path

    def local_namespace(self, _run_id: str, _owner: str) -> _ExternalDirectory:
        return _ExternalDirectory(self.path)


@dataclass(frozen=True)
class _Project:
    state: _ProjectState


def _factory_for_test(
    parts: ServerParts,
    tmp_path: Path,
    build_agent: Callable[[RunAttachment, AgentSelection, str | None, Path], ChatAgentResources],
) -> ExperimentChatFactory:
    defaults = ChatRunSettings(driver="agentshim", provider="codex", model="gpt-test")

    def resolve_selection(
        *, driver: str | None, provider: str | None, model: str | None
    ) -> AgentSelection:
        return AgentSelection(
            driver=driver or defaults.driver,
            provider=provider or defaults.provider,
            model=model or defaults.model,
        )

    return ExperimentChatFactory(
        manager=parts.chat,
        controller=cast("Any", object()),
        executions=cast("Any", object()),
        project=cast("Any", _Project(_ProjectState(tmp_path / "chat"))),
        run_id="run-1",
        workspace=tmp_path,
        log_dir=tmp_path,
        defaults=defaults,
        resolve_selection=resolve_selection,
        attachment=cast("Any", object()),
        build_agent=build_agent,
        fallback=lambda _question: "fallback",
    )


def test_factory_closes_session_finishing_after_close_once(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    construction_started = threading.Event()
    release_construction = threading.Event()
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1

    def build_agent(
        _attachment: RunAttachment,
        _selection: AgentSelection,
        _thread_id: str | None,
        shared_state_dir: Path,
    ) -> ChatAgentResources:
        construction_started.set()
        assert release_construction.wait(timeout=2)
        return ChatAgentResources(
            client=object(),
            close=close,
            log=lambda _message: None,
            flush_logs=lambda: None,
            environment=dict,
            progress=lambda: None,
            agent_shared_state_dir=str(shared_state_dir),
        )

    factory = _factory_for_test(parts, tmp_path, build_agent)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        construction = pool.submit(factory.start)
        assert construction_started.wait(timeout=2)
        pool.submit(factory.close).result(timeout=2)
        release_construction.set()
        with pytest.raises(RuntimeError, match="factory is closed"):
            construction.result(timeout=2)

    factory.close()
    with pytest.raises(RuntimeError, match="factory is closed"):
        factory.start()
    assert close_calls == 1
