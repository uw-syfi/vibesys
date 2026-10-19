"""Runtime ownership of product-composed auxiliary agent conversations."""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_agent.api import AgentSessionKey, SessionScope, check_tool_servers
from vs_project.api import RunLogger
from vs_sim.api import CloseOnce, ReleaseStack

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_agent.api import AgentClientProtocol, AgentEventSink, AgentSpec, ToolServerFailure
    from vs_mcp.api import ToolServerDescriptor
    from vs_runtime._agent_execution import AgentExecutionEnvironment
    from vs_sandbox.api import HostResource
    from vs_sim.api import Clock, Released, Threads


@dataclass(frozen=True, slots=True)
class ToolServerStartupCheck:
    """A check of a conversation's tool servers and where its failures are published.

    ``check`` returns the servers that cannot start; each is published on ``events`` as an
    error tool result naming the server and the cause, which a client shows in the conversation.
    """

    check: Callable[[], tuple[ToolServerFailure, ...]]
    events: AgentEventSink


class _Closeable(Protocol):
    """One resource whose close operation is idempotent."""

    def close(self) -> None:
        """Release the resource."""
        ...


@dataclass(frozen=True, slots=True)
class ManagedConversationSpec:
    """Fixed execution policy for one auxiliary conversation."""

    role: str
    member_id: str
    workspace: Path
    system_prompt: str
    continuation_prompt: str | None = None
    tool_servers: tuple[ToolServerDescriptor, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    tool_server_checks: tuple[ToolServerDescriptor, ...] = ()
    """Startup checks of ``tool_servers``: each exits 0 when its server could start.

    In an agent container, each runs there before a turn until all pass, and a failure is
    reported on the conversation's event stream with its cause. On the host nothing runs: the
    server starts on the interpreter that runs VibeSys.
    """


class ManagedConversation(Protocol):
    """A serialized, context-preserving, explicitly closeable conversation."""

    def turn(self, message: str, *, invocation_id: str | None = None) -> str:
        """Send one follow-on message in this conversation."""
        ...

    def close(self) -> None:
        """Release this conversation and every transferred resource."""
        ...


class _RuntimeManagedConversation:
    """Thread-safe owner of one client's provider conversation and resources."""

    def __init__(
        self,
        client: AgentClientProtocol,
        spec: ManagedConversationSpec,
        resources: tuple[_Closeable, ...],
        threads: Threads,
        startup: ToolServerStartupCheck | None = None,
    ) -> None:
        for name, value in (("role", spec.role), ("member_id", spec.member_id)):
            if not value.strip():
                error = f"managed conversation {name} must not be empty"
                raise ValueError(error)
        self._client = client
        self._spec = spec
        self._resources = ReleaseStack()
        for resource in resources:
            self._resources.callback(resource.close)
        self._session_key = AgentSessionKey(
            SessionScope.MEMBER,
            f"{spec.member_id}:{uuid.uuid4().hex}",
        )
        self._lock = threads.lock()
        self._close = CloseOnce()
        self._startup = startup

    def turn(self, message: str, *, invocation_id: str | None = None) -> str:
        """Serialize a turn and reuse only this object's provider context."""
        if not message.strip():
            error = "managed conversation turn message must not be empty"
            raise ValueError(error)
        with self._lock:
            if self._close.started:
                error = "managed conversation is closed"
                raise RuntimeError(error)
            self._report_tool_server_failures(invocation_id)
            previous = self._client.provider_session_id(self._session_key)
            prompt = (
                self._spec.continuation_prompt
                if previous is not None and self._spec.continuation_prompt is not None
                else self._spec.system_prompt
            )
            answer = self._invoke(message, prompt, invocation_id=invocation_id)
            if (
                previous is not None
                and prompt == self._spec.continuation_prompt
                and self._client.last_turn_provider_session_id(self._session_key) != previous
            ):
                return self._invoke(
                    message,
                    self._spec.system_prompt,
                    invocation_id=invocation_id,
                )
            return answer

    def _report_tool_server_failures(self, invocation_id: str | None) -> None:
        """Check the tool servers until every one passes; say why each failing one cannot start.

        The turn still runs: the agent can answer without its tools, and the operator sees on
        the stream which server it lacks and why.
        """
        if self._startup is None:
            return
        failures = self._startup.check()
        if not failures:
            self._startup = None
            return
        for failure in failures:
            self._startup.events.tool_result(
                failure.server,
                f"tool server {failure.server} did not start: {failure.detail}",
                is_error=True,
                agent_kind=self._spec.role,
                round_label=f"auxiliary {self._spec.role}",
                invocation_id=invocation_id,
            )

    def _invoke(
        self,
        message: str,
        system_prompt: str,
        *,
        invocation_id: str | None,
    ) -> str:
        return self._client.invoke_text(
            kind=self._spec.role,
            workspace=self._spec.workspace,
            system_prompt=system_prompt,
            env=dict(self._spec.environment),
            user_prompt=message,
            round_label=f"auxiliary {self._spec.role}",
            invocation_id=invocation_id,
            progress=None,
            reuse_session=True,
            session_key=self._session_key,
            tool_servers=list(self._spec.tool_servers),
        )

    def close(self) -> None:
        """Wait for an active turn, then close transferred resources in reverse.

        A resource whose close raised is closed again on the next call.
        """
        with self._lock:
            self._close.run(self._release)

    def _release(self) -> Released:
        return self._resources.close()


def create_managed_conversation(
    client: AgentClientProtocol,
    spec: ManagedConversationSpec,
    *,
    resources: tuple[_Closeable, ...],
    threads: Threads,
    startup: ToolServerStartupCheck | None = None,
) -> ManagedConversation:
    """Transfer *resources* after validating and creating one conversation.

    If construction raises, ownership remains with the caller.  After a
    successful return, the conversation closes every resource in reverse order.
    ``threads`` makes the lock that serializes its turns. ``startup``, when given, is checked
    before each turn until it finds no failure.
    """
    return _RuntimeManagedConversation(client, spec, resources, threads, startup)


def _container_tool_check(
    spec: ManagedConversationSpec,
    environment: AgentExecutionEnvironment,
    events: AgentEventSink,
) -> ToolServerStartupCheck | None:
    """The startup checks of *spec*'s tool servers, run in the agent container; ``None`` off one.

    Absolute paths in a check are not translated: the container interpreter and the
    framework mount are the same on both sides.
    """
    backends = environment.backends
    if not spec.tool_server_checks or not environment.use_docker or not backends:
        return None
    (runner,) = backends.values()

    def check() -> tuple[ToolServerFailure, ...]:
        return check_tool_servers(runner, spec.tool_server_checks, agent_path=str)

    return ToolServerStartupCheck(check, events)


def open_managed_conversation(  # noqa: PLR0913  # lint-waiver: LW-994218 [PLR0913]; this lower composition seam binds independently resolved agent, environment, logging, event, and conversation inputs once.
    spec: ManagedConversationSpec,
    *,
    agent_spec: AgentSpec,
    environment: AgentExecutionEnvironment,
    log_directory: Path,
    agent_events: AgentEventSink,
    additional_host_resources: tuple[HostResource, ...] = (),
    client_factory: Callable[..., AgentClientProtocol],
    agent_homes_directory: Path | None = None,
    clock: Clock,
    threads: Threads,
) -> ManagedConversation:
    """Build one auxiliary conversation and own all transferred resources.

    Calling this function transfers ``environment`` immediately. Construction
    failures close every acquired resource and preserve the construction error;
    a successful conversation owns the environment, logger, and client until
    its idempotent ``close``. ``clock`` counts seconds since the epoch; it names the
    conversation's log file and times and stamps its turns; ``threads`` serializes them.
    """
    ownership = ExitStack()
    ownership.callback(environment.close)
    try:
        logger = RunLogger(log_directory, clock=clock, tee_stderr=False)
        ownership.callback(logger.close)
        logger.switch(f"auxiliary-{spec.role}")
        client = client_factory(
            spec=agent_spec,
            backends=environment.backends,
            skill_source_dirs=list(environment.skill_source_dirs),
            skill_selection=environment.skill_selection,
            run_log_file=logger.writer,
            use_docker=environment.use_docker,
            log_dir=log_directory,
            agent_homes_dir=agent_homes_directory,
            clock=clock,
            project_path_policy=environment.project_path_policy,
            require_host_sandbox=not environment.use_docker,
            host_resources=(*environment.host_resources, *additional_host_resources),
            events=agent_events,
        )
        ownership.callback(client.close)
        conversation = create_managed_conversation(
            client,
            spec,
            resources=(environment, logger, client),
            threads=threads,
            startup=_container_tool_check(spec, environment, agent_events),
        )
    except BaseException as construction_error:
        try:
            ownership.close()
        except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-994219 [BLE001]; cleanup must preserve the construction failure while reporting teardown failure.
            construction_error.add_note(
                "Additional error while cleaning up managed-conversation construction: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        raise
    else:
        ownership.pop_all()
        return conversation


__all__ = [
    "ManagedConversation",
    "ManagedConversationSpec",
    "create_managed_conversation",
    "open_managed_conversation",
]
