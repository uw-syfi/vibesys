"""Built-in scoped agent construction selected by application wiring."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from launch.scripted_agents import ScriptedDynamicAgents
from vibesys.api import AuxiliarySkills, agent_spec_from_config
from vibesys.api.wiring import chat_skill_dirs, platform_skill_selection
from vibesys.orchestration.dynamic.scripted.api import CHAT_REPLY
from vs_agent.api import AgentBackend, ScriptedAgentClient, ScriptedClientSetup, build_agent_client
from vs_mcp.api import ToolServerDescriptor, expose_as_tools
from vs_runtime.api.infrastructure import (
    ManagedConversationSpec,
    open_agent_execution_environment,
    open_managed_conversation,
)
from vs_sandbox.api import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.api import AuxiliaryAgentLaunch, ManagedAgent
    from vibesys.api.wiring import RunResources
    from vs_agent.api import (
        AgentClientProtocol,
        AgentEventSink,
        AgentSpec,
        SessionStore,
    )
    from vs_runtime.api.infrastructure import ScopedAgentEnvironment
    from vs_sim.api import Threads


@dataclass(frozen=True, slots=True)
class BuiltInSessionAgents:
    """Construct run-scoped environments and conversations with built-in tools."""

    client_factory: Callable[..., AgentClientProtocol]

    def open_environment(
        self,
        resources: RunResources,
        *,
        mounts: tuple[HostResource, ...] = (),
        agent_backend: str | None = None,
        cli_provider: str | None = None,
        skills: AuxiliarySkills = AuxiliarySkills.RUN,
    ) -> ScopedAgentEnvironment:
        """Bind product skill policy to the runtime environment lifecycle.

        ``skills`` names the skill set to install: the run's own, or the chat agent's.
        """
        environment = resources.environment_resources
        return open_agent_execution_environment(
            environment.request,
            environment.session,
            share_session=False,
            skill_selection=platform_skill_selection(resources.backend),
            skill_source_dirs=(
                chat_skill_dirs() if skills is AuxiliarySkills.CHAT else resources.skill_source_dirs
            ),
            host_resources=resources.host_resources,
            mounts=mounts,
            agent_backend=agent_backend,
            cli_provider=cli_provider,
            open_session=environment.open_session,
        )

    def create_agent(
        self,
        launch: AuxiliaryAgentLaunch,
        resources: RunResources,
        agent_events: AgentEventSink,
    ) -> ManagedAgent:
        """Open a conversation, closing provisional resources on failure."""
        environment = resources.environment_resources
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
                workspace=environment.request.workspace,
                system_prompt=launch.system_prompt,
                continuation_prompt=launch.continuation_prompt,
                tool_servers=_investigation_tools(opened, resources),
                environment=tuple(
                    (item.environment_variable, opened.agent_path(item.path))
                    for item in launch.readable_inputs
                ),
            )
            pending_environment.pop_all()
            return open_managed_conversation(
                conversation_spec,
                agent_spec=spec,
                environment=opened,
                log_directory=environment.request.log_dir,
                agent_homes_directory=environment.request.agent_homes_dir,
                agent_events=agent_events,
                additional_host_resources=readable_resources,
                client_factory=self.client_factory,
                clock=resources.clock,
                threads=resources.threads,
            )


def _investigation_tools(
    environment: ScopedAgentEnvironment,
    resources: RunResources,
) -> tuple[ToolServerDescriptor, ...]:
    project = resources.project_resources
    return (
        expose_as_tools(
            name="vibesys-run",
            entrypoint_module="entrypoints.chat_tools_server",
            entrypoint_args=(
                "--run-id",
                project.state.run_id,
                "--project-root",
                environment.agent_path(project.project.root),
            ),
        ),
    )


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
