"""Product composition for the runtime-owned core run host."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, cast

from vibesys.composition import AgentToolContext, agent_spec_from_config, resolve_run_agent_specs
from vibesys.events import CoreEventType, FrameworkSource, FrameworkWarningData
from vibesys.orchestration.skill_selection import platform_skill_selection
from vibesys.run.agent_events import CoreAgentEventSink
from vibesys.run.contracts import ProfilerKind
from vibesys.run.core_services import (
    CoreAgentResources,
    CoreCompositionError,
    CoreEnvironment,
    CoreResources,
    CoreServices,
    agent_session_spec,
    build_core_services,
)
from vibesys.run.evaluation import create_evaluation
from vibesys.run.measurement_events import CoreMeasurementEvents
from vibesys.run.profilers import WITHHELD_TOOLS_ENV, ProfilerToolOffer, profiler_tool_offer
from vibesys.run.resources import open_run_resources
from vibesys.run.slurm_evaluation import SlurmSemanticEvaluationExecutor
from vs_agent.api import AgentSessionKey, AgentSessionState, DurableSessionStore
from vs_runtime.api import PollingEvaluationExecutor, RetryingEvaluationExecutor
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    BlockingOperations,
    CapacityHandling,
    QuotaPolicy,
    RunHostComponents,
    StateValidationPasses,
    WorkspaceResourceFactory,
    bounded_stop,
    create_model_request_reconciler,
    create_workspace_runtime,
    install_capacity_gate,
    open_run_host,
    release_all,
    stop_gated_evaluation,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from pathlib import Path

    from pydantic import BaseModel

    from vibesys.run.contracts import RunRequest
    from vibesys.run.integration import CommittedStateProjector, LocalRunIntegration
    from vibesys.run.profilers import ProfilerPreflight
    from vibesys.run.resources import _PreparedRun
    from vs_agent.api import AgentClientProtocol, AgentInvocationStore, AgentSpec
    from vs_core.api import LifecycleCapability
    from vs_mcp.api import ToolServerDescriptor
    from vs_project.api import StateNamespace
    from vs_runtime.api import (
        AgentRole,
        Evaluation,
        OrchestrationDescriptor,
        OrchestrationPlugin,
        OrchestrationResumeDecision,
        RunFacts,
        Workspaces,
    )
    from vs_runtime.api.core import RunTiming
    from vs_runtime.api.infrastructure import (
        AgentExecutionEnvironment,
        RunState,
        StopTimer,
        WorkspaceRuntime,
    )
    from vs_sandbox.api import ComputeBackendImpl, DockerSandbox
    from vs_sim.api import BlockingRunner, Network, ReleaseStack, SleepingClock


type StatelessToolServer = Callable[[object], ToolServerDescriptor | None]
"""The server of a tool that holds no run state, for a run's tool context; None if it has none."""


def _profiler_tool_offer(resources: _PreparedRun) -> ProfilerToolOffer:
    return profiler_tool_offer(
        ProfilerKind(resources.facts.profiler_id),
        editor_has_gpu=resources.environment_resources.view.editor_has_gpu,
        captures_remotely=resources.environment_resources.view.profile_execution == "remote",
    )


def _tool_context(resources: _PreparedRun) -> AgentToolContext:
    env = resources.environment_resources.view.profiler_mcp_env
    withheld = _profiler_tool_offer(resources).withheld
    if withheld:
        env = (*env, (WITHHELD_TOOLS_ENV, ",".join(withheld)))
    return AgentToolContext(resources.facts.profiler_id, env)


_NO_PROFILE_CAPTURE_NOTE = (
    "[profiler] framework profile capture is unavailable in this run (it needs a Slurm run "
    "environment with remote profiling), so profile workstreams are not offered. A profiler "
    "agent can still profile with its own commands."
)
_CORE_CLEANUP_FAILURE = "core services cleanup failed"

STOP_GRACE_S = 60.0
"""Seconds in-flight agent turns get to end on their own after a stop request.

A stop rejects new evaluations at once and cancels the running ones, so a turn's next
evaluation tool call tells it the run is stopping. Ending the turn then takes about one
more model round trip (seconds to a few tens of seconds), and 60 s allows two. After that
the run is cancelled like a repeated signal, which kills the agent processes; without
this bound a turn could run until the agent CLI timeout (3600 s).
"""


