"""Private product session implementation."""

from __future__ import annotations

import threading
from contextlib import ExitStack
from typing import TYPE_CHECKING, cast

from vibesys.api.auxiliary import (
    AgentDriver,
    AuxiliaryAgentDriver,
    AuxiliaryAgentLaunch,
    ManagedAgent,
    RunReady,
)
from vibesys.api.contracts import RunResult, RunStatus
from vibesys.api.store import open_run_store
from vibesys.composition import AGENT_TOOL_BINDINGS, agent_spec_from_config
from vibesys.events import CoreEventType, EventStatus, RunStartedData
from vibesys.plugin_catalog import project_run
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration, RunResources
from vibesys.run.skills import platform_skill_selection
from vs_agent.api import (
    ToolServerDescriptor,
    agent_catalog,
    expose_as_tools,
)
from vs_project.api import Project
from vs_runtime.api.infrastructure import (
    ManagedConversationSpec,
    ScopedAgentEnvironment,
    open_agent_execution_environment,
    open_managed_conversation,
)
from vs_sandbox.api import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vibesys.api.contracts import EventSink, RunView
    from vibesys.plugin_catalog import OrchestrationProjector, OrchestrationRegistry
    from vibesys.run.contracts import RunRequest
    from vs_agent.api import AgentClientProtocol
    from vs_runtime.api import OrchestrationPlugin, Workspace
    from vs_runtime.api import RunStatus as PluginRunStatus
    from vs_runtime.api.infrastructure import AgentExecutionEnvironment
    from vs_sandbox.api import ComputeBackendImpl


def _create_session(
    request: RunRequest,
    *,
    sink: EventSink,
    registry: OrchestrationRegistry | None = None,
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
) -> _LocalRunSession:
    """Compose the product session with optional test-owned effect factories."""
    return _LocalRunSession(
        request,
        sink=sink,
        registry=registry,
        agent_client_factory=agent_client_factory,
        backend_factory=backend_factory,
    )


async def run_plugin(  # noqa: PLR0913  # lint-waiver: LW-040002 [PLR0913]; the product composition boundary binds independently owned runtime effects once.
    request: RunRequest,
    integration: LocalRunIntegration,
    plugin: OrchestrationPlugin,
    options: BaseModel,
    *,
    open_agent_environment: Callable[..., AgentExecutionEnvironment] | None = None,
    projector: OrchestrationProjector | None = None,
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
    agent_tool_bindings: Mapping[
        str, Callable[[object, Workspace], tuple[ToolServerDescriptor, ...]]
    ]
    | None = None,
) -> PluginRunStatus:
    """Compose the private runtime host and invoke one validated plugin."""
    async with open_product_run_host(
        request,
        integration,
        open_agent_environment=open_agent_environment,
        projector=projector,
        agent_client_factory=agent_client_factory,
        backend_factory=backend_factory,
        agent_tool_bindings=agent_tool_bindings,
        plugin=plugin,
    ) as host:
        return await plugin.orchestrate(host, options)


