"""Private product session implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api.auxiliary import (
    AuxiliaryAgentLaunch,
    ManagedAgent,
    RunReady,
)
from vibesys.api.contracts import RunResult, RunStatus
from vibesys.api.store import open_run_store
from vibesys.composition import resolve_agent_specs
from vibesys.events import CoreEventType, EventStatus, RunFailedData, RunStartedData
from vibesys.orchestration.agent_options import attribution_command_of, objective_axes_of
from vibesys.plugin_catalog import project_run
from vibesys.run.core_run import (
    OperatorPaused,
    OperatorStopped,
    RunEnd,
    drive_core_run,
    ensure_resumable,
)
from vibesys.run.host import open_product_core_host
from vibesys.run.integration import AuxiliaryResources, LocalRunIntegration, RunResources
from vibesys.run.overview import overview_of
from vibesys.run.profilers import (
    ProfilerPreflight,
    validate_attribution_command,
    validate_run_request,
)
from vs_core.api import StopMode
from vs_project.api import Project
from vs_runtime.api.infrastructure import RunStopped
from vs_sim.api import CloseOnce, ReleaseStack

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vibesys.api.assembly import SessionAgents, SessionImplementations
    from vibesys.api.auxiliary import AuxiliaryAgents
    from vibesys.api.contracts import EventSink, RunView
    from vibesys.plugin_catalog import OrchestrationProjector, OrchestrationRegistry
    from vibesys.run.contracts import RunRequest
    from vibesys.run.host import StatelessToolServer
    from vs_agent.api import (
        AgentClientProtocol,
        AgentEventSink,
        AgentInvocationStore,
        AgentSessionKey,
    )
    from vs_project.api import OrchestrationDescriptor
    from vs_runtime.api import (
        OrchestrationPlugin,
        OrchestrationResumeDecision,
    )
    from vs_runtime.api.core import RunTiming
    from vs_runtime.api.infrastructure import (
        AgentExecutionEnvironment,
        ControlAck,
        ControlRequest,
        RunState,
        ScopedAgentEnvironment,
        StopTimer,
    )
    from vs_sandbox.api import ComputeBackendImpl, HostResource
    from vs_sim.api import BlockingRunner, Network, Released, Threads


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
    open_agent_environment: Callable[..., AgentExecutionEnvironment] | None = None,
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
    profiler_preflight: ProfilerPreflight,
    stateless_tool_servers: Mapping[str, StatelessToolServer] | None = None,
    stop_timer: StopTimer,
    timing: RunTiming,
    blocking: BlockingRunner,
    network: Network,
    invocation_store_factory: Callable[[RunState, AgentSessionKey], AgentInvocationStore],
) -> RunEnd:
    """Compose the private runtime host and run one validated plugin.

    The plugin's ``core`` policy is driven by the runtime's core run loop over services
    built from the run's resources.
    """
    if request.resume is not None:
        ensure_resumable(request.project_root, request.resume.run_id)
    async with open_product_core_host(
        request,
        integration,
        open_agent_environment=open_agent_environment,
        projector=projector,
        resume_policy=resume_policy,
        agent_client_factory=agent_client_factory,
        backend_factory=backend_factory,
        profiler_preflight=profiler_preflight,
        stateless_tool_servers=stateless_tool_servers,
        plugin=plugin,
        options=options,
        stop_timer=stop_timer,
        timing=timing,
        blocking=blocking,
        network=network,
        invocation_store_factory=invocation_store_factory,
    ) as core_host:
        return await drive_core_run(core_host, integration)


class _LocalRunSession:
    """`RunSession` that awaits one selected policy in-process.

    `RunControl` methods go through one `ChannelRunControl` over
    `self._integration.control`, the same `RunControlChannel` consumed by runtime
    run boundaries and agent turns.
    """

    def __init__(
        self,
        request: RunRequest,
        *,
        sink: EventSink,
        registry: OrchestrationRegistry,
        implementations: SessionImplementations,
    ) -> None:
        validate_run_request(request, git=implementations.git)
        self._request = request
        self._implementations = implementations
        self._sink = sink
        self._registry = registry
        self._registration = self._registry.resolve(request.orchestration.id)
        # Descriptor validation precedes integration and run resource setup.
        self._plugin_options = self._registration.parse_options(request.orchestration)
        validate_attribution_command(request, attribution_command_of(self._plugin_options))
        resolve_agent_specs(
            request.config,
            self._registration.plugin.agents,
            backend=request.agent_backend,
            provider=request.cli_provider,
            allow_unvetted_model=request.allow_unvetted_model,
        )
        self._integration = LocalRunIntegration(
            implementations.git_repository,
            implementations.state_stores,
            clock=implementations.timing.clock,
            git=implementations.git,
            github=implementations.github,
            threads=implementations.timing.threads,
            system=implementations.system,
        )
        self._control = self._integration.run_control
        self._integration.events.checkout = implementations.checkout
        self._integration.add_committed_state_listener(self._handle_committed_state)
        self._integration.add_resource_listener(self._handle_resources)
        self._committed_view_listener: Callable[[RunView, tuple[str, ...] | None], None] | None = (
            None
        )
        self._ready_listener: Callable[[RunReady], None] | None = None
        self._resources: RunResources | None = None
        self._auxiliary_scope: InProcessAuxiliaryAgents | None = None
        self._threads = implementations.timing.threads
        self._auxiliary_lock = self._threads.lock()
        self._close = CloseOnce()
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
                run_ready(resources.auxiliary(), self._registry, self._implementations)
            )

        request = self._request
        project_max_rounds = self._registration.project_max_rounds
        self._integration.events.emit(
            CoreEventType.RUN_STARTED,
            status=EventStatus.ACTIVE,
            data=RunStartedData(
                outer_loop=request.orchestration.id,
                input=str(request.input_bundle.root),
                max_rounds=project_max_rounds(self._plugin_options)
                if project_max_rounds is not None
                else None,
                expected_roles=tuple(role.id for role in self._registration.plugin.agents),
                overview=overview_of(request.input_bundle, objective_axes_of(self._plugin_options)),
            ),
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
            resources.auxiliary(),
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
            if self._close.started:
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
            if self._close.started:
                message = "run session is closed"
                raise RuntimeError(message)
            resources = self._resources
            if resources is None:
                message = "the run is not ready to create auxiliary agents"
                raise RuntimeError(message)
            return self._new_auxiliary_scope(resources)

    def _new_auxiliary_scope(self, resources: RunResources) -> InProcessAuxiliaryAgents:
        return InProcessAuxiliaryAgents(
            resources.auxiliary(),
            self._implementations.agents,
            self._integration.agent_events,
            self._threads,
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
        try:
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
                profiler_preflight=self._implementations.profiler_preflight,
                stateless_tool_servers=self._implementations.stateless_tool_servers,
                stop_timer=self._implementations.stop_timer,
                timing=self._implementations.timing,
                blocking=self._implementations.blocking,
                network=self._implementations.network,
                invocation_store_factory=self._implementations.invocation_store_factory,
            )
            succeeded = outcome.status.value == "succeeded"
        except OperatorPaused:
            self._status = RunStatus.PARKED
            self._integration.events.emit(CoreEventType.PARKED, "Run paused")
            return RunResult(
                run_id=self._run_id(),
                loop=request.orchestration.id,
                succeeded=False,
                status=RunStatus.PARKED,
            )
        except RunStopped as stop:
            self._status = RunStatus.STOPPED
            self._integration.events.emit(
                CoreEventType.STOPPED, "Run stopped by request", status=EventStatus.INTERRUPTED
            )
            return RunResult(
                run_id=self._run_id(),
                loop=request.orchestration.id,
                succeeded=False,
                status=RunStatus.STOPPED,
                stop_reason=str(stop) or None,
                uncompared=isinstance(stop, OperatorStopped) and stop.uncompared,
            )
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
            failure = None if succeeded else outcome.failure
            if succeeded:
                self._integration.events.emit(
                    CoreEventType.RUN_FINISHED, status=EventStatus.COMPLETED
                )
            else:
                self._integration.events.emit(
                    CoreEventType.RUN_FAILED,
                    "" if failure is None else failure.reason,
                    status=EventStatus.FAILED,
                    data=None if failure is None else RunFailedData(failure=failure),
                )
            return RunResult(
                run_id=self._run_id(),
                loop=request.orchestration.id,
                succeeded=succeeded,
                failure=failure,
                uncompared=outcome.uncompared,
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
            Project.open(self._request.project_root, state_stores=self._integration.state_stores),
            run_id=self._run_id(),
            status=self._status,
            loop=self._request.orchestration.id,
        )

    def submit(self, request: ControlRequest) -> ControlAck:
        """Apply one operator control request; every control ingress calls this."""
        return self._control.submit(request)

    def steer(self, text: str) -> None:
        """Send free-text steering input to the active run."""
        self._control.steer(text)

    def pause(self) -> None:
        """Request the run reach its next safe boundary and release the write lease."""
        self._control.pause()

    def resume(self) -> None:
        """Request the run acquire the write lease and continue from checkpoint."""
        self._control.resume()

    def resume_with_fallback(self) -> bool:
        """Resume a quota-paused run on its configured fallback; False when it is not quota-paused."""
        return self._control.resume_with_fallback()

    def resume_when_capacity_returns(self) -> bool:
        """Resume a quota-paused run once capacity returns; False when it is not quota-paused."""
        return self._control.resume_when_capacity_returns()

    def stop(self, mode: StopMode = StopMode.DRAIN) -> None:
        """Request the run terminate; the operator chooses to drain or cancel work in flight.

        Draining is the default at this edge; core and the runtime take the mode explicitly.
        """
        self._control.stop(mode)

    def close(self) -> None:
        """Close the run-owned auxiliary scope, preserving transferred scopes.

        A scope whose close raised is closed again on the next call.
        """
        with self._auxiliary_lock:
            self._close.run(self._release)

    def _release(self) -> Released:
        steps = ReleaseStack()
        if self._auxiliary_scope is not None:
            steps.callback(self._auxiliary_scope.close)
        return steps.close()


class InProcessAuxiliaryAgents:
    """Own conversations independently of the originating session's task."""

    def __init__(
        self,
        resources: AuxiliaryResources,
        agents: SessionAgents,
        events: AgentEventSink,
        threads: Threads,
    ) -> None:
        self._resources = resources
        self._agents = agents
        self._events = events
        self._conversations = ReleaseStack()
        self._lock = threads.lock()
        self._close = CloseOnce()

    def create_auxiliary_agent(self, launch: AuxiliaryAgentLaunch) -> ManagedAgent:
        """Create and own one conversation unless the scope has closed."""
        with self._lock:
            if self._close.started:
                message = "auxiliary agent scope is closed"
                raise RuntimeError(message)
            agent = self._agents.create_agent(launch, self._resources, self._events)
            self._conversations.callback(agent.close)
            return agent

    def close(self) -> None:
        """Close every owned conversation in reverse construction order.

        A conversation whose close raised is closed again on the next call.
        """
        with self._lock:
            self._close.run(self._conversations.close)


def run_ready(
    resources: AuxiliaryResources,
    registry: OrchestrationRegistry,
    implementations: SessionImplementations,
) -> RunReady:
    """Project private resource facts to the narrow frontend contract."""
    store = open_run_store(
        resources.project,
        registry=registry,
        git=implementations.git,
        threads=implementations.timing.threads,
    )
    return RunReady(
        record=store.get_record(resources.run_id),
        project_root=resources.project.root,
        log_directory=resources.environment_request.log_dir,
        frontend_state_directory=resources.project.state.local_namespace(
            resources.run_id, "server"
        ).external_directory(),
        agent_provider=resources.provider,
        agent_model=resources.model,
        agent_providers=implementations.agent_providers,
        role_models=resources.role_models,
    )
