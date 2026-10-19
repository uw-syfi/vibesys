"""Built-in scoped agent construction selected by application wiring."""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from launch.chat_tools import build_tools
from launch.scripted_agents import ScriptedDynamicAgents
from vibesys.api import AuxiliarySkills, agent_spec_from_config
from vibesys.api.store import open_run_store
from vibesys.api.wiring import agent_tool_socket, chat_skill_dirs, platform_skill_selection
from vibesys.orchestration.dynamic.scripted.api import CHAT_REPLY
from vs_agent.api import AgentBackend, ScriptedAgentClient, ScriptedClientSetup, build_agent_client
from vs_evaluation.api.tool_relay import (
    ToolRelayService,
    relay_check_descriptor,
    relay_mcp_descriptor,
)
from vs_runtime.api.infrastructure import (
    ManagedConversationSpec,
    open_agent_execution_environment,
    open_managed_conversation,
)
from vs_sandbox.api import HostResource, HostResourceAccess
from vs_sim.api import ReleaseStack

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.api import AuxiliaryAgentLaunch, ManagedAgent, OrchestrationRegistry
    from vibesys.api.wiring import AuxiliaryResources
    from vs_agent.api import (
        AgentClientProtocol,
        AgentEventSink,
        AgentSpec,
        SessionStore,
    )
    from vs_mcp.api import ToolServerDescriptor
    from vs_project.api import Git
    from vs_runtime.api.infrastructure import ScopedAgentEnvironment
    from vs_sim.api import Network, Threads

CHAT_TOOL_SERVER = "vibesys-run"
"""The name the chat agent sees its run tools under."""


@dataclass(frozen=True, slots=True)
class BuiltInSessionAgents:
    """Construct run-scoped environments and conversations with built-in tools.

    ``network`` serves the tool relay a chat agent reaches its run tools through, and
    ``git`` and ``registry`` open the run's read-model, which those tools read.
    """

    client_factory: Callable[..., AgentClientProtocol]
    network: Network
    git: Git
    registry: OrchestrationRegistry

    def open_environment(
        self,
        resources: AuxiliaryResources,
        *,
        mounts: tuple[HostResource, ...] = (),
        agent_backend: str | None = None,
        cli_provider: str | None = None,
        skills: AuxiliarySkills = AuxiliarySkills.RUN,
    ) -> ScopedAgentEnvironment:
        """Bind product skill policy to the runtime environment lifecycle.

        ``skills`` names the skill set to install: the run's own, or the chat agent's.
        """
        return open_agent_execution_environment(
            resources.environment_request,
            None,
            share_session=False,
            skill_selection=platform_skill_selection(resources.backend),
            skill_source_dirs=(
                chat_skill_dirs() if skills is AuxiliarySkills.CHAT else resources.skill_source_dirs
            ),
            host_resources=resources.host_resources,
            mounts=mounts,
            agent_backend=agent_backend,
            cli_provider=cli_provider,
            open_session=resources.open_environment_session,
        )

    def create_agent(
        self,
        launch: AuxiliaryAgentLaunch,
        resources: AuxiliaryResources,
        agent_events: AgentEventSink,
    ) -> ManagedAgent:
        """Open a conversation, closing provisional resources on failure."""
        environment = resources.environment_request
        missing = tuple(item.path for item in launch.readable_inputs if not item.path.exists())
        if missing:
            message = f"auxiliary agent readable path does not exist: {missing[0]}"
            raise FileNotFoundError(message)
        readable_resources = tuple(
            HostResource(item.path, HostResourceAccess.READ_ONLY, item.purpose)
            for item in launch.readable_inputs
        )
        spec = agent_spec_from_config(
            resources.config,
            backend=resources.agent_backend,
            provider=launch.provider,
            model=launch.model,
            allow_unvetted_model=resources.allow_unvetted_model,
        )
        with ExitStack() as owned:
            relay = self._serve_run_tools(resources)
            owned.callback(relay.service.close)
            with ExitStack() as pending_environment:
                opened = self.open_environment(
                    resources,
                    mounts=readable_resources,
                    agent_backend=resources.agent_backend,
                    cli_provider=launch.provider,
                    skills=launch.skills,
                )
                pending_environment.callback(opened.close)
                conversation_spec = ManagedConversationSpec(
                    role=launch.role,
                    member_id=launch.member_id,
                    workspace=environment.workspace,
                    system_prompt=launch.system_prompt,
                    continuation_prompt=launch.continuation_prompt,
                    tool_servers=(relay.server,),
                    tool_server_checks=(relay.check,),
                    environment=tuple(
                        (item.environment_variable, opened.agent_path(item.path))
                        for item in launch.readable_inputs
                    ),
                )
                pending_environment.pop_all()
                agent = _ServedAgent(
                    open_managed_conversation(
                        conversation_spec,
                        agent_spec=spec,
                        environment=opened,
                        log_directory=environment.log_dir,
                        agent_homes_directory=environment.agent_homes_dir,
                        agent_events=agent_events,
                        additional_host_resources=readable_resources,
                        client_factory=self.client_factory,
                        clock=resources.clock,
                        threads=resources.threads,
                    ),
                    relay.service,
                )
            owned.pop_all()
            return agent

    def _serve_run_tools(self, resources: AuxiliaryResources) -> _RunTools:
        """Serve the run's read-model to one conversation, on the host, behind a private token.

        The tools read the run's state, which no agent container may see
        (``docs/contributing/sandboxing.md``, "Run state is part of the trust boundary"), so they
        run here and the agent's tool server only forwards to them. The socket is in the run's
        private directory, which every agent container mounts; the token is issued to this
        conversation's server alone, so another agent of the run cannot read through it.
        """
        store = open_run_store(
            resources.project,
            registry=self.registry,
            git=self.git,
            threads=resources.threads,
        )
        socket_path = agent_tool_socket(resources, f"chat-{uuid.uuid4().hex[:8]}.sock")
        token = uuid.uuid4().hex
        service = ToolRelayService(
            build_tools(store, resources.run_id),
            socket_path,
            token=token,
            network=self.network,
            threads=resources.threads,
            clock=resources.clock,
        )
        service.start()
        server, check = run_tool_descriptors(str(socket_path), token)
        return _RunTools(service, server, check)