@dataclass(slots=True)
class _ProductHostFactory:
    request: RunRequest
    integration: LocalRunIntegration
    open_agent_environment: Callable[..., AgentExecutionEnvironment] | None
    projector: CommittedStateProjector | None
    agent_client_factory: Callable[..., AgentClientProtocol] | None
    backend_factory: Callable[..., ComputeBackendImpl]
    plugin: OrchestrationPlugin
    invocation_store_factory: Callable[[RunState, AgentSessionKey], AgentInvocationStore] | None
    timing: RunTiming
    blocking: BlockingRunner
    network: Network
    resume_policy: (
        Callable[
            [OrchestrationDescriptor, OrchestrationDescriptor],
            OrchestrationResumeDecision,
        ]
        | None
    )
    options: BaseModel
    profiler_preflight: ProfilerPreflight
    stateless_tool_servers: Mapping[str, StatelessToolServer] | None = None
    core_services: CoreServices | None = field(default=None, init=False)
    local_evaluation: Evaluation | None = field(default=None, init=False)
    facts: RunFacts | None = field(default=None, init=False)

    def prepare(self, ownership: ReleaseStack) -> RunHostComponents:
        """Open product resources and compose the core services over them."""
        plugin = self.plugin
        if self.request.orchestration.id != plugin.id:
            message = (
                f"selected orchestration {self.request.orchestration.id!r} does not match "
                f"plugin {plugin.id!r}"
            )
            raise ValueError(message)
        agent_specs = resolve_run_agent_specs(
            self.request.config,
            plugin.agents,
            backend=self.request.agent_backend,
            provider=self.request.cli_provider,
        )
        if agent_specs:
            _require_supported_fallback(self.request.config.agent.quota.to_policy(), agent_specs)
        resources = open_run_resources(
            self.request,
            self.integration,
            ownership=ownership,
            agent_specs=agent_specs,
            resume_policy=self.resume_policy,
            backend_factory=self.backend_factory,
            profiler_preflight=self.profiler_preflight,
        )
        return self._components(resources, ownership)

    def _components(self, resources: _PreparedRun, ownership: ReleaseStack) -> RunHostComponents:
        blocking = BlockingOperations(self.blocking)
        project = resources.project_resources
        environment = resources.environment_resources
        logger = project.logger
        policy = self.plugin.core
        if policy is None:
            message = "core services require a plugin with a core policy"
            raise ValueError(message)
        workspace_resources = WorkspaceResourceFactory(
            project,
            environment,
            evaluation_plan=resources.evaluation_plan,
            memory_paths=policy.artifact_directories,
            skill_source_dirs=tuple(resources.skill_source_paths),
            skill_selection=platform_skill_selection(resources.backend),
            host_resources=resources.agent_host_resources,
            events=self.integration.workspace_resource_event,
            threads=self.integration.threads,
            blocking=self.blocking,
            model_requests=(
                create_model_request_reconciler() if environment.view.env_kind == "modal" else None
            ),
            root_agent_environment_opener=self._root_agent_environment_opener(),
        )
        offer = _profiler_tool_offer(resources)
        if offer.withheld:
            logger.lprint(f"[profiler] {offer.reason}.")
        agent_runtime = create_workspace_runtime(
            workspace_resources=workspace_resources,
            blocking=blocking,
            validation_passes=StateValidationPasses(
                project.project.state.local_namespace(project.state.run_id, "validation")
            ),
        )
        workspaces = agent_runtime.workspaces
        evaluation = create_evaluation(
            project.state.run_id,
            self.request,
            agent_runtime,
            self.integration.events,
            logger.lprint,
        )
        evaluation = stop_gated_evaluation(evaluation, self.integration.control)
        self.local_evaluation = evaluation
        self.facts = resources.facts
        self.core_services = self._core_services(
            resources, workspace_resources, agent_runtime, evaluation, ownership
        )
        if self.core_services.profile_capture_withheld:
            logger.lprint(_NO_PROFILE_CAPTURE_NOTE)
        return RunHostComponents(workspaces=workspaces, blocking=blocking)

    def _root_agent_environment_opener(
        self,
    ) -> Callable[[AgentExecutionConfiguration], AgentExecutionEnvironment] | None:
        opener = self.open_agent_environment
        if opener is None:
            return None

        def open_environment(
            configuration: AgentExecutionConfiguration,
        ) -> AgentExecutionEnvironment:
            return opener(
                mounts=configuration.resources,
                agent_backend=configuration.spec.backend.value,
                cli_provider=configuration.spec.provider,
            )

        return open_environment

    def _agent_configuration(
        self, resources: _PreparedRun, role: AgentRole
    ) -> AgentExecutionConfiguration:
        """The session-fixed agent inputs of one role: its spec and the resources it mounts.

        The evaluation tool's socket is not mounted per role: its directory is mounted into
        every agent container when the run environment opens (``evaluation_socket_mount``).
        """
        spec = resources.agent_specs[role.id]
        return AgentExecutionConfiguration(
            agent_id=role.id,
            spec=spec,
            resources=(
                resources.profiler_agent_resources
                if any(tool.id == "profiler" for tool in role.extra_tools)
                else ()
            ),
            reasoning_effort=spec.reasoning_effort,
        )

    def _offered_roles(self, resources: _PreparedRun) -> tuple[AgentRole, ...]:
        """The plugin's roles, without a stateless tool whose provider declines for this run.

        A provider that declines (the selected profiler is ``none``) says the run has no such
        tool, and a strategy only runs the turns that use it when the run offers it. A stateless
        tool with no provider at all is a wiring mistake and stays for composition to refuse.
        """
        if not self.plugin.agents:
            return ()
        context = _tool_context(resources)
        declined = frozenset(
            tool
            for tool, server in (self.stateless_tool_servers or {}).items()
            if server(context) is None
        )
        return tuple(
            role.model_copy(
                update={
                    "extra_tools": tuple(
                        tool
                        for tool in role.extra_tools
                        if not (tool.stateless and tool.id in declined)
                    )
                }
            )
            for role in self.plugin.agents
        )

    def _stateless_tool_servers(
        self, resources: _PreparedRun, roles: tuple[AgentRole, ...]
    ) -> dict[str, ToolServerDescriptor]:
        """The server of each stateless tool this run's environment can serve, by tool id."""
        if not roles:
            return {}
        context = _tool_context(resources)
        served = {
            tool: server(context) for tool, server in (self.stateless_tool_servers or {}).items()
        }
        return {tool: server for tool, server in served.items() if server is not None}

    def _core_services(
        self,
        resources: _PreparedRun,
        workspace_resources: WorkspaceResourceFactory,
        agent_runtime: WorkspaceRuntime,
        evaluation: Evaluation,
        ownership: ReleaseStack,
    ) -> CoreServices:
        """Compose the core run's services from the resources opened above."""
        policy = self.plugin.core
        if policy is None:
            message = "core services require a plugin with a core policy"
            raise ValueError(message)
        project = resources.project_resources
        workspaces = agent_runtime.workspaces
        roles, agents, role_models = self._core_agent_resources(
            resources, workspace_resources, agent_runtime, ownership
        )
        namespace = project.project.state.local_namespace(project.state.run_id, "core-evaluation")
        executor = self._semantic_executor(
            self.integration, resources, workspaces, namespace, self.timing.clock
        )
        return build_core_services(
            policy,
            CoreResources(
                run_id=project.state.run_id,
                project=project.project,
                facts=resources.facts,
                options=self.options,
                workspaces=workspaces,
                evaluation=RetryingEvaluationExecutor(
                    executor
                    if executor is not None
                    else PollingEvaluationExecutor(evaluation, workspaces, clock=self.timing.clock),
                    self.request.config.evaluation.evaluation_retries(),
                    project.project.state.local_namespace(
                        project.state.run_id, "core-evaluation-tries"
                    ),
                ),
                environment=self._core_environment(resources),
                roles=roles,
                role_models=role_models,
                agents=agents,
                clock=self.timing.clock,
                github=self.integration.github,
                threads=self.integration.threads,
                blocking=self.blocking,
                network=self.network,
                evaluation_diagnostics=self._evaluation_diagnostic,
                stateless_tool_servers=self._stateless_tool_servers(resources, roles),
                commands=agent_runtime.commands,
                measurement_observer=CoreMeasurementEvents(self.integration.events),
                commit_observer=self.integration.core_commit_observer(
                    project.state.run_id, self.projector, self.plugin.id
                ),
            ),
        )

    def _core_agent_resources(
        self,
        resources: _PreparedRun,
        workspace_resources: WorkspaceResourceFactory,
        agent_runtime: WorkspaceRuntime,
        ownership: ReleaseStack,
    ) -> tuple[tuple[AgentRole, ...], CoreAgentResources | None, dict[str, str]]:
        """Resolve offered roles and open their indivisible session bundle, if any."""
        roles = self._offered_roles(resources)
        if not roles:
            return (), None, {}
        if self.agent_client_factory is None:
            raise CoreCompositionError(
                "agent_client_factory", "a core run with agent roles needs a client factory"
            )
        if self.invocation_store_factory is None:
            raise CoreCompositionError(
                "invocation_store_factory",
                "a core run with agent roles needs a durable invocation journal",
            )
        project = resources.project_resources
        session_store = DurableSessionStore(
            project.state.local("agent").slot("sessions.json", AgentSessionState),
            log=project.logger.lprint,
            threads=self.timing.threads,
        )
        scope = workspace_resources.root.agent_scope()
        specs = resources.agent_specs
        base = agent_spec_from_config(
            self.request.config,
            backend=self.request.agent_backend,
            provider=self.request.cli_provider,
        )
        # One client serves every role, so each role's own model and effort ride the spec.
        base = replace(
            base,
            role_models={role: spec.model for role, spec in specs.items() if spec.model},
            role_reasoning_efforts={
                role: spec.reasoning_effort for role, spec in specs.items() if spec.reasoning_effort
            },
        )
        environment = scope.open_environment(
            AgentExecutionConfiguration(
                agent_id="core", spec=base, reasoning_effort=base.reasoning_effort
            )
        )
        ownership.callback(environment.close)
        events = CoreAgentEventSink(self.integration.events.record, self.timing.clock)
        client = self.agent_client_factory(
            spec=base,
            clock=self.timing.clock,
            session_store=session_store,
            backends=(
                {role.id: environment.backends["chat"] for role in roles}
                if environment.backends is not None
                else None
            ),
            skill_source_dirs=list(environment.skill_source_dirs),
            skill_selection=environment.skill_selection,
            run_log_file=scope.current_log_file(),
            # A turn runs in the container that mounts its workspace, so a
            # candidate's turn is not sent to the root container.
            workspace_sandboxes=(
                partial(_candidate_agent_sandbox, agent_runtime) if environment.use_docker else None
            ),
            use_docker=environment.use_docker,
            log_dir=scope.log_directory,
            agent_homes_dir=scope.agent_homes_directory,
            host_resources=(*environment.host_resources,),
            project_path_policy=environment.project_path_policy,
            require_host_sandbox=not environment.use_docker,
            events=events,
        )
        ownership.callback(client.close)
        install_capacity_gate(
            client,
            control=self.integration.control,
            events=events,
            handling=CapacityHandling.for_policy(
                self.request.config.agent.quota.to_policy(), self.integration.threads
            ),
            clock=self.timing.clock,
        )
        configuration = partial(self._agent_configuration, resources)
        invocations = self.invocation_store_factory(
            project.state, AgentSessionKey.for_member("core", "run")
        )
        role_models = {
            role: spec.model
            for role, spec in specs.items()
            if spec.model and spec.model != base.model
        }
        return (
            roles,
            CoreAgentResources(
                client=client,
                invocation_slot=invocations,
                configuration=configuration,
                session_spec=agent_session_spec(
                    client=client,
                    environment=environment,
                    specs=specs,
                    variables=scope.environment_variables,
                    configuration=configuration,
                ),
                lifecycle=self.integration.agent_execution_event,
            ),
            role_models,
        )

    def _core_environment(self, resources: _PreparedRun) -> CoreEnvironment:
        """The evaluation environment and product bounds as facts: capacity, pacing, lifecycle."""
        view = resources.environment_resources.view
        capacity = 1
        lifecycle: frozenset[LifecycleCapability] = frozenset()
        if view.cluster_evaluation is not None:
            capacity = view.cluster_evaluation.evaluation_capacity
        if view.captures_profile:
            lifecycle = frozenset({"profile-capture"})
        config = self.request.config
        return CoreEnvironment(
            view=view,
            evaluation_plan=resources.evaluation_plan,
            evaluation_capacity=capacity,
            queue_allowance_seconds=config.evaluation.queue_allowance_seconds,
            observe_interval_seconds=config.evaluation.observe_interval_seconds,
            observe_backoff_cap_seconds=config.evaluation.observe_backoff_cap_seconds,
            request_retries=config.evaluation.request_retries(),
            max_run_seconds=config.run.max_run_seconds,
            cancellation_bound_seconds=config.run.cancellation_bound_seconds,
            cleanup_bound_seconds=config.run.cleanup_bound_seconds,
            reconciliation_bound_seconds=config.run.reconciliation_bound_seconds,
            lifecycle=lifecycle,
        )

    def _evaluation_diagnostic(self, line: str) -> None:
        """Journal one agent evaluation call the run refused as busy."""
        self.integration.events.emit(
            CoreEventType.FRAMEWORK_WARNING,
            data=FrameworkWarningData(
                summary=line, source=FrameworkSource.LOOP, source_label="agent-evaluation"
            ),
        )

    @staticmethod
    def _semantic_executor(
        integration: LocalRunIntegration,
        resources: _PreparedRun,
        workspaces: Workspaces,
        namespace: StateNamespace,
        clock: SleepingClock,
    ) -> SlurmSemanticEvaluationExecutor | None:
        """Build the cluster's semantic execution adapter when the environment reports one."""
        cluster = resources.environment_resources.view.cluster_evaluation
        if cluster is None:
            return None
        return SlurmSemanticEvaluationExecutor(
            cluster.settings.config,
            cluster.settings.policy,
            cluster.plan,
            resources.evaluation_plan,
            workspaces,
            namespace,
            namespace.external_directory() / "slurm-provider-handles",
            clock=clock,
            sleeper=clock,
            processes=integration.system.slurm_processes,
        )

    async def stop_new_work(self) -> None:
        """Nothing to cancel at the moment of a stop: no run-owned service holds jobs here.

        Every core measurement, an agent's included, is a job of the run's evaluation
        executor. Core refuses new submissions once the stop is committed, and the executor
        cancels the jobs still on the cluster when ``close_core_services`` closes it as the
        run exits (``tests/system/agent_evaluation/test_stops_on_stop.py``). The legacy
        evaluation service this hook once cancelled was never installed on a core run.
        """

    async def close_core_services(self) -> None:
        """Release the evaluation executor before the workspaces it evaluates in."""
        if self.core_services is not None:
            await release_all([self.core_services.close], failure=_CORE_CLEANUP_FAILURE)


