"""Construction and ownership of experiment-chat agent sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from server.chat.manager import (
    ChatManager,
    ChatThreadHandle,
    TerminalChatResource,
)
from server.chat.options import ChatRunSettings
from server.chat.prompts import (
    experiment_chat_continuation_prompt,
    experiment_chat_system_prompt,
)
from server.chat.session import (
    ExperimentChatDependencies,
    ExperimentChatSession,
)
from server.chat.transcript import FileChatTranscriptStore
from server.events import ChatThreadCreatedData, ChatThreadId
from server.run_attachment import AgentSelection, RunAttachment
from vibesys.api import AuxiliaryAgentLaunch
from vs_sim.api import CloseOnce, ReleaseStack, utc_datetime

if TYPE_CHECKING:
    from collections.abc import Callable

    from server.controller import RunController
    from server.execution import ExecutionTracker
    from vibesys.api import ManagedAgent
    from vs_sim.api import Clock, Released, Threads


#: Session-key identifier for the run's default chat, which has no thread ID of
#: its own. Threads are ``uuid4().hex``, so this cannot collide with one; it is
#: the same name the client already shows the default thread under
#: (``DEFAULT_CHAT_THREAD_ID`` in ``clients/libs/run-state``).
DEFAULT_CHAT_THREAD = "default"


@dataclass(frozen=True)
class ChatAgentBuildRequest:
    """Inputs needed to construct one independently owned chat agent."""

    session: AuxiliaryAgentFactory
    selection: AgentSelection
    instance_id: ChatThreadId | None


class AuxiliaryAgentFactory(Protocol):
    """The single run-session capability needed to construct chat."""

    def create_auxiliary_agent(self, launch: AuxiliaryAgentLaunch) -> ManagedAgent:
        """Create one fresh run-attached auxiliary conversation."""
        ...


class ChatAgentBuilder(Protocol):
    """Construct an independently owned chat agent from attached run resources."""

    def __call__(
        self,
        request: ChatAgentBuildRequest,
        /,
    ) -> ManagedAgent:
        """Build one independently owned chat conversation."""
        ...


def build_chat_agent(
    request: ChatAgentBuildRequest,
) -> ManagedAgent:
    """Declare one chat conversation through the managed public boundary."""
    selection = request.selection
    return request.session.create_auxiliary_agent(
        AuxiliaryAgentLaunch(
            role="chat",
            member_id=request.instance_id or DEFAULT_CHAT_THREAD,
            provider=selection.provider,
            model=selection.model,
            system_prompt=experiment_chat_system_prompt(),
            continuation_prompt=experiment_chat_continuation_prompt(),
        )
    )


class ExperimentChatFactory:
    """Build and own chat sessions from an attached core run."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-011106 [PLR0913]; These dependencies have separate owners and lifetimes (manager, run controller/tracker/session, attachment, builder, fallback); a wrapper would only hide the composition boundary.
        self,
        *,
        manager: ChatManager,
        controller: RunController,
        executions: ExecutionTracker,
        session: AuxiliaryAgentFactory,
        attachment: RunAttachment,
        build_agent: ChatAgentBuilder,
        fallback: Callable[[str], str],
        threads: Threads,
        clock: Clock,
    ) -> None:
        """Configure session construction and resource ownership for one run."""
        self._threads = threads
        self._clock = clock
        self._manager = manager
        self._controller = controller
        self._executions = executions
        self._chat_state_dir = attachment.chat_state_dir
        self._defaults = ChatRunSettings(
            provider=attachment.agent_defaults.provider,
            model=attachment.agent_defaults.model,
            agent_providers=attachment.agent_providers,
            role_models=attachment.agent_defaults.role_models,
        )
        self._session = session
        self._build_agent = build_agent
        self._fallback = fallback
        self._lock = self._threads.lock()
        self._closing = self._threads.lock()
        self._close = CloseOnce()
        self._owned = ReleaseStack()
        self._sessions: list[ExperimentChatSession] = []
        self._default_retained = False
        self._transcripts: FileChatTranscriptStore | None = None

    def start(self) -> None:
        """Install the default session and per-thread factory on the manager."""
        with self._lock:
            if self._close.started:
                raise _factory_closed_error()
            if self._transcripts is not None:
                raise _factory_started_error()
            transcripts = FileChatTranscriptStore.open(self._chat_state_dir)
            self._transcripts = transcripts
            self._manager.set_run_settings(self._defaults)
            self._manager.set_thread_factory(self._create_thread)
        default = self._build_session(
            None,
            AgentSelection(
                provider=self._defaults.provider,
                model=self._defaults.model,
            ),
        )
        with self._lock:
            if self._close.started:
                raise _factory_closed_error()
            self._default_retained = self._manager.retain_terminal_resource(
                TerminalChatResource(handler=default.ask, close=default.close)
            )
            if self._default_retained:
                self._sessions.remove(default)
            else:
                self._manager.install_default_handler(default.ask)

    def close(self) -> None:
        """Drain chat calls and close every factory-owned session.

        A session or transcript store whose close raised is closed again on the next call.
        """
        with self._closing:
            self._close.run(self._release)

    def _release(self) -> Released:
        self._manager.clear_threads_and_drain()
        if not self._default_retained:
            self._manager.clear_default_handler_and_drain()
        with self._lock:
            sessions, self._sessions = self._sessions, []
            transcripts, self._transcripts = self._transcripts, None
        if transcripts is not None:
            self._owned.callback(transcripts.close)
        for session in reversed(sessions):
            self._owned.callback(session.close)
        return self._owned.close()

    def _create_thread(
        self,
        thread_id: ChatThreadId,
        provider: str | None,
        model: str | None,
    ) -> ChatThreadHandle:
        selection = self._resolve_selection(
            provider=provider,
            model=model,
        )
        session = self._build_session(thread_id, selection)
        return ChatThreadHandle(
            spec=ChatThreadCreatedData(
                thread_id=thread_id,
                provider=selection.provider,
                model=selection.model,
                created_at=utc_datetime(self._clock),
            ),
            handler=session.ask,
            close=session.close,
        )

    def _resolve_selection(
        self,
        *,
        provider: str | None,
        model: str | None,
    ) -> AgentSelection:
        """Resolve one chat thread's agent choice against the attached run."""
        resolved_provider = provider or self._defaults.provider
        resolved_model = model or self._defaults.model
        supported = self._defaults.agent_providers
        if resolved_provider not in supported:
            message = (
                f"agent provider {resolved_provider!r} is not supported; "
                f"supported providers: {', '.join(supported)}"
            )
            raise ValueError(message)
        return AgentSelection(
            provider=resolved_provider,
            model=resolved_model,
        )

    def _build_session(
        self, thread_id: ChatThreadId | None, selection: AgentSelection
    ) -> ExperimentChatSession:
        with self._lock:
            if self._close.started:
                raise _factory_closed_error()
            transcripts = self._transcripts
            if transcripts is None:
                raise _factory_not_started_error()
            transcript = transcripts.open_transcript(thread_id)
        try:
            transcript_history = transcript.read_history()
            agent = self._build_agent(
                ChatAgentBuildRequest(
                    session=self._session,
                    selection=selection,
                    instance_id=thread_id,
                )
            )
        except BaseException:
            transcript.close()
            raise
        try:
            session = ExperimentChatSession(
                ExperimentChatDependencies(
                    controller=self._controller,
                    executions=self._executions,
                    agent=agent,
                    # The wire leaves the default thread's ID absent instead of
                    # naming it. Stamping DEFAULT_CHAT_THREAD here would file the
                    # session's events under a thread the terminal answer event
                    # does not claim.
                    chat_thread_id=thread_id,
                    transcript=transcript,
                    transcript_history=transcript_history,
                    provider=selection.provider,
                    model=selection.model,
                    fallback=self._fallback,
                    threads=self._threads,
                ),
                agent,
            )
        except BaseException as construction_error:
            transcript.close()
            try:
                agent.close()
            except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-010247 [BLE001]; failed construction still closes resources and preserves the original exception.
                construction_error.add_note(
                    "Additional error while cleaning up chat-session construction: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

        with self._lock:
            if not self._close.started:
                self._sessions.append(session)
                return session
        session.close()
        raise _factory_closed_error()


def _factory_closed_error() -> RuntimeError:
    return RuntimeError("Experiment chat factory is closed")


def _factory_not_started_error() -> RuntimeError:
    return RuntimeError("Experiment chat factory is not started")


def _factory_started_error() -> RuntimeError:
    return RuntimeError("Experiment chat factory is already started")
