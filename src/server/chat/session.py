"""One agent-backed experiment-chat conversation."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from server.chat.prompts import (
    experiment_chat_continuation_prompt,
    experiment_chat_system_prompt,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.chat.evidence import TrajectoryEvidence
    from server.controller import RunController
    from server.execution import ExecutionTracker
    from vibesys.agents.session_key import AgentSessionKey


class ChatAgentClient(Protocol):
    """Minimal text invocation interface required by an experiment chat."""

    def invoke_text(self, **kwargs: Any) -> str:  # noqa: ANN401  # Mirrors agent clients.
        """Invoke the agent with the driver-specific keyword contract."""
        ...

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Name the provider conversation the next turn on the key continues."""
        ...

    def last_turn_provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Name the provider conversation the key's last completed turn ran in."""
        ...


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
    agent_client: ChatAgentClient
    #: Identity of this thread's provider conversation. Follow-ups reuse it so
    #: the agent keeps the context of the questions before them.
    session_key: AgentSessionKey
    #: Thread this session answers on, as the wire names it. ``None`` is the
    #: run's default chat, which carries no thread ID of its own, so streamed
    #: output lands in the same transcript as the terminal answer event.
    chat_thread_id: str | None
    workspace: Path
    state_dir: Path
    agent_shared_state_dir: str
    agent_state_dir: str
    evidence: TrajectoryEvidence
    log: Callable[[str], None]
    environment: Callable[[], dict[str, str]]
    progress: Callable[[], object | None]
    driver: str
    provider: str
    model: str
    fallback: Callable[[str], str]


class ExperimentChatSession:
    """Own one chat agent, transcript, and evidence refresh."""

    def __init__(
        self,
        dependencies: ExperimentChatDependencies,
        resources: Closeable | None = None,
    ) -> None:
        """Initialize a serialized chat session over one provider conversation."""
        self._controller = dependencies.controller
        self._executions = dependencies.executions
        self._agent_client = dependencies.agent_client
        self._session_key = dependencies.session_key
        self._chat_thread_id = dependencies.chat_thread_id
        self._workspace = dependencies.workspace
        self._state_dir = dependencies.state_dir
        self._evidence = dependencies.evidence
        self._log = dependencies.log
        self._environment = dependencies.environment
        self._progress = dependencies.progress
        self._driver = dependencies.driver
        self._provider = dependencies.provider
        self._model = dependencies.model
        self._fallback = dependencies.fallback
        self._resources = resources
        self._lock = threading.Lock()
        self._system_prompt = experiment_chat_system_prompt(
            dependencies.agent_shared_state_dir,
            dependencies.agent_state_dir,
        )
        self._continuation_prompt = experiment_chat_continuation_prompt(
            dependencies.agent_state_dir
        )

    def ask(self, question: str) -> str:
        """Refresh evidence, invoke the chat agent, and persist its answer."""
        with self._lock:
            self._evidence.refresh(self._system_prompt)
            # The shortened prompt assumes the agent is still in the
            # conversation that was given the full instructions, so it is
            # justified by a live provider conversation rather than by the
            # transcript on disk. Without one the turn cold-starts and carries
            # the whole prompt. This is also what puts the instructions back
            # after a driver retires a conversation it has finished serving,
            # such as a renewed Codex thread.
            continued = self._agent_client.provider_session_id(self._session_key)
            answer = self._invoke(question, resumed=continued is not None)
            if (
                continued is not None
                and self._agent_client.last_turn_provider_session_id(self._session_key) != continued
            ):
                # The driver replaced that conversation *while* serving the
                # turn (a Codex thread whose rollout was gone, a Claude session
                # the CLI refused to resume), so the answer above came from an
                # agent that was never given the read-only rules. Ask once
                # more, with them. The retry cold-starts by construction: the
                # client evicted the session and cleared its checkpoint when
                # the driver reported the restart, so a second restart has
                # nothing left to lose and the loop cannot repeat.
                self._log(
                    "[chat] provider session was replaced mid-question; "
                    "re-asking with the full instructions"
                )
                answer = self._invoke(question, resumed=False)
            if not answer.strip():
                answer = (
                    "Chat agent did not return an answer.\n\n"
                    f"Fallback diagnostic:\n{self._fallback(question)}"
                )
            self._append_exchange(question, answer)
            return answer

    def _invoke(self, question: str, *, resumed: bool) -> str:
        """Run one chat turn, reusing this thread's provider conversation."""
        system_prompt = self._continuation_prompt if resumed else self._system_prompt
        execution = self._controller.start_agent_execution(
            "chat",
            "experiment-chat",
            question,
            system_prompt,
            consume_steering=False,
            participates_in_run_control=False,
            driver=self._driver,
            provider=self._provider,
            model=self._model,
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
                answer = self._agent_client.invoke_text(
                    kind="chat",
                    workspace=self._workspace,
                    system_prompt=system_prompt,
                    env=self._environment(),
                    user_prompt=question,
                    round_label="experiment chat",
                    invocation_id=execution.execution_id,
                    progress=self._progress(),
                    reuse_session=True,
                    session_key=self._session_key,
                )
            except BaseException as exc:
                error = exc
                if isinstance(exc, Exception):
                    raise RuntimeError(  # noqa: TRY003, TRY004  # Normalize agent errors.
                        f"Chat agent failed: {type(exc).__name__}: {exc}"
                    ) from exc
                raise
            finally:
                self._controller.after_agent(
                    "chat",
                    "experiment-chat",
                    result=answer,
                    error=error,
                    execution_id=execution.execution_id,
                )
        assert answer is not None  # noqa: S101
        return answer

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
            self._log(f"[warn] could not persist experiment chat: {exc}")
