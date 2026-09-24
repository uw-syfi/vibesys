"""Construction and ownership of experiment-chat agent sessions."""

from __future__ import annotations

import threading
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from server.chat.manager import (
    ChatManager,
    ChatThreadHandle,
    TerminalChatResource,
)
from server.chat.session import (
    ExperimentChatDependencies,
    ExperimentChatSession,
)
from server.events import ChatThreadCreatedData
from server.run_attachment import AgentSelection, RunAttachment
from vibesys.api import agent_spec_from_config, output_sink
from vs_agent.api import AgentSessionKey, SessionScope, build_agent_client
from vs_project.api import RunLogger
from vs_sandbox.api import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.chat.options import ChatRunSettings
    from server.controller import RunController
    from server.execution import ExecutionTracker
    from vibesys.api import RunSession
    from vs_agent.api import MCPServerSpec
    from vs_project.api import Project


#: Session-key identifier for the run's default chat, which has no thread ID of
#: its own. Threads are ``uuid4().hex``, so this cannot collide with one; it is
#: the same name the client already shows the default thread under
#: (``DEFAULT_CHAT_THREAD_ID`` in ``clients/core-state``).
DEFAULT_CHAT_THREAD = "default"


class SelectionResolver(Protocol):
    """Resolve optional thread choices into a complete agent selection."""

    def __call__(
        self,
        *,
        driver: str | None,
        provider: str | None,
        model: str | None,
    ) -> AgentSelection:
        """Validate and complete one requested agent selection."""
        ...


@dataclass(frozen=True)
class ChatAgentResources:
    """One server-owned chat agent and its runtime callbacks."""

    client: Any
    close: Callable[[], None]
    log: Callable[[str], None]
    flush_logs: Callable[[], None]
    environment: Callable[[], dict[str, str]]
    progress: Callable[[], object | None]
    agent_shared_state_dir: str
    mcp_servers: tuple[MCPServerSpec, ...]


class ChatAgentBuilder(Protocol):
    """Construct an independently owned chat agent from attached run resources."""

    def __call__(
        self,
        session: RunSession,
        attachment: RunAttachment,
        selection: AgentSelection,
        instance_id: str | None,
        shared_state_dir: Path,
        /,
    ) -> ChatAgentResources:
        """Build resources for one independently owned chat agent."""
        ...


#: Fixed container path the chat shared-state directory is mounted at inside
#: an isolated agent-construction environment. A deployment constant, not a
#: derived value: `vibesys.api.RunSession.open_agent_environment` folds this
#: into the run's own environment request as a plain bind-mount target, the
#: same way `HostResource.agent_path` names any other fixed container path.
_CHAT_CONTAINER_STATE_DIR = "/opt/vibesys-chat"


