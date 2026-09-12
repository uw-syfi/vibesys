"""Experiment-chat routing, thread metadata, and resource leases."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.events import (
    ChatData,
    ChatThreadCreatedData,
    EventHeader,
    EventStatus,
    EventType,
    RunEvent,
)

if TYPE_CHECKING:
    from server.chat.options import ChatRunSettings
    from server.journal import EventJournal
    from server.run_lifecycle import RunStatus

_CHAT_DRAIN_TIMEOUT_SECONDS = 5.0
_CHAT_THREAD_TITLE_MAX_CHARS = 40


@dataclass(frozen=True)
class TerminalChatResource:
    """Chat handler and resources retained while the terminal client is open."""

    handler: Callable[[str], str]
    close: Callable[[], None]


@dataclass(frozen=True)
class ChatThreadHandle:
    """Resolved thread settings and its answering handler."""

    spec: ChatThreadCreatedData
    handler: Callable[[str], str]


ChatThreadFactory = Callable[[str, str | None, str | None, str | None], ChatThreadHandle]


class ChatManager:
    """Own default and per-thread chat routing independently of run execution."""

    def __init__(
        self,
        condition: threading.Condition,
        journal: EventJournal,
        *,
        run_status: Callable[[], RunStatus],
    ) -> None:
        """Initialize chat routing over the shared server condition and journal."""
        self._condition = condition
        self._journal = journal
        self._run_status = run_status
        self._chat_response_local = _ChatResponseLocal()
        self._fallback_answer: Callable[[str], str] | None = None
        self._default_handler: Callable[[str], str] | None = None
        self._thread_factory: ChatThreadFactory | None = None
        self._thread_handlers: dict[str, Callable[[str], str]] = {}
        self._thread_specs: dict[str, ChatThreadCreatedData] = {}
        self._run_settings: ChatRunSettings | None = None
        self._active_default_calls = 0
        self._active_thread_calls = 0
        self._retain_terminal = False
        self._terminal_resource: TerminalChatResource | None = None
        self._retired_terminal_resource: TerminalChatResource | None = None

    @staticmethod
    def replay_filter(header: EventHeader) -> bool:
        """Select persisted events needed to reconstruct chat thread metadata."""
        return header.type is EventType.CHAT_THREAD_CREATED or (
            header.type is EventType.CHAT and header.chat_thread_id is not None
        )

    def apply_replayed_event(self, event: RunEvent) -> None:
        """Fold chat metadata from a live append or resumed journal."""
        captures = self._chat_response_local.captures
        if event.type is EventType.CHAT and captures:
            captures[-1] = event
        if event.type is EventType.CHAT_THREAD_CREATED and isinstance(
            event.data, ChatThreadCreatedData
        ):
            self._thread_specs.setdefault(event.data.thread_id, event.data)
            return
        if (
            event.type is EventType.CHAT
            and event.chat_thread_id is not None
            and isinstance(event.data, ChatData)
            and event.data.thread_title
        ):
            spec = self._thread_specs.get(event.chat_thread_id)
            if spec is not None and not spec.title:
                self._thread_specs[event.chat_thread_id] = spec.model_copy(
                    update={"title": event.data.thread_title}
                )

    def set_fallback_answer(self, answer: Callable[[str], str]) -> None:
        """Set the read-only answer source used without a live chat agent."""
        self._fallback_answer = answer

    def default_agent_available(self) -> bool:
        """Return whether the default chat route has a live agent."""
        with self._condition:
            return self._default_handler is not None

    def chat(self, text: str, thread_id: str | None = None) -> str:
        """Answer and record a question on the default or selected thread."""
        if thread_id is not None:
            return self._thread_chat(text, thread_id)
        with self._condition:
            handler = self._default_handler
            if handler is not None:
                self._active_default_calls += 1
        try:
            if handler is None:
                fallback = (
                    self._fallback_answer(text)
                    if self._fallback_answer
                    else "No recorded summary is available."
                )
                answer = (
                    "The experiment chat agent is not available for this run"
                    f" ({self._unavailable_reason()}), so this is a read-only"
                    " summary from the recorded events rather than an answer.\n\n" + fallback
                )
            else:
                answer = handler(text)
            self._journal.record(
                EventType.CHAT,
                text,
                status=EventStatus.ANSWERED,
                agent_kind="chat",
                round_label="experiment-chat",
                data=ChatData(answer=answer),
            )
            return answer
        finally:
            if handler is not None:
                self._release_default_call()

    def chat_with_event(
        self, text: str, thread_id: str | None = None
    ) -> tuple[str, RunEvent | None]:
        """Answer a question and return its synchronously folded terminal event."""
        captures = self._chat_response_local.captures
        captures.append(None)
        try:
            answer = self.chat(text, thread_id)
            return answer, captures[-1]
        finally:
            captures.pop()

    def create_thread(
        self,
        *,
        driver: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        title: str | None = None,
    ) -> ChatThreadCreatedData:
        """Create, register, and record a chat thread with resolved settings."""
        with self._condition:
            factory = self._thread_factory
        if factory is None:
            raise RuntimeError(  # noqa: TRY003  # Report current run availability.
                "Experiment chat threads are not available for this run "
                f"({self._unavailable_reason()})"
            )
        handle = factory(uuid.uuid4().hex, driver, provider, model)
        spec = handle.spec
        if title is not None and title.strip():
            spec = spec.model_copy(update={"title": title.strip()})
        with self._condition:
            self._thread_specs[spec.thread_id] = spec
            self._thread_handlers[spec.thread_id] = handle.handler
        self._journal.record(
            EventType.CHAT_THREAD_CREATED,
            agent_kind="chat",
            round_label="experiment-chat",
            chat_thread_id=spec.thread_id,
            data=spec,
        )
        return spec

    def threads_locked(self) -> list[ChatThreadCreatedData]:
        """Return known thread metadata while the caller holds the condition."""
        return list(self._thread_specs.values())

    def threads(self) -> list[ChatThreadCreatedData]:
        """Return known chat threads ordered by creation time."""
        with self._condition:
            return sorted(self._thread_specs.values(), key=lambda spec: spec.created_at)

    def set_thread_factory(self, factory: ChatThreadFactory | None) -> None:
        """Set or remove the factory used to create and restore thread agents."""
        with self._condition:
            self._thread_factory = factory

    def set_run_settings(self, settings: ChatRunSettings | None) -> None:
        """Set the chat agent choices advertised for the attached run."""
        with self._condition:
            self._run_settings = settings

    @property
    def run_settings(self) -> ChatRunSettings | None:
        """Return the chat settings for the attached run, if available."""
        with self._condition:
            return self._run_settings

    def install_default_handler(self, handler: Callable[[str], str] | None) -> None:
        """Install or remove the handler for the default chat route."""
        with self._condition:
            self._default_handler = handler

    def clear_default_handler_and_drain(self) -> None:
        """Stop new default calls and wait for active calls to finish."""
        with self._condition:
            self._default_handler = None
            self._wait_locked(lambda: self._active_default_calls > 0, timeout=None)

    def clear_threads_and_drain(self) -> None:
        """Stop thread creation, clear routes, and drain active thread calls."""
        with self._condition:
            self._thread_factory = None
            self._thread_handlers.clear()
            self._wait_locked(
                lambda: self._active_thread_calls > 0, timeout=_CHAT_DRAIN_TIMEOUT_SECONDS
            )

    def enable_terminal_retention(self) -> None:
        """Request that the next default resource survive run teardown."""
        with self._condition:
            self._retain_terminal = True

    def terminal_retention_enabled(self) -> bool:
        """Return whether terminal retention is waiting for a resource."""
        with self._condition:
            return self._retain_terminal

    def retain_terminal_resource(self, resource: TerminalChatResource) -> bool:
        """Retain a default chat resource when terminal retention is enabled."""
        with self._condition:
            if not self._retain_terminal:
                return False
            if self._terminal_resource is not None:
                raise RuntimeError(  # noqa: TRY003  # This invariant has no input value.
                    "Terminal chat resources are already retained"
                )
            self._terminal_resource = resource
            self._default_handler = resource.handler
            return True

    def close_terminal_resource(self) -> None:
        """Drain and close the resource retained for the terminal client."""
        with self._condition:
            resource = self._terminal_resource
            self._terminal_resource = None
            self._retain_terminal = False
            if resource is not None and self._default_handler == resource.handler:
                self._default_handler = None
            if resource is not None and not self._wait_locked(
                lambda: self._active_default_calls > 0,
                timeout=_CHAT_DRAIN_TIMEOUT_SECONDS,
            ):
                self._retired_terminal_resource = resource
                resource = None
        if resource is not None:
            resource.close()

    def _thread_chat(self, text: str, thread_id: str) -> str:
        handler = self._resolve_thread_handler(thread_id)
        if isinstance(handler, str):
            return handler
        with self._condition:
            self._active_thread_calls += 1
        try:
            answer = handler(text)
            thread_title = self._title_thread_if_needed(thread_id, text)
            self._journal.record(
                EventType.CHAT,
                text,
                status=EventStatus.ANSWERED,
                agent_kind="chat",
                round_label="experiment-chat",
                chat_thread_id=thread_id,
                data=ChatData(answer=answer, thread_title=thread_title),
            )
            return answer
        finally:
            with self._condition:
                self._active_thread_calls -= 1
                self._condition.notify_all()

    def _resolve_thread_handler(self, thread_id: str) -> Callable[[str], str] | str:
        with self._condition:
            handler = self._thread_handlers.get(thread_id)
            spec = self._thread_specs.get(thread_id)
            factory = self._thread_factory
        if handler is not None:
            return handler
        if spec is None:
            return (
                f"Unknown experiment chat thread {thread_id!r}. Create one with "
                "/new-chat, or omit the thread to use the default experiment chat."
            )
        if factory is None:
            return (
                f"Experiment chat thread {thread_id!r} cannot answer right now "
                f"({self._unavailable_reason()})."
            )
        try:
            handle = factory(thread_id, spec.driver, spec.provider, spec.model)
        except Exception as exc:  # noqa: BLE001
            return (
                f"Could not restore experiment chat thread {thread_id!r}: "
                f"{type(exc).__name__}: {exc}"
            )
        with self._condition:
            return self._thread_handlers.setdefault(thread_id, handle.handler)

    def _title_thread_if_needed(self, thread_id: str, question: str) -> str | None:
        with self._condition:
            spec = self._thread_specs.get(thread_id)
            if spec is None or spec.title:
                return None
            title = _chat_thread_title(question)
            if not title:
                return None
            self._thread_specs[thread_id] = spec.model_copy(update={"title": title})
            return title

    def _release_default_call(self) -> None:
        retired: TerminalChatResource | None = None
        with self._condition:
            self._active_default_calls -= 1
            if self._active_default_calls == 0:
                retired = self._retired_terminal_resource
                self._retired_terminal_resource = None
            self._condition.notify_all()
        if retired is None:
            return
        try:
            retired.close()
        except Exception as exc:  # noqa: BLE001
            self._journal.publish_output(
                "stderr",
                f"Terminal experiment chat cleanup failed: {type(exc).__name__}: {exc}\n",
                source="terminal-chat",
            )

    def _unavailable_reason(self) -> str:
        return (
            "the run has finished"
            if self._run_status().has_ended
            else "the run has not finished starting up"
        )

    def _wait_locked(self, busy: Callable[[], bool], *, timeout: float | None) -> bool:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while busy():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self._condition.wait(timeout=remaining)
        return True


def _chat_thread_title(question: str) -> str:
    line = next((part.strip() for part in question.strip().splitlines() if part.strip()), "")
    if len(line) <= _CHAT_THREAD_TITLE_MAX_CHARS:
        return line
    cut = line[:_CHAT_THREAD_TITLE_MAX_CHARS]
    head, separator, _rest = cut.rpartition(" ")
    return f"{head.rstrip() if separator else cut}…"


class _ChatResponseLocal(threading.local):
    """Active request scopes awaiting a terminal chat event on this thread."""

    def __init__(self) -> None:
        self.captures: list[RunEvent | None] = []
