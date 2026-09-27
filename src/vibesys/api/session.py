"""Session contracts and the `create_session` entry point.

`RunSession` separates query and control from its lifecycle and optional
frontend-agent surface, so consumers can receive only the capability they use.
"""

from __future__ import annotations

import threading
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol, cast

from vibesys.api.auxiliary import AuxiliaryAgentLaunch, ManagedAgent, RunReady
from vibesys.api.contracts import RunResult, RunStatus
from vibesys.composition import AGENT_TOOL_BINDINGS, agent_spec_from_config
from vibesys.events import CoreEventType, EventStatus, RunStartedData
from vibesys.orchestration._common import resolved_run_id
from vibesys.orchestration.contracts import project_run
from vibesys.orchestration.environment import open_run_environment
from vibesys.orchestration.skills import platform_skill_selection
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration, RunResources
from vs_agent.api import (
    ToolServerDescriptor,
    build_agent_client,
    expose_as_tools,
)
from vs_project.api import Project, RunLogger
from vs_runtime.api.infrastructure import ManagedConversationSpec, create_managed_conversation
from vs_sandbox.api import EnvironmentBindMount, HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from pydantic import BaseModel

    from vibesys.api.contracts import EventSink, RunView
    from vibesys.config import Config
    from vibesys.orchestration.contracts import OrchestrationProjector, OrchestrationRegistry
    from vibesys.orchestration.request import RunRequest
    from vibesys.orchestration.skills import SkillSelection
    from vs_agent.api import AgentClientProtocol
    from vs_runtime.api import OrchestrationPlugin, Workspace
    from vs_runtime.api import RunStatus as PluginRunStatus
    from vs_runtime.api.infrastructure import AgentExecutionEnvironment, RunEnvironmentSession
    from vs_sandbox.api import ComputeBackendImpl, ProjectPathPolicy, Sandbox


class RunQuery(Protocol):
    """Semantic reads of a run's authoritative facts."""

    def view(self) -> RunView:
        """Return the current read-only snapshot of this run."""
        ...


class RunControl(Protocol):
    """Messages to the run loop, the sole writer of run state.

    `steer`/`pause`/`resume`/`stop` are messages to the writer, which holds
    an exclusive write lease, not writes performed by the caller. `pause`
    means: reach the next safe boundary, checkpoint durable state, and
    release the lease. `resume` means: acquire the lease, restore the
    checkpoint, and continue. A cross-process resume is
    `create_session(RunRequest(resume=ResumeRef(run_id)))`.
    """

    def steer(self, text: str) -> None:
        """Send free-text steering input to the active run."""
        ...

    def pause(self) -> None:
        """Request the run reach its next safe boundary and release the write lease."""
        ...

    def resume(self) -> None:
        """Request the run acquire the write lease and continue from checkpoint."""
        ...

    def stop(self) -> None:
        """Request the run terminate."""
        ...


class RunSession(RunQuery, RunControl, Protocol):
    """One live or resumable run: query + workspace + control + lifecycle."""

    def start(self) -> None:
        """Begin executing the run."""
        ...

    async def await_result(self) -> RunResult:
        """Wait for the run to reach a terminal state and return its outcome."""
        ...

    def on_committed_view(
        self, listener: Callable[[RunView, tuple[str, ...] | None], None]
    ) -> None:
        """Register a listener for policy views and policy-defined changed keys."""
        ...

    def on_ready(self, listener: Callable[[RunReady], None]) -> None:
        """Register the sole frontend listener for this run's readiness facts."""
        ...

    def create_auxiliary_agent(self, launch: AuxiliaryAgentLaunch) -> ManagedAgent:
        """Create a fresh product-owned auxiliary conversation for this run."""
        ...

    def close(self) -> None:
        """Close any auxiliary agents the caller did not release early."""
        ...