@dataclass(frozen=True, slots=True)
class CoreHost:
    """The open host of a core run: the services its loop runs on, and the run's workspaces.

    ``facts`` are the run's resolved facts. ``evaluation`` is the trusted evaluation the run
    measures through on a host without a cluster executor; a cluster run measures through
    ``services.evaluation`` instead.
    """

    workspaces: Workspaces
    evaluation: Evaluation
    facts: RunFacts
    services: CoreServices
    timing: RunTiming


@asynccontextmanager
async def open_product_core_host(  # noqa: PLR0913  # lint-waiver: LW-948092 [PLR0913]; independent product effects remain explicit at the sole wiring boundary.
    request: RunRequest,
    integration: LocalRunIntegration,
    *,
    plugin: OrchestrationPlugin,
    options: BaseModel,
    resume_policy: (
        Callable[
            [OrchestrationDescriptor, OrchestrationDescriptor],
            OrchestrationResumeDecision,
        ]
        | None
    ) = None,
    open_agent_environment: Callable[..., AgentExecutionEnvironment] | None = None,
    projector: CommittedStateProjector | None = None,
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
    backend_factory: Callable[..., ComputeBackendImpl],
    profiler_preflight: ProfilerPreflight,
    stop_timer: StopTimer,
    timing: RunTiming,
    blocking: BlockingRunner,
    network: Network,
    invocation_store_factory: Callable[[RunState, AgentSessionKey], AgentInvocationStore]
    | None = None,
    stateless_tool_servers: Mapping[str, StatelessToolServer] | None = None,
) -> AsyncIterator[CoreHost]:
    """Open the host of a plugin whose ``core`` policy the runtime drives.

    Composition builds every core service from the resources it opens, and fails with
    ``CoreCompositionError`` naming what is missing. A stop requested while the caller's
    block runs is bounded: if the block is still running `STOP_GRACE_S` later (timed by
    *stop_timer*), its task is cancelled and the block ends in `RunStopped`. Teardown then
    releases the core services before the workspaces they evaluate in.
    """
    if plugin.core is None:
        message = f"orchestration {plugin.id!r} has no core policy"
        raise CoreCompositionError("core", message)
    factory = _ProductHostFactory(
        request=request,
        integration=integration,
        open_agent_environment=open_agent_environment,
        projector=projector,
        agent_client_factory=agent_client_factory,
        backend_factory=backend_factory,
        profiler_preflight=profiler_preflight,
        plugin=plugin,
        invocation_store_factory=invocation_store_factory,
        timing=timing,
        blocking=blocking,
        network=network,
        resume_policy=resume_policy,
        options=options,
        stateless_tool_servers=stateless_tool_servers,
    )
    async with open_run_host(factory.prepare, runner=blocking) as host:
        try:
            async with bounded_stop(
                integration.control,
                grace_s=STOP_GRACE_S,
                on_stop=factory.stop_new_work,
                timer=stop_timer,
            ):
                services = factory.core_services
                if services is None:
                    message = "core host composition produced no services"
                    raise CoreCompositionError("services", message)
                local_evaluation = factory.local_evaluation
                if local_evaluation is None:
                    message = "core host composition produced no local evaluation"
                    raise CoreCompositionError("evaluation", message)
                facts = factory.facts
                if facts is None:
                    message = "core host composition produced no run facts"
                    raise CoreCompositionError("facts", message)
                yield CoreHost(host.workspaces, local_evaluation, facts, services, timing)
        finally:
            await factory.close_core_services()


