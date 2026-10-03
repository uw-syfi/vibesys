"""One agent-backed experiment-chat conversation."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from server.chat.manager import ChatAnswer
from server.execution import AgentExecutionRequest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.controller import RunController
    from server.execution import ExecutionTracker
    from vibesys.api import ManagedAgent

_LOG = logging.getLogger(__name__)


class Closeable(Protocol):
    """Resource that supports deterministic cleanup."""

    def close(self) -> None:
        """Release the owned resource."""
        ...


@dataclass(frozen=True)
class ExperimentChatDependencies:
    """Dependencies and resolved agent settings for one chat session."""

    controller: RunController
    executions: ExecutionTracker
    agent: ManagedAgent
    #: Thread this session answers on, as the wire names it. ``None`` is the
    #: run's default chat, which carries no thread ID of its own, so streamed
    #: output lands in the same transcript as the terminal answer event.
    chat_thread_id: str | None
    state_dir: Path
    driver: str
    provider: str
    model: str
    fallback: Callable[[str], str]


class ExperimentChatSession:
    """Own one chat agent and its transcript."""

    def __init__(
        self,
        dependencies: ExperimentChatDependencies,
        resources: Closeable | None = None,
    ) -> None:
        """Initialize a serialized chat session over one provider conversation."""
        self._controller = dependencies.controller
        self._executions = dependencies.executions
        self._agent = dependencies.agent
        self._chat_thread_id = dependencies.chat_thread_id
        self._state_dir = dependencies.state_dir
        self._driver = dependencies.driver
        self._provider = dependencies.provider
        self._model = dependencies.model
        self._fallback = dependencies.fallback
        self._resources = resources
        self._lock = threading.Lock()

    def ask(self, question: str) -> ChatAnswer:
        """Invoke the chat agent and persist its answer."""
        with self._lock:
            answer = self._invoke(question)
            if not answer.text.strip():
                # The turn still ran under the invocation's identity, so the
                # substitute text keeps that id and closes the streamed turn.
                answer = replace(
                    answer,
                    text=(
                        "Chat agent did not return an answer.\n\n"
                        f"Fallback diagnostic:\n{self._fallback(question)}"
                    ),
                )
            self._append_exchange(question, answer.text)
            return answer

    def _invoke(self, question: str) -> ChatAnswer:
        """Run one chat turn, reusing this thread's provider conversation."""
        execution = self._controller.start_agent_execution(
            AgentExecutionRequest(
                kind="chat",
                round_label="experiment-chat",
                user_prompt=question,
                participates_in_run_control=False,
                driver=self._driver,
                provider=self._provider,
                model=self._model,
            ),
        )
        answer: str | None = None
        error: BaseException | None = None
        with self._executions.presentation_scope(
            agent_kind="chat",
            round_label="experiment-chat",
            invocation_id=execution.execution_id,
            chat_thread_id=self._chat_thread_id,
        ):
            try:
                answer = self._agent.turn(question, invocation_id=execution.execution_id)
            except Exception as exc:
                error = exc
                message = f"Chat agent failed: {type(exc).__name__}: {exc}"
                raise RuntimeError(  # Normalize agent errors.
                    message
                ) from exc
            except BaseException as exc:
                error = exc
                raise
            finally:
                self._controller.after_agent(
                    "chat",
                    "experiment-chat",
                    result=answer,
                    error=error,
                    execution_id=execution.execution_id,
                )
        if answer is None:
            message = "chat agent completed without an answer"
            raise RuntimeError(message)
        return ChatAnswer(text=answer, invocation_id=execution.execution_id)

    def close(self) -> None:
        """Release resources owned by this chat session once."""
        resources, self._resources = self._resources, None
        if resources is not None:
            resources.close()

    def _append_exchange(self, question: str, answer: str) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            with (self._state_dir / "conversation.jsonl").open("a", encoding="utf-8") as transcript:
                transcript.write(
                    json.dumps({"question": question, "answer": answer}, ensure_ascii=False) + "\n"
                )
        except OSError as exc:
            _LOG.warning("could not persist experiment chat: %s", exc)