class _LocalRunSession:
    """`RunSession` that runs one loop function in-process via `asyncio.to_thread`.

    `RunControl` methods write to `self._integration.control`, the same
    `vs_runtime.api.infrastructure.RunControlChannel` consumed by run boundaries
    and agent turns in `vibesys.orchestration.runtime`.
    """

    def __init__(
        self,
        request: RunRequest,
        *,
        sink: EventSink,
        registry: OrchestrationRegistry | None,
        agent_client_factory: Callable[..., AgentClientProtocol] | None,
        backend_factory: Callable[..., ComputeBackendImpl] | None,
    ) -> None:
        self._request = request
        self._sink = sink
        if registry is None:
            # lint-waiver: LW-020004 [PLC0415]; the product catalog imports every built-in policy, so it loads only when a caller needs it.
            from vibesys.plugin_catalog import built_in_orchestrations  # noqa: PLC0415

            registry = built_in_orchestrations()
        self._registry = registry
        self._registration = self._registry.resolve(request.orchestration.id)
        # Descriptor validation precedes integration and run resource setup.
        self._plugin_options = self._registration.parse_options(request.orchestration)
        self._agent_client_factory = agent_client_factory
        self._backend_factory = backend_factory
        self._integration = LocalRunIntegration()
        self._integration.add_committed_state_listener(self._handle_committed_state)
        self._integration.add_resource_listener(self._handle_resources)
        self._committed_view_listener: Callable[[RunView, tuple[str, ...] | None], None] | None = (
            None
        )
        self._ready_listener: Callable[[RunReady], None] | None = None
        self._resources: RunResources | None = None
        self._auxiliary_agents: list[ManagedAgent] = []
        self._auxiliary_lock = threading.Lock()
        self._closed = False
        self._unsubscribe: Callable[[], None] | None = None
        # A session exists to run its request, so it reads as active from
        # construction (before `start()`/`await_result()`) through to
        # `_run_sync` recording its terminal outcome below.
        self._status: RunStatus = RunStatus.ACTIVE

    def on_committed_view(
        self, listener: Callable[[RunView, tuple[str, ...] | None], None]
    ) -> None:
        """Register a listener for policy views and policy-defined changed keys."""
        self._committed_view_listener = listener

    def _handle_committed_state(
        self,
        namespace: str,
        state: BaseModel,
        changed_keys: tuple[str, ...] | None,
    ) -> None:
        if self._committed_view_listener is None:
            return
        projector = self._registration.projector
        if projector is None:
            return
        view = projector.project_committed(namespace, state, run_id=self._run_id())
        if view is not None:
            self._committed_view_listener(view, changed_keys)

    def on_ready(self, listener: Callable[[RunReady], None]) -> None:
        """Register the sole frontend listener for this run's readiness facts."""
        self._ready_listener = listener

    def _handle_resources(self, resources: RunResources) -> None:
        self._resources = resources
        if self._ready_listener is not None:
            self._ready_listener(_run_ready(resources, self._registry))

    def _run_id(self) -> str:
        """Use the provisioned ID once a custom runtime has created its run."""
        if self._resources is not None:
            return self._resources.project_resources.state.run_id
        return self._request.resolved_run_id

    def _open_agent_environment(
        self,
        *,
        mounts: tuple[HostResource, ...] = (),
        agent_backend: str | None = None,
        cli_provider: str | None = None,
    ) -> ScopedAgentEnvironment:
        """Bind product facts to the runtime-owned scoped environment lifecycle."""
        resources = self._resources
        if resources is None:
            message = "the run is not ready to create agents"
            raise RuntimeError(message)
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

    def create_auxiliary_agent(self, launch: AuxiliaryAgentLaunch) -> ManagedAgent:
        """Create a fresh frontend-owned conversation over this run's resources.

        Construction stays under the session lock so ``close`` either precedes
        it and rejects the request or follows it and owns the completed agent.
        """
        with self._auxiliary_lock:
            if self._closed:
                message = "run session is closed"
                raise RuntimeError(message)
            resources = self._resources
            if resources is None:
                message = "the run is not ready to create auxiliary agents"
                raise RuntimeError(message)
            environment = resources.environment_resources
            missing = tuple(item.path for item in launch.readable_inputs if not item.path.exists())
            if missing:
                message = f"auxiliary agent readable path does not exist: {missing[0]}"
                raise FileNotFoundError(message)
            readable_resources = tuple(
                HostResource(
                    item.path,
                    HostResourceAccess.READ_ONLY,
                    item.purpose,
                )
                for item in launch.readable_inputs
            )
            with ExitStack() as pending_environment:
                opened = self._open_agent_environment(
                    mounts=readable_resources,
                    agent_backend=resources.agent_backend,
                    cli_provider=launch.provider,
                )
                pending_environment.callback(opened.close)
                spec = agent_spec_from_config(
                    resources.config,
                    backend=resources.agent_backend,
                    driver=launch.driver,
                    provider=launch.provider,
                    model=launch.model,
                )
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
                agent = open_managed_conversation(
                    conversation_spec,
                    agent_spec=spec,
                    environment=opened,
                    log_directory=environment.request.log_dir,
                    agent_events=self._integration.agent_events,
                    additional_host_resources=readable_resources,
                )
            self._auxiliary_agents.append(agent)
            return agent

    def start(self) -> None:
        """Subscribe `sink` to this run's event stream."""
        self._unsubscribe = self._integration.events.subscribe(self._sink)

    async def await_result(self) -> RunResult:
        """Run the selected policy and return its terminal outcome."""
        return await self._run()

    async def _run(self) -> RunResult:
        request = self._request
        plugin = self._registration.plugin
        options = self._plugin_options
        max_rounds = (
            plugin.project_max_rounds(options) if plugin.project_max_rounds is not None else None
        )
        try:
            self._integration.events.emit(
                CoreEventType.RUN_STARTED,
                status=EventStatus.ACTIVE,
                data=RunStartedData(
                    outer_loop=request.orchestration.id,
                    input=str(request.input_bundle.root),
                    max_rounds=max_rounds,
                    expected_roles=tuple(role.id for role in plugin.agents),
                ),
            )
            outcome = await run_plugin(
                request,
                self._integration,
                plugin,
                options,
                open_agent_environment=self._open_agent_environment,
                projector=self._registration.projector,
                agent_client_factory=self._agent_client_factory,
                backend_factory=self._backend_factory,
                agent_tool_bindings=AGENT_TOOL_BINDINGS,
            )
            succeeded = outcome.value == "succeeded"
        except BaseException as exc:
            self._status = RunStatus.FAILED
            self._integration.events.emit(
                CoreEventType.RUN_FAILED,
                f"{type(exc).__name__}: {exc}",
                status=EventStatus.FAILED,
            )
            raise
        else:
            self._status = RunStatus.COMPLETED if succeeded else RunStatus.FAILED
            self._integration.events.emit(
                CoreEventType.RUN_FINISHED if succeeded else CoreEventType.RUN_FAILED,
                status=EventStatus.COMPLETED if succeeded else EventStatus.FAILED,
            )
            return RunResult(
                run_id=self._run_id(),
                loop=request.orchestration.id,
                succeeded=succeeded,
            )
        finally:
            if self._unsubscribe is not None:
                self._unsubscribe()
            self._integration.close()

    def view(self) -> RunView:
        """Project this session's live run into a read-only `RunView`.

        Reopens `Project`/agent state on every call rather than caching: this
        session has no subscription to its own run's writes, so a cache could
        only go stale. `status` reflects `_run_sync`'s own progress
        (`ACTIVE` until it returns or raises), not a re-derivation from state.
        """
        return project_run(
            self._registration,
            Project.open(self._request.project_root),
            run_id=self._run_id(),
            status=self._status,
            loop=self._request.orchestration.id,
        )

    def steer(self, text: str) -> None:
        """Send free-text steering input to the active run."""
        self._integration.control.queue_steer(text)

    def pause(self) -> None:
        """Request the run reach its next safe boundary and release the write lease."""
        self._integration.control.request_pause()

    def resume(self) -> None:
        """Request the run acquire the write lease and continue from checkpoint."""
        self._integration.control.resume()

    def stop(self) -> None:
        """Request the run terminate."""
        self._integration.control.request_stop()

    def close(self) -> None:
        """Close remaining auxiliary agents in reverse creation order."""
        with self._auxiliary_lock:
            if self._closed:
                return
            self._closed = True
            agents, self._auxiliary_agents = self._auxiliary_agents, []
        first_error: BaseException | None = None
        for agent in reversed(agents):
            try:
                agent.close()
            except BaseException as exc:  # noqa: BLE001  # lint-waiver: LW-948025 [BLE001]; session teardown must attempt every owned auxiliary agent even if one cleanup fails.
                first_error = first_error or exc
        if first_error is not None:
            raise first_error


def _run_ready(
    resources: RunResources,
    registry: OrchestrationRegistry,
) -> RunReady:
    """Project private resource facts to the narrow frontend contract."""
    project = resources.project_resources
    environment = resources.environment_resources
    return RunReady(
        record=open_run_store(project.project, registry=registry).get_record(project.state.run_id),
        log_directory=environment.request.log_dir,
        frontend_state_directory=project.project.state.local_namespace(
            project.state.run_id, "server"
        ).external_directory(),
        agent_driver=cast("AgentDriver", resources.driver),
        agent_provider=resources.provider,
        agent_model=resources.model,
        agent_drivers=tuple(
            AuxiliaryAgentDriver(
                driver=info.driver.value,
                providers=info.providers,
            )
            for info in agent_catalog().values()
        ),
        role_models=resources.role_models,
    )


def _investigation_tools(
    environment: ScopedAgentEnvironment,
    resources: RunResources,
) -> tuple[ToolServerDescriptor, ...]:
    """Project one runtime environment into the product's run-history tool."""
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
