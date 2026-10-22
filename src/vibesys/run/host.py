"""Product composition for the runtime-owned core run host."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, cast

from vibesys.comparisons.api import (
    ComparisonReport,
    comparisons_read_data,
    stored_comparisons,
    visible_comparison_variables,
)
from vibesys.composition import AgentToolContext, agent_spec_from_config, resolve_agent_specs
from vibesys.events import CoreEventType, EventStatus, FrameworkSource, FrameworkWarningData
from vibesys.orchestration.skill_selection import platform_skill_selection
from vibesys.run.agent_events import CoreAgentEventSink
from vibesys.run.contracts import ProfilerKind
from vibesys.run.core_services import (
    CoreCompositionError,
    CoreEnvironment,
    CoreResources,
    CoreServices,
    agent_session_spec,
    build_core_services,
    turn_executor,
)
from vibesys.run.evaluation import CoreLocalValidationEvents, create_evaluation
from vibesys.run.evaluation_socket import prepare_queue_status_socket
from vibesys.run.measurement_events import CoreMeasurementEvents
from vibesys.run.profilers import WITHHELD_TOOLS_ENV, ProfilerToolOffer, profiler_tool_offer
from vibesys.run.queue_status import queue_status_reader, queue_status_service
from vibesys.run.resources import open_run_resources
from vibesys.run.slurm_evaluation import SlurmSemanticEvaluationExecutor
from vs_agent.api import (
    AgentSessionKey,
    AgentSessionState,
    DurableSessionStore,
    ProviderRoutedExecutor,
)
from vs_runtime.api import PollingEvaluationExecutor, RetryingEvaluationExecutor, TryHandles
from vs_runtime.api.core import MeasurementDecisions
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    BlockingOperations,
    CapacityHandling,
    QuotaPolicy,
    RunHostComponents,
    StateBuildStore,
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
    from vs_runtime.api.core import RunTiming, StageOutputOpener
    from vs_runtime.api.infrastructure import (
        AgentExecutionEnvironment,
        AgentExecutionScope,
        OwnedWorkspaces,
        RunState,
        StopTimer,
        WorkspaceRuntime,
    )
    from vs_sandbox.api import ComputeBackendImpl, DockerSandbox
    from vs_sim.api import BlockingRunner, Network, ReleaseStack


type StatelessToolServer = Callable[[object], ToolServerDescriptor | None]
"""The server of a tool that holds no run state, for a run's tool context; None if it has none."""


def _profiler_tool_offer(resources: _PreparedRun) -> ProfilerToolOffer:
    return profiler_tool_offer(
        ProfilerKind(resources.facts.profiler_id),
        editor_has_gpu=resources.environment_resources.view.editor_has_gpu,
        captures_remotely=resources.environment_resources.view.profile_execution == "remote",
    )


def _tool_context(resources: _PreparedRun, queue_status_socket: Path) -> AgentToolContext:
    env = resources.environment_resources.view.profiler_mcp_env
    withheld = _profiler_tool_offer(resources).withheld
    if withheld:
        env = (*env, (WITHHELD_TOOLS_ENV, ",".join(withheld)))
    return AgentToolContext(
        resources.facts.profiler_id, env, queue_status_socket=str(queue_status_socket)
    )