def create_session(
    request: RunRequest,
    *,
    sink: EventSink,
    registry: OrchestrationRegistry | None = None,
) -> RunSession:
    """Build a session for *request*, publishing its event stream to *sink*.

    Headless calls `create_session(req, sink=renderer.handle).start()`
    then `await session.await_result()`; server does the same with its own
    presentation sink, and reaches optional committed-state/readiness seams
    through `on_committed_view`/`on_ready` instead of an injected integration
    object. Pass a registry to execute a custom
    orchestration ID; otherwise the built-in registry is used.
    """
    return _create_session(request, sink=sink, registry=registry)


def _create_session(
    request: RunRequest,
    *,
    sink: EventSink,
    registry: OrchestrationRegistry | None = None,
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
) -> RunSession:
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
            self._ready_listener(_run_ready(resources))

    def _run_id(self) -> str:
        """Use the provisioned ID once a custom runtime has created its run."""
        if self._resources is not None:
            return self._resources.run_id
        return resolved_run_id(self._request)

    def _open_agent_environment(
        self,
        *,
        mounts: tuple[HostResource, ...] = (),
        agent_backend: str | None = None,
        cli_provider: str | None = None,
    ) -> _OpenedAgentEnvironment:
        """Open a private construction environment for core-owned agent wiring."""
        resources = self._resources
        if resources is None:
            message = "the run is not ready to create agents"
            raise RuntimeError(message)
        request = replace(
            resources.environment_request,
            agent_backend=(
                agent_backend
                if agent_backend is not None
                else resources.environment_request.agent_backend
            ),
            cli_provider=(
                cli_provider
                if cli_provider is not None
                else resources.environment_request.cli_provider
            ),
            environment_bind_mounts=(
                *resources.environment_request.environment_bind_mounts,
                *(_environment_bind_mount(mount) for mount in mounts),
            ),
        )
        opened = open_run_environment(resources.environment, request)
        backends: dict[str, Sandbox] | None = None
        use_docker = False
        isolated = False
        if resources.run_environment_sandboxed:
            backends = {"chat": opened.sandbox}
            use_docker = opened.view.cli_sandboxed
            isolated = opened.view.isolated
        return _OpenedAgentEnvironment(
            opened,
            config=resources.config,
            skill_selection=platform_skill_selection(resources.compute_backend),
            skill_source_dirs=resources.skill_source_dirs,
            project_path_policy=resources.project_path_policy,
            host_resources=resources.host_resources,
            backends=backends,
            use_docker=use_docker,
            isolated=isolated,
            run_id=resources.run_id,
            project=resources.project,
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
            missing = tuple(item.path for item in launch.readable_inputs if not item.path.exists())
            if missing:
                message = f"auxiliary agent readable path does not exist: {missing[0]}"
                raise FileNotFoundError(message)
            ownership = ExitStack()
            try:
                logger = RunLogger(resources.log_dir, tee_stderr=False)
                ownership.callback(logger.close)
                logger.switch(f"auxiliary-{launch.role}")
                opened = self._open_agent_environment(
                    mounts=tuple(
                        HostResource(
                            item.path,
                            HostResourceAccess.READ_ONLY,
                            item.purpose,
                        )
                        for item in launch.readable_inputs
                    ),
                    agent_backend=resources.agent_backend,
                    cli_provider=launch.provider,
                )
                ownership.callback(opened.close)
                spec = agent_spec_from_config(
                    resources.config,
                    backend=resources.agent_backend,
                    driver=launch.driver,
                    provider=launch.provider,
                    model=launch.model,
                )
                client = build_agent_client(
                    spec=spec,
                    backends=opened.backends,
                    skill_source_dirs=list(opened.skill_source_dirs),
                    skill_selection=opened.skill_selection,
                    run_log_file=logger.writer,
                    use_docker=opened.use_docker,
                    log_dir=resources.log_dir,
                    project_path_policy=opened.project_path_policy,
                    require_host_sandbox=not opened.use_docker,
                    host_resources=(
                        *opened.host_resources,
                        *(
                            HostResource(
                                item.path,
                                HostResourceAccess.READ_ONLY,
                                item.purpose,
                            )
                            for item in launch.readable_inputs
                        ),
                    ),
                    events=self._integration.agent_events,
                )
                ownership.callback(client.close)
                agent = create_managed_conversation(
                    client,
                    ManagedConversationSpec(
                        role=launch.role,
                        member_id=launch.member_id,
                        workspace=resources.workspace,
                        system_prompt=launch.system_prompt,
                        continuation_prompt=launch.continuation_prompt,
                        tool_servers=opened.investigation_tools(),
                        environment=tuple(
                            (item.environment_variable, opened.agent_path(item.path))
                            for item in launch.readable_inputs
                        ),
                    ),
                    resources=(logger, opened, client),
                )
                ownership.pop_all()
            except BaseException as construction_error:
                try:
                    ownership.close()
                except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-948033 [BLE001]; cleanup must preserve the construction failure while releasing every acquired resource.
                    construction_error.add_note(
                        "Additional error while cleaning up auxiliary-agent construction: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                raise
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
                    outer_loop=request.orchestration_id,
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
                loop=request.orchestration_id,
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
            loop=self._request.orchestration_id,
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


def _run_ready(resources: RunResources) -> RunReady:
    """Project private resource facts to the narrow frontend contract."""
    return RunReady(
        run_id=resources.run_id,
        project_root=resources.project.root,
        log_directory=resources.log_dir,
        agent_driver=resources.driver,
        agent_provider=resources.provider,
        agent_model=resources.model,
        role_models=resources.role_models,
    )


class _AgentPathSandbox(Protocol):
    """The one lookup `_OpenedAgentEnvironment.agent_path` needs from a sandbox.

    Mirrors the runtime environment's agent-path contract: every
    sandbox kind `RunEnvironment.open` can return (host-only or Docker)
    implements this, even though `vs_sandbox.execution.Sandbox` itself does
    not declare it.
    """

    def agent_path(self, host_path: Path | str) -> str: ...


@dataclass(frozen=True, slots=True)
class _OpenedAgentEnvironment:
    """`AgentEnvironment` backed by one already-opened `RunEnvironmentSession`."""

    _session: RunEnvironmentSession
    config: Config
    skill_selection: SkillSelection
    skill_source_dirs: tuple[Path, ...]
    project_path_policy: ProjectPathPolicy
    host_resources: tuple[HostResource, ...]
    backends: dict[str, Sandbox] | None
    use_docker: bool
    isolated: bool
    run_id: str
    project: Project

    def agent_path(self, host: Path) -> str:
        """Map a host path to its path inside this environment's sandbox."""
        return cast("_AgentPathSandbox", self._session.sandbox).agent_path(host)

    def investigation_tools(self) -> tuple[ToolServerDescriptor, ...]:
        """Build the read-only tool server for investigating this run's history.

        Launches `entrypoints.chat_tools_server` with the project root
        translated into this environment's own sandbox path
        (`self.agent_path`), so the subprocess -- which the agent's own
        driver spawns inside that sandbox -- can resolve it.
        """
        descriptor = expose_as_tools(
            name="vibesys-run",
            entrypoint_module="entrypoints.chat_tools_server",
            entrypoint_args=(
                "--run-id",
                self.run_id,
                "--project-root",
                self.agent_path(self.project.root),
            ),
        )
        return (descriptor,)

    def close(self) -> None:
        """Release the opened environment session."""
        self._session.close()


def _environment_bind_mount(mount: HostResource) -> EnvironmentBindMount:
    """Fold one requested host mount into an `EnvironmentBindMount`.

    `HostResource.agent_path` names the fixed container path a caller wants
    a resource presented at (its own docstring: unset means "imported at its
    own host path"), so that is exactly the container path this mount asks
    for, falling back to the host path unchanged when unset.
    """
    return EnvironmentBindMount(
        mount.path,
        mount.agent_path if mount.agent_path is not None else str(mount.path),
        read_only=mount.access is HostResourceAccess.READ_ONLY,
    )
