"""Construction and ownership of experiment-chat agent sessions."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
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
from server.events import ChatThreadCreatedData
from server.run_attachment import AgentSelection, RunAttachment
from vibesys.api import AgentDriver, AuxiliaryAgentLaunch, AuxiliaryReadableInput

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.controller import RunController
    from server.execution import ExecutionTracker
    from vibesys.api import ManagedAgent


#: Session-key identifier for the run's default chat, which has no thread ID of
#: its own. Threads are ``uuid4().hex``, so this cannot collide with one; it is
#: the same name the client already shows the default thread under
#: (``DEFAULT_CHAT_THREAD_ID`` in ``clients/core-state``).
DEFAULT_CHAT_THREAD = "default"
_CHAT_STATE_ENV = "VIBESYS_CHAT_STATE_DIR"


@dataclass(frozen=True)
class ChatAgentBuildRequest:
    """Inputs needed to construct one independently owned chat agent."""

    session: AuxiliaryAgentFactory
    selection: AgentSelection
    instance_id: str | None
    shared_state_dir: Path


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
    shared_state_dir = request.shared_state_dir
    state_path = f"${_CHAT_STATE_ENV}"
    return request.session.create_auxiliary_agent(
        AuxiliaryAgentLaunch(
            role="chat",
            member_id=request.instance_id or DEFAULT_CHAT_THREAD,
            driver=selection.driver,
            provider=selection.provider,
            model=selection.model,
            system_prompt=experiment_chat_system_prompt(state_path),
            continuation_prompt=experiment_chat_continuation_prompt(state_path),
            readable_inputs=(
                AuxiliaryReadableInput(
                    path=shared_state_dir,
                    environment_variable=_CHAT_STATE_ENV,
                    purpose="server chat transcript",
                ),
            ),
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
    ) -> None:
        """Configure session construction and resource ownership for one run."""
        self._manager = manager
        self._controller = controller
        self._executions = executions
        self._chat_state_dir = attachment.chat_state_dir
        self._defaults = ChatRunSettings(
            driver=attachment.agent_defaults.driver,
            provider=attachment.agent_defaults.provider,
            model=attachment.agent_defaults.model,
            agent_drivers=attachment.agent_drivers,
            role_models=attachment.agent_defaults.role_models,
        )
        self._session = session
        self._build_agent = build_agent
        self._fallback = fallback
        self._lock = threading.Lock()
        self._closed = False
        self._sessions: list[ExperimentChatSession] = []
        self._default_retained = False

    def start(self) -> None:
        """Install the default session and per-thread factory on the manager."""
        with self._lock:
            if self._closed:
                raise _factory_closed_error()
            self._manager.set_run_settings(self._defaults)
            self._manager.set_thread_factory(self._create_thread)
        default = self._build_session(
            None,
            AgentSelection(
                driver=self._defaults.driver,
                provider=self._defaults.provider,
                model=self._defaults.model,
            ),
        )
        with self._lock:
            if self._closed:
                raise _factory_closed_error()
            self._default_retained = self._manager.retain_terminal_resource(
                TerminalChatResource(handler=default.ask, close=default.close)
            )
            if self._default_retained:
                self._sessions.remove(default)
            else:
                self._manager.install_default_handler(default.ask)

    def close(self) -> None:
        """Drain chat calls and close every factory-owned session."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._manager.clear_threads_and_drain()
        if not self._default_retained:
            self._manager.clear_default_handler_and_drain()
        with self._lock:
            sessions, self._sessions = self._sessions, []
        first_error: BaseException | None = None
        for session in sessions:
            try:
                session.close()
            except BaseException as exc:  # noqa: BLE001  # lint-waiver: LW-010246 [BLE001]; session teardown attempts every owned session even when one is cancelled.
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def _create_thread(
        self,
        thread_id: str,
        driver: str | None,
        provider: str | None,
        model: str | None,
    ) -> ChatThreadHandle:
        selection = self._resolve_selection(
            driver=driver,
            provider=provider,
            model=model,
        )
        session = self._build_session(thread_id, selection)
        return ChatThreadHandle(
            spec=ChatThreadCreatedData(
                thread_id=thread_id,
                driver=selection.driver,
                provider=selection.provider,
                model=selection.model,
                created_at=datetime.now(UTC),
            ),
            handler=session.ask,
            close=session.close,
        )

    def _resolve_selection(
        self,
        *,
        driver: str | None,
        provider: str | None,
        model: str | None,
    ) -> AgentSelection:
        """Resolve one chat thread's agent choice against the attached run."""
        resolved_driver = _agent_driver(driver or self._defaults.driver)
        resolved_provider = provider or self._defaults.provider
        resolved_model = model or self._defaults.model
        supported = self._defaults.providers_for(resolved_driver)
        if resolved_provider not in supported:
            message = (
                f"agent driver {resolved_driver!r} does not support provider "
                f"{resolved_provider!r}; supported providers: {', '.join(supported)}"
            )
            raise ValueError(message)
        return AgentSelection(
            driver=resolved_driver,
            provider=resolved_provider,
            model=resolved_model,
        )

    def _build_session(
        self, thread_id: str | None, selection: AgentSelection
    ) -> ExperimentChatSession:
        with self._lock:
            if self._closed:
                raise _factory_closed_error()
        shared_state_dir = self._chat_state_dir
        shared_state_dir.mkdir(parents=True, exist_ok=True)
        agent = self._build_agent(
            ChatAgentBuildRequest(
                session=self._session,
                selection=selection,
                instance_id=thread_id,
                shared_state_dir=shared_state_dir,
            )
        )
        state_dir = (
            shared_state_dir if thread_id is None else shared_state_dir / "threads" / thread_id
        )
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
                    state_dir=state_dir,
                    driver=selection.driver,
                    provider=selection.provider,
                    model=selection.model,
                    fallback=self._fallback,
                ),
                agent,
            )
        except BaseException as construction_error:
            try:
                agent.close()
            except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-010247 [BLE001]; failed construction still closes resources and preserves the original exception.
                construction_error.add_note(
                    "Additional error while cleaning up chat-session construction: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

        with self._lock:
            if not self._closed:
                self._sessions.append(session)
                return session
        session.close()
        raise _factory_closed_error()


def _factory_closed_error() -> RuntimeError:
    return RuntimeError("Experiment chat factory is closed")


def _agent_driver(value: str) -> AgentDriver:
    """Validate a wire-supplied driver against the public closed set."""
    if value not in ("agentshim", "omnigent"):
        message = f"auxiliary agent driver is unavailable: {value!r}"
        raise ValueError(message)
    return value
