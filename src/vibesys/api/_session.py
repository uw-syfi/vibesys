"""Private product session implementation."""

from __future__ import annotations

import threading
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
from vibesys.composition import resolve_agent_specs
from vibesys.events import CoreEventType, EventStatus, RunStartedData
from vibesys.plugin_catalog import project_run
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration, RunResources
from vibesys.run.profilers import validate_run_request
from vs_project.api import Project

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vibesys.api.assembly import SessionAgents, SessionImplementations
    from vibesys.api.auxiliary import AuxiliaryAgents
    from vibesys.api.contracts import EventSink, RunView
    from vibesys.plugin_catalog import OrchestrationProjector, OrchestrationRegistry
    from vibesys.run.contracts import RunRequest
    from vs_agent.api import (
        AgentClientProtocol,
        AgentEventSink,
        AgentInvocationStore,
        AgentSessionKey,
        ToolServerDescriptor,
    )
    from vs_project.api import OrchestrationDescriptor
    from vs_runtime.api import (
        AgentToolBindingContext,
        OrchestrationPlugin,
        OrchestrationResumeDecision,
    )
    from vs_runtime.api import RunStatus as PluginRunStatus
    from vs_runtime.api.infrastructure import (
        AgentExecutionEnvironment,
        RunState,
        ScopedAgentEnvironment,
        StopTimer,
    )
    from vs_sandbox.api import ComputeBackendImpl, HostResource


def _create_session(
    request: RunRequest,
    *,
    sink: EventSink,
    registry: OrchestrationRegistry,
    implementations: SessionImplementations,
) -> _LocalRunSession:
    """Compose a session from an explicit catalog and implementations."""
    return _LocalRunSession(request, sink=sink, registry=registry, implementations=implementations)


async def run_plugin(  # noqa: PLR0913  # lint-waiver: LW-040002 [PLR0913]; the product composition boundary binds independently owned runtime implementations once.
    request: RunRequest,
    integration: LocalRunIntegration,
    plugin: OrchestrationPlugin,
    options: BaseModel,
    *,
    open_agent_environment: Callable[..., AgentExecutionEnvironment],
    projector: OrchestrationProjector | None = None,
    resume_policy: (
        Callable[
            [OrchestrationDescriptor, OrchestrationDescriptor],
            OrchestrationResumeDecision,
        ]
        | None
    ) = None,
    agent_client_factory: Callable[..., AgentClientProtocol],
    backend_factory: Callable[..., ComputeBackendImpl],
    agent_tool_bindings: Mapping[
        str, Callable[[object, AgentToolBindingContext], tuple[ToolServerDescriptor, ...]]
    ]
    | None = None,
    stop_timer: StopTimer,
    invocation_store_factory: Callable[[RunState, AgentSessionKey], AgentInvocationStore],
) -> PluginRunStatus:
    """Compose the private runtime host and invoke one validated plugin."""
    async with open_product_run_host(
        request,
        integration,
        open_agent_environment=open_agent_environment,
        projector=projector,
        resume_policy=resume_policy,
        agent_client_factory=agent_client_factory,
        backend_factory=backend_factory,
        agent_tool_bindings=agent_tool_bindings,
        plugin=plugin,
        stop_timer=stop_timer,
        invocation_store_factory=invocation_store_factory,
    ) as host:
        return await plugin.orchestrate(host, options)