def build_chat_agent(
    session: RunSession,
    attachment: RunAttachment,
    selection: AgentSelection,
    instance_id: str | None,
    shared_state_dir: Path,
) -> ChatAgentResources:
    """Build one chat agent without making the core aware of chat sessions."""
    resources = ExitStack()
    try:
        logger = RunLogger(attachment.log_dir, tee_stderr=False)
        resources.callback(logger.close)
        logger_name = (
            "experiment-chat" if instance_id is None else f"experiment-chat-{instance_id[:8]}"
        )
        logger.switch(logger_name)

        env = session.open_agent_environment(
            mounts=(
                HostResource(
                    shared_state_dir,
                    HostResourceAccess.READ_ONLY,
                    "server chat transcript",
                    agent_path=_CHAT_CONTAINER_STATE_DIR,
                ),
            )
        )
        resources.callback(env.close)
        agent_shared_state_dir = (
            env.agent_path(shared_state_dir) if env.isolated else str(shared_state_dir)
        )
        tool_servers = env.investigation_tools()

        config = env.config.model_copy(
            update={"agent": env.config.agent.model_copy(update={"driver": selection.driver})}
        )
        agent_spec = agent_spec_from_config(
            config,
            backend=attachment.agent_backend,
            driver=selection.driver,
            provider=selection.provider,
            model=selection.model,
        )
        client = build_agent_client(
            spec=agent_spec,
            backends=env.backends,
            skill_source_dirs=list(env.skill_source_dirs),
            skill_selection=env.skill_selection,
            run_log_file=logger.writer,
            use_docker=env.use_docker,
            log_dir=attachment.log_dir,
            project_path_policy=env.project_path_policy,
            require_host_sandbox=not env.use_docker,
            host_resources=(
                *env.host_resources,
                HostResource(
                    shared_state_dir,
                    HostResourceAccess.READ_ONLY,
                    "server chat transcript",
                ),
            ),
            events=output_sink(),
        )
        resources.callback(client.close)
        owner = resources.pop_all()
        return ChatAgentResources(
            client=client,
            close=owner.close,
            log=logger.lprint,
            flush_logs=logger.writer.flush,
            environment=dict,
            progress=lambda: None,
            agent_shared_state_dir=agent_shared_state_dir,
            mcp_servers=tool_servers,
        )
    except BaseException as construction_error:
        try:
            resources.close()
        except BaseException as cleanup_error:
            construction_error.add_note(
                "Additional error while cleaning up chat-agent construction: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        raise


class ExperimentChatFactory:
    """Build and own chat sessions from an attached core run."""

    def __init__(  # Construction wires independent run resources.
        self,
        *,
        manager: ChatManager,
        controller: RunController,
        executions: ExecutionTracker,
        project: Project,
        run_id: str,
        workspace: Path,
        defaults: ChatRunSettings,
        resolve_selection: SelectionResolver,
        session: RunSession,
        attachment: RunAttachment,
        build_agent: ChatAgentBuilder,
        fallback: Callable[[str], str],
    ) -> None:
        """Configure session construction and resource ownership for one run."""
        self._manager = manager
        self._controller = controller
        self._executions = executions
        self._project = project
        self._run_id = run_id
        self._workspace = workspace
        self._defaults = defaults
        self._resolve_selection = resolve_selection
        self._session = session
        self._attachment = attachment
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
            except BaseException as exc:
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

    def _build_session(
        self, thread_id: str | None, selection: AgentSelection
    ) -> ExperimentChatSession:
        with self._lock:
            if self._closed:
                raise _factory_closed_error()
        shared_state_dir = self._project.state.local_namespace(
            self._run_id, "server"
        ).external_directory("chat")
        resources = self._build_agent(
            self._session, self._attachment, selection, thread_id, shared_state_dir
        )
        state_dir = (
            shared_state_dir if thread_id is None else shared_state_dir / "threads" / thread_id
        )
        agent_state_dir = (
            resources.agent_shared_state_dir
            if thread_id is None
            else f"{resources.agent_shared_state_dir}/threads/{thread_id}"
        )
        try:
            session = ExperimentChatSession(
                ExperimentChatDependencies(
                    controller=self._controller,
                    executions=self._executions,
                    agent_client=resources.client,
                    # One conversation per thread. The run's default chat has no
                    # thread ID of its own, so it names the identifier the threads
                    # cannot collide with.
                    session_key=AgentSessionKey(
                        SessionScope.CHAT,
                        DEFAULT_CHAT_THREAD if thread_id is None else thread_id,
                    ),
                    # The wire leaves the default thread's ID absent instead of
                    # naming it, so the session key above and this field disagree
                    # for that one thread on purpose. Stamping DEFAULT_CHAT_THREAD
                    # here would file the session's events under a thread the
                    # terminal answer event does not claim.
                    chat_thread_id=thread_id,
                    workspace=self._workspace,
                    state_dir=state_dir,
                    agent_state_dir=agent_state_dir,
                    mcp_servers=resources.mcp_servers,
                    log=resources.log,
                    environment=resources.environment,
                    progress=resources.progress,
                    driver=selection.driver,
                    provider=selection.provider,
                    model=selection.model,
                    fallback=self._fallback,
                ),
                _CloseCallback(resources.close),
            )
        except BaseException as construction_error:
            try:
                resources.close()
            except BaseException as cleanup_error:
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


class _CloseCallback:
    """Adapt a resource close callback to the session ownership protocol."""

    def __init__(self, close: Callable[[], None]) -> None:
        self._close = close

    def close(self) -> None:
        close, self._close = self._close, _noop
        close()


def _noop() -> None:
    pass


def _factory_closed_error() -> RuntimeError:
    return RuntimeError("Experiment chat factory is closed")