def run_tool_descriptors(
    socket_path: str, token: str
) -> tuple[ToolServerDescriptor, ToolServerDescriptor]:
    """The chat agent's run tool server and its startup check, for a relay at ``socket_path``."""
    return (
        relay_mcp_descriptor(CHAT_TOOL_SERVER, socket_path, token),
        relay_check_descriptor(CHAT_TOOL_SERVER, socket_path),
    )


@dataclass(frozen=True, slots=True)
class _RunTools:
    """The host service of one conversation's run tools, its agent-side server and its check."""

    service: ToolRelayService
    server: ToolServerDescriptor
    check: ToolServerDescriptor


class _ServedAgent:
    """A conversation that also owns the host service its tool server forwards to."""

    def __init__(self, conversation: ManagedAgent, service: ToolRelayService) -> None:
        self._conversation = conversation
        self._owned = ReleaseStack()
        self._owned.callback(service.close)
        self._owned.callback(conversation.close)

    def turn(self, message: str, *, invocation_id: str | None = None) -> str:
        """Send one follow-on message in the conversation."""
        return self._conversation.turn(message, invocation_id=invocation_id)

    def close(self) -> None:
        """Close the conversation, then the service it read through; a failed close runs again."""
        self._owned.close()


def scripted_agent_client(
    *,
    spec: AgentSpec,
    threads: Threads,
    events: AgentEventSink,
    session_store: SessionStore | None = None,
    log_dir: Path | None = None,
    **_unused: object,
) -> AgentClientProtocol:
    """The client of an agent-less run: scripted agents answer the dynamic search's roles.

    The services a real client would use to start a provider (sandboxes, readiness probes) are
    not needed, so they are not read: ``_unused`` takes what the run host passes.
    """
    setup = ScriptedClientSetup(
        responders=ScriptedDynamicAgents(seed=spec.scripted_seed, sink=events).responders(),
        chat=CHAT_REPLY,
        provider=spec.provider,
        model=spec.model,
        event_sink=events,
        session_store=session_store,
        log_dir=log_dir,
    )
    return ScriptedAgentClient(setup, threads=threads)


def default_agent_client_factory(*, threads: Threads) -> Callable[..., AgentClientProtocol]:
    """Build the client for the spec's backend: a provider CLI, or scripted agents."""
    build = partial(build_agent_client, threads=threads)
    scripted = partial(scripted_agent_client, threads=threads)

    def factory(*, spec: AgentSpec, **services: Any) -> AgentClientProtocol:  # noqa: ANN401  # lint-waiver: LW-482909 [ANN401]; the services are the keyword set the run host passes to every backend, and each backend reads its own subset.
        if spec.backend is AgentBackend.SCRIPTED:
            return scripted(spec=spec, **services)
        return build(spec=spec, **services)

    return factory


__all__ = ["BuiltInSessionAgents", "default_agent_client_factory"]