__all__ = [
    "STOP_GRACE_S",
    "CoreHost",
    "open_product_core_host",
]


def _candidate_agent_sandbox(runtime: WorkspaceRuntime, path: Path) -> DockerSandbox | None:
    """The container that mounts the candidate checked out at ``path``.

    ``None`` is the root workspace, which the role's own container serves; a
    path no live candidate owns raises ``AgentWorkspaceRouteError``.
    """
    return cast("DockerSandbox | None", runtime.workspaces.agent_sandbox_at(path))


def _require_supported_fallback(policy: QuotaPolicy, agent_specs: Mapping[str, AgentSpec]) -> None:
    """Reject a fallback a core run cannot honor, before the run starts.

    A core run drives every role through one agent client bound to one provider, so it
    cannot open a session on another provider. A fallback is therefore refused rather than
    accepted and ignored; a fallback that is a role's own provider is refused first, since
    it could never be a switch.
    """
    if policy.fallback is None:
        return
    for role_id, spec in agent_specs.items():
        if spec.provider == policy.fallback.provider:
            message = (
                f"agent.quota.fallback_provider {policy.fallback.provider!r} is the provider "
                f"role {role_id!r} already runs on; a fallback must be a different provider"
            )
            raise ValueError(message)
    message = (
        f"agent.quota.fallback_provider {policy.fallback.provider!r}: a run drives every role "
        "through one agent client on one provider, so it cannot switch provider on a quota "
        "stop; remove agent.quota.fallback_provider and fallback_model (policy = 'fallback' "
        "needs them, so use 'pause', 'wait' or 'fail')"
    )
    raise ValueError(message)