class _LocalRunSession:
    """`RunSession` that awaits one selected policy in-process.

    `RunControl` methods write to `self._integration.control`, the same
    `vs_runtime.api.infrastructure.RunControlChannel` consumed by runtime run
    boundaries and agent turns.
    """

    def __init__(
        self,
        request: RunRequest,
        *,
        sink: EventSink,
        registry: OrchestrationRegistry,
        implementations: SessionImplementations,
    ) -> None:
        validate_run_request(request)
        self._request = request
        self._implementations = implementations
        self._sink = sink
        self._registry = registry
        self._registration = self._registry.resolve(request.orchestration.id)
        # Descriptor validation precedes integration and run resource setup.
        self._plugin_options = self._registration.parse_options(request.orchestration)
        resolve_agent_specs(
            request.config,
            self._registration.plugin.agents,
            backend=request.agent_backend,
            provider=request.cli_provider,
        )
        self._integration = LocalRunIntegration()
        self._integration.add_committed_state_listener(self._handle_committed_state)
        self._integration.add_resource_listener(self._handle_resources)
        self._committed_view_listener: Callable[[RunView, tuple[str, ...] | None], None] | None = (
            None
        )
        self._ready_listener: Callable[[RunReady], None] | None = None
        self._resources: RunResources | None = None
        self._auxiliary_scope: InProcessAuxiliaryAgents | None = None
        self._auxiliary_lock = threading.Lock()
        self._closed = False
        self._unsubscribe: Callable[[], None] | None = None
        # A session exists to run its request, so it reads as active from
        # construction (before `start()`/`await_result()`) through to
        # `_run` recording its terminal outcome below.
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
            self._ready_listener(
                _run_ready(resources, self._registry, self._implementations.agent_drivers)
            )

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
        return self._implementations.agents.open_environment(
            resources,
            mounts=mounts,
            agent_backend=agent_backend,
            cli_provider=cli_provider,
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
            if self._auxiliary_scope is None:
                self._auxiliary_scope = self._new_auxiliary_scope(resources)
            return self._auxiliary_scope.create_auxiliary_agent(launch)

    def open_auxiliary_agents(self) -> AuxiliaryAgents:
        """Transfer an independent auxiliary scope to the caller."""
        with self._auxiliary_lock:
            if self._closed:
                message = "run session is closed"
                raise RuntimeError(message)
            resources = self._resources
            if resources is None:
                message = "the run is not ready to create auxiliary agents"
                raise RuntimeError(message)
            return self._new_auxiliary_scope(resources)

    def _new_auxiliary_scope(self, resources: RunResources) -> InProcessAuxiliaryAgents:
        return InProcessAuxiliaryAgents(
            resources, self._implementations.agents, self._integration.agent_events
        )

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
        project_max_rounds = self._registration.project_max_rounds
        max_rounds = project_max_rounds(options) if project_max_rounds is not None else None
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
                resume_policy=self._registration.resume_policy,
                agent_client_factory=self._implementations.agent_client_factory,
                backend_factory=self._implementations.backend_factory,
                agent_tool_bindings=self._implementations.agent_tool_bindings,
                stop_timer=self._implementations.stop_timer,
                invocation_store_factory=self._implementations.invocation_store_factory,
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
        only go stale. `status` reflects `_run`'s own progress
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
        """Close the run-owned auxiliary scope, preserving transferred scopes."""
        with self._auxiliary_lock:
            if self._closed:
                return
            self._closed = True
            scope, self._auxiliary_scope = self._auxiliary_scope, None
        if scope is not None:
            scope.close()


class InProcessAuxiliaryAgents:
    """Own conversations independently of the originating session's task."""

    def __init__(
        self,
        resources: RunResources,
        agents: SessionAgents,
        events: AgentEventSink,
    ) -> None:
        self._resources = resources
        self._agents = agents
        self._events = events
        self._conversations: list[ManagedAgent] = []
        self._lock = threading.Lock()
        self._closed = False

    def create_auxiliary_agent(self, launch: AuxiliaryAgentLaunch) -> ManagedAgent:
        """Create and own one conversation unless the scope has closed."""
        with self._lock:
            if self._closed:
                message = "auxiliary agent scope is closed"
                raise RuntimeError(message)
            agent = self._agents.create_agent(launch, self._resources, self._events)
            self._conversations.append(agent)
            return agent

    def close(self) -> None:
        """Close every owned conversation in reverse construction order."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            agents, self._conversations = self._conversations, []
        first_error: BaseException | None = None
        for agent in reversed(agents):
            try:
                agent.close()
            except BaseException as exc:  # noqa: BLE001  # lint-waiver: LW-949001 [BLE001]; scope teardown must attempt every owned auxiliary agent even if one cleanup fails.
                first_error = first_error or exc
        if first_error is not None:
            raise first_error


def _run_ready(
    resources: RunResources,
    registry: OrchestrationRegistry,
    agent_drivers: tuple[AuxiliaryAgentDriver, ...],
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
        agent_drivers=agent_drivers,
        role_models=resources.role_models,
    )