def _slurm_handle_root(namespace: StateNamespace) -> Path:
    """Where the run's cluster evaluation executor keeps its handles and cluster records."""
    return namespace.external_directory() / "slurm-provider-handles"


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
    comparisons: tuple[ComparisonReport, ...] = field(default=(), init=False)

    def prepare(self, ownership: ReleaseStack) -> RunHostComponents:
        """Open product resources and compose the core services over them."""
        plugin = self.plugin
        if self.request.orchestration.id != plugin.id:
            message = (
                f"selected orchestration {self.request.orchestration.id!r} does not match "
                f"plugin {plugin.id!r}"
            )
            raise ValueError(message)
        agent_specs = resolve_agent_specs(
            self.request.config,
            plugin.agents,
            backend=self.request.agent_backend,
            provider=self.request.cli_provider,
            allow_unvetted_model=self.request.allow_unvetted_model,
        )
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
        blocking = BlockingOperations(self.blocking, self.timing.threads)
        project = resources.project_resources
        environment = resources.environment_resources
        logger = project.logger
        session_store = DurableSessionStore(
            project.state.local("agent").slot("sessions.json", AgentSessionState),
            log=logger.lprint,
            threads=self.timing.threads,
        )
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
            builds=StateBuildStore(
                project.project.state.local_namespace(project.state.run_id, "builds")
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
        self.comparisons = self._read_comparisons(resources)
        self.core_services = self._core_services(
            resources, workspace_resources, session_store, agent_runtime, evaluation, ownership
        )
        if self.core_services.profile_capture_withheld:
            logger.lprint(_NO_PROFILE_CAPTURE_NOTE)
        self._journal_comparisons()
        return RunHostComponents(workspaces=workspaces, blocking=blocking)

    def _read_comparisons(self, resources: _PreparedRun) -> tuple[ComparisonReport, ...]:
        """What the project's store holds for the task's comparison baselines.

        Reads only: a run never measures a comparison (``vibesys compare`` does), so this starts
        no job and cannot block the run.
        """
        bundle = self.request.input_bundle
        if not bundle.manifest.comparison:
            return ()
        cluster = resources.environment_resources.view.cluster_evaluation
        return stored_comparisons(
            resources.project_resources.project,
            bundle,
            None if cluster is None else cluster.settings,
        )

    def _journal_comparisons(self) -> None:
        """Journal the comparison readings the run found."""
        if not self.comparisons:
            return
        self.integration.events.emit(
            CoreEventType.COMPARISONS_READ,
            status=EventStatus.COMPLETED,
            data=comparisons_read_data(self.comparisons),
        )

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

    def _offered_roles(self, context: AgentToolContext) -> tuple[AgentRole, ...]:
        """The plugin's roles, without a stateless tool whose provider declines for this run.

        A provider that declines (the selected profiler is ``none``) says the run has no such
        tool, and a strategy only runs the turns that use it when the run offers it. A stateless
        tool with no provider at all is a wiring mistake and stays for composition to refuse.
        """
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

    def _stateless_tool_servers(self, context: AgentToolContext) -> dict[str, ToolServerDescriptor]:
        """The server of each stateless tool this run's environment can serve, by tool id."""
        served = {
            tool: server(context) for tool, server in (self.stateless_tool_servers or {}).items()
        }
        return {tool: server for tool, server in served.items() if server is not None}

    def _core_services(  # noqa: PLR0913  # lint-waiver: LW-948090 [PLR0913]; each argument is an independently owned resource the host already opened, and the composition reads them once.
        self,
        resources: _PreparedRun,
        workspace_resources: WorkspaceResourceFactory,
        session_store: DurableSessionStore,
        agent_runtime: WorkspaceRuntime,
        evaluation: Evaluation,
        ownership: ReleaseStack,
    ) -> CoreServices:
        """Compose the core run's services from the resources opened above."""
        policy = self.plugin.core
        if policy is None:
            message = "core services require a plugin with a core policy"
            raise ValueError(message)
        factory = self.agent_client_factory
        if factory is None:
            raise CoreCompositionError(
                "agent_client_factory", "a core run needs a factory for its agent client"
            )
        if self.invocation_store_factory is None:
            raise CoreCompositionError(
                "invocation_store_factory", "a core run needs a durable invocation journal"
            )
        project = resources.project_resources
        workspaces = agent_runtime.workspaces
        scope = workspace_resources.root.agent_scope()
        specs = resources.agent_specs
        base = agent_spec_from_config(
            self.request.config,
            backend=self.request.agent_backend,
            provider=self.request.cli_provider,
            allow_unvetted_model=self.request.allow_unvetted_model,
        )
        events = CoreAgentEventSink(self.integration.events.record, self.timing.clock)
        agents: dict[str, _ProviderAgents] = {}
        for provider, spec in _provider_specs(base, specs).items():
            agents[provider] = self._open_provider_agents(
                factory, spec, scope, session_store, agent_runtime, events, ownership
            )
        turns = ProviderRoutedExecutor(
            {provider: turn_executor(agent.client) for provider, agent in agents.items()}
        )
        # Every core session shares one run-scoped journal, named by a fixed member key.
        invocations = self.invocation_store_factory(
            project.state, AgentSessionKey.for_member("core", "run")
        )
        namespace = project.project.state.local_namespace(project.state.run_id, "core-evaluation")
        # Each stage's output goes to the run's log store under its gate, named by the
        # measurement's handle and try, as its gate events name it: an in-process stage's as
        # its command prints it, a Slurm stage's as each scheduler poll reads it.
        tries = TryHandles()
        decisions = MeasurementDecisions()
        stage_output = tries.stage_output(decisions.stage_output(resources.stage_output))
        executor = self._semantic_executor(
            resources,
            workspaces,
            namespace,
            stage_output,
            ledger_identity=(project.project.state.load_project().project_id, project.state.run_id),
        )
        tool_context = _tool_context(
            resources,
            self._serve_queue_status(resources, _slurm_handle_root(namespace), ownership),
        )
        configuration = partial(self._agent_configuration, resources)
        return build_core_services(
            policy,
            CoreResources(
                run_id=project.state.run_id,
                project=project.project,
                resuming=project.resuming,
                facts=resources.facts,
                options=self.options,
                workspaces=workspaces,
                evaluation=RetryingEvaluationExecutor(
                    executor
                    if executor is not None
                    else PollingEvaluationExecutor(
                        evaluation,
                        workspaces,
                        clock=self.timing.clock,
                        stage_output=stage_output,
                        control=self.integration.control,
                    ),
                    self.request.config.evaluation.evaluation_retries(),
                    project.project.state.local_namespace(
                        project.state.run_id, "core-evaluation-tries"
                    ),
                    tries=tries,
                ),
                environment=self._core_environment(resources),
                roles=self._offered_roles(tool_context),
                role_models={
                    role: spec.model
                    for role, spec in specs.items()
                    if spec.model and spec.model != base.model
                },
                agent_turns=turns,
                control=self.integration.control,
                invocation_slot=invocations,
                configuration=configuration,
                clock=self.timing.clock,
                github=self.integration.github,
                threads=self.integration.threads,
                blocking=self.blocking,
                network=self.network,
                agent_lifecycle=self.integration.agent_execution_event,
                evaluation_diagnostics=self._evaluation_diagnostic,
                stateless_tool_servers=self._stateless_tool_servers(tool_context),
                commands=agent_runtime.commands,
                capture_commands=agent_runtime.capture_commands,
                # No round_label: this instance is built once for the run and
                # shared across every candidate's local-validation attempts,
                # so no single label identifies "one attempt" for it (see
                # CoreLocalValidationEvents.__init__). Its GATE_FINISHED
                # events stay ungrouped until this path carries its own
                # per-attempt identity to pass through here.
                local_validation_events=CoreLocalValidationEvents(self.integration.events),
                measurement_observer=CoreMeasurementEvents(self.integration.events, decisions),
                measurement_decisions=decisions,
                commit_observer=self.integration.core_commit_observer(
                    project.state.run_id, self.projector, self.plugin.id
                ),
                session_spec=agent_session_spec(
                    environments={
                        provider: agent.environment for provider, agent in agents.items()
                    },
                    specs=specs,
                    variables=scope.environment_variables,
                    configuration=configuration,
                ),
            ),
        )

    def _open_provider_agents(  # noqa: PLR0913  # lint-waiver: LW-948091 [PLR0913]; each argument is an independently owned resource the host already opened, and this reads them once per provider.
        self,
        factory: Callable[..., AgentClientProtocol],
        spec: AgentSpec,
        scope: AgentExecutionScope,
        session_store: DurableSessionStore,
        agent_runtime: WorkspaceRuntime,
        events: CoreAgentEventSink,
        ownership: ReleaseStack,
    ) -> _ProviderAgents:
        """Open the agent environment and client of one provider, released with ``ownership``.

        The client is bound to ``spec.provider``, so each role's own model and effort ride
        ``spec`` for the roles that run on it.
        """
        environment = scope.open_environment(
            AgentExecutionConfiguration(
                agent_id="core", spec=spec, reasoning_effort=spec.reasoning_effort
            )
        )
        ownership.callback(environment.close)
        client = factory(
            spec=spec,
            clock=self.timing.clock,
            session_store=session_store,
            backends=(
                {role.id: environment.backends["chat"] for role in self.plugin.agents}
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
        return _ProviderAgents(environment, client)

    def _serve_queue_status(
        self, resources: _PreparedRun, handle_root: Path, ownership: ReleaseStack
    ) -> Path:
        """Serve the run's read-only queue status to its agents; return the socket path.

        The service stops when ``ownership`` releases; its socket sits in the run's private
        socket directory, which every agent container mounts.
        """
        project = resources.project_resources
        socket_path = prepare_queue_status_socket(project.project.root, project.state.run_id)
        service = queue_status_service(
            queue_status_reader(
                resources.environment_resources.view,
                run_id=project.state.run_id,
                handle_root=handle_root,
                processes=self.integration.system.slurm_processes,
                clock=self.timing.clock,
            ),
            socket_path,
            network=self.network,
            threads=self.integration.threads,
            clock=self.timing.clock,
        )
        ownership.callback(service.close)
        service.start()
        return socket_path

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
            queue_wait_budget_seconds=config.evaluation.queue_wait_budget_seconds,
            queue_notice_interval_seconds=config.evaluation.queue_notice_interval_seconds,
            request_retries=config.evaluation.request_retries(),
            max_run_seconds=config.run.max_run_seconds,
            cancellation_bound_seconds=config.run.cancellation_bound_seconds,
            cleanup_bound_seconds=config.run.cleanup_bound_seconds,
            reconciliation_bound_seconds=config.run.reconciliation_bound_seconds,
            lifecycle=lifecycle,
            prompt_variables=visible_comparison_variables(self.comparisons),
        )

    def _evaluation_diagnostic(self, line: str) -> None:
        """Journal one agent evaluation call the run refused as busy."""
        self.integration.events.emit(
            CoreEventType.FRAMEWORK_WARNING,
            data=FrameworkWarningData(
                summary=line, source=FrameworkSource.LOOP, source_label="agent-evaluation"
            ),
        )

    def _semantic_executor(
        self,
        resources: _PreparedRun,
        workspaces: Workspaces,
        namespace: StateNamespace,
        stage_output: StageOutputOpener,
        *,
        ledger_identity: tuple[str, str],
    ) -> SlurmSemanticEvaluationExecutor | None:
        """Build the cluster's semantic execution adapter when the environment reports one."""
        cluster = resources.environment_resources.view.cluster_evaluation
        if cluster is None:
            return None
        clock = self.timing.clock
        return SlurmSemanticEvaluationExecutor(
            cluster.settings.config,
            cluster.settings.policy,
            cluster.plan,
            resources.evaluation_plan,
            workspaces,
            namespace,
            _slurm_handle_root(namespace),
            clock=clock,
            sleeper=clock,
            processes=self.integration.system.slurm_processes,
            ledger_identity=ledger_identity,
            stage_output=stage_output,
            root_proof=cluster.root_proof,
            control=self.integration.control,
        )

    async def stop_new_work(self) -> None:
        """Nothing to cancel at the moment of a stop: no run-owned service holds jobs here.

        Every core measurement, an agent's included, is a job of the run's evaluation
        executor. Core refuses new submissions once the stop is committed, and the executor
        cancels the jobs still on the cluster when ``close_core_services`` closes it as the
        run exits; with the operator's evaluation ledger, #2757, it cancels those no other run
        wants, unless the ledger policy keeps jobs on a stop
        (``tests/system/agent_evaluation/test_stops_on_stop.py``,
        ``libs/vs-sandbox/tests/test_slurm_shutdown_leaves_no_job.py``). The legacy
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

    workspaces: OwnedWorkspaces
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


@dataclass(frozen=True, slots=True)
class _ProviderAgents:
    """The agent environment and the client of the roles that run on one provider."""

    environment: AgentExecutionEnvironment
    client: AgentClientProtocol


def _provider_specs(base: AgentSpec, specs: Mapping[str, AgentSpec]) -> dict[str, AgentSpec]:
    """One client spec per provider the roles use, carrying those roles' models and efforts.

    A provider's client runs only its own roles, so a role's model belongs to the client of the
    role's provider. A run whose plugin declares no role still gets the run's own provider.
    """
    providers = dict.fromkeys(spec.provider for spec in specs.values()) or {base.provider: None}
    clients: dict[str, AgentSpec] = {}
    for provider in providers:
        mine = {role: spec for role, spec in specs.items() if spec.provider == provider}
        first = next(iter(mine.values()), None)
        clients[provider] = replace(
            base,
            provider=provider,
            model=base.model if provider == base.provider or first is None else first.model,
            role_models={role: spec.model for role, spec in mine.items() if spec.model},
            role_reasoning_efforts={
                role: spec.reasoning_effort for role, spec in mine.items() if spec.reasoning_effort
            },
        )
    return clients


def _candidate_agent_sandbox(runtime: WorkspaceRuntime, path: Path) -> DockerSandbox | None:
    """The container that mounts the candidate checked out at ``path``.

    ``None`` is the root workspace, which the role's own container serves; a
    path no live candidate owns raises ``AgentWorkspaceRouteError``.
    """
    return cast("DockerSandbox | None", runtime.workspaces.agent_sandbox_at(path))


def _require_supported_fallback(policy: QuotaPolicy, agent_specs: Mapping[str, AgentSpec]) -> None:
    """Reject a fallback a core run cannot honor, before the run starts.

    A core run fixes each role's provider when it starts (one agent client per provider), so
    it cannot open a session on another provider. A fallback is therefore refused rather than
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
        f"agent.quota.fallback_provider {policy.fallback.provider!r}: a run fixes each role's "
        "provider when it starts, so it cannot switch provider on a quota "
        "stop; remove agent.quota.fallback_provider and fallback_model (policy = 'fallback' "
        "needs them, so use 'pause', 'wait' or 'fail')"
    )
    raise ValueError(message)
