"""Built-in scoped agent construction selected by application wiring."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.api import agent_spec_from_config
from vibesys.api.wiring import platform_skill_selection
from vs_agent.api import ToolServerDescriptor, build_agent_client, expose_as_tools
from vs_runtime.api.infrastructure import (
    ManagedConversationSpec,
    open_agent_execution_environment,
    open_managed_conversation,
)
from vs_sandbox.api import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api import AuxiliaryAgentLaunch, ManagedAgent
    from vibesys.api.wiring import RunResources
    from vs_agent.api import AgentClientProtocol, AgentEventSink
    from vs_runtime.api.infrastructure import ScopedAgentEnvironment


@dataclass(frozen=True, slots=True)
class BuiltInSessionAgents:
    """Construct run-scoped environments and conversations with built-in tools."""

    client_factory: Callable[..., AgentClientProtocol] = build_agent_client

    def open_environment(
        self,
        resources: RunResources,
        *,
        mounts: tuple[HostResource, ...] = (),
        agent_backend: str | None = None,
        cli_provider: str | None = None,
    ) -> ScopedAgentEnvironment:
        """Bind product skill policy to the runtime environment lifecycle."""
        environment = resources.environment_resources
        return open_agent_execution_environment(
            environment.request,
            environment.session,
            share_session=False,
            skill_selection=platform_skill_selection(resources.backend),
            skill_source_dirs=resources.skill_source_dirs,
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
            driver=launch.driver,
            provider=launch.provider,
            model=launch.model,
        )
        with ExitStack() as pending_environment:
            opened = self.open_environment(
                resources,
                mounts=readable_resources,
                agent_backend=resources.agent_backend,
                cli_provider=launch.provider,
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


__all__ = ["BuiltInSessionAgents"]
