"""Compose every service of one core run from the resources the host already opened.

The host opens a run's workspaces, evaluation, agent client and Project once. This
module joins them with a plugin's ``CorePolicy`` into ``CoreServices``: the initial
core state, the executor bindings, and the durable locations the production run loop
needs. Nothing here runs a loop or decides policy, and every path comes from
``Project``. A resource that is missing, or a strategy that needs something the host
cannot serve, fails at composition with the name of what is missing, never mid-run.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from vibesys.run.evaluation_socket import prepare_evaluation_socket
from vs_agent.api import (
    AgentBackend,
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentTurnExecutor,
    ClientAgentSessions,
)
from vs_core.api import ContractError, SchemaRef
from vs_evaluation.api import PollingEvaluationExecutor as PollingEvaluation
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore, CorePlan, CoreRunContext, OwnedOperationResources
from vs_runtime.api.core import (
    EVALUATION_TOOL_ID,
    AgentEvaluationBridge,
    AgentEvaluationRecords,
    CombinedToolServers,
    CoreStartup,
    IgnoreCommits,
    IgnoreMeasurement,
    MeasurementDecisions,
    MeasurementServices,
    OperationPorts,
    OwedSubmissions,
    ProductionSessionResolver,
    ReceiptEvidenceLedger,
    ReceiptStore,
    ResolverInputs,
    ScopeWorkspaces,
    SessionServices,
    StatelessToolServers,
    StoreWorkspaceReceipts,
    build_operation_catalog,
    commit_of,
    core_bindings,
    new_core_state,
    production_owners,
    report_turns,
    revision_ref,
)
from vs_runtime.api.infrastructure import StateValidationPasses

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from vs_agent.api import AgentClientProtocol, AgentInvocationStore, AgentSpec
    from vs_core.api import CoreState, LifecycleCapability, RequestRetries, SearchDef
    from vs_github.api import GitHubClient
    from vs_mcp.api import ToolServerDescriptor
    from vs_project.api import ControlInbox, Project, StateNamespace, StateStore
    from vs_runtime.api import AgentRole, CaptureCommands, Commands, CorePolicy, RunFacts
    from vs_runtime.api.core import (
        CommitObserver,
        CoreRuntimeBindings,
        MeasurementObserver,
        OperationCatalog,
        SessionSpecFactory,
    )
    from vs_runtime.api.infrastructure import (
        AgentConfigurationResolver,
        AgentExecutionEnvironment,
        AgentExecutionLifecycleSink,
        LocalValidationEvents,
        RunControlChannel,
        RunEnvironmentView,
        RuntimeWorkspaces,
        TrustedEvaluationPlan,
    )
    from vs_sim.api import BlockingRunner, Network, SleepingClock, Threads


class CoreCompositionError(RuntimeError):
    """A core run cannot be composed; ``resource`` names what is missing or unsupported."""

    def __init__(self, resource: str, detail: str) -> None:
        """Name the resource so a caller and the operator can tell what to supply."""
        self.resource = resource
        super().__init__(f"core run composition: {resource}: {detail}")


class ClosableEvaluation(PollingEvaluation, Protocol):
    """A pollable evaluation executor the host owns and releases at shutdown."""

    async def close(self) -> None:
        """Settle provider tasks and release candidate worktrees."""
        ...


@dataclass(frozen=True, slots=True)
class CoreEnvironment:
    """The run's evaluation environment and product bounds, as facts the plan reads."""

    view: RunEnvironmentView
    evaluation_plan: TrustedEvaluationPlan
    evaluation_capacity: int
    queue_allowance_seconds: int
    observe_interval_seconds: int
    observe_backoff_cap_seconds: int
    queue_wait_budget_seconds: int
    queue_notice_interval_seconds: int
    request_retries: RequestRetries
    max_run_seconds: int | None
    cancellation_bound_seconds: int
    cleanup_bound_seconds: int
    reconciliation_bound_seconds: int
    lifecycle: frozenset[LifecycleCapability]
    """Lifecycle capabilities this host offers the strategy."""
    prompt_variables: Mapping[str, object] = field(default_factory=dict)
    """Data the host adds to the run's prompts (see ``CoreRunContext.prompt_variables``)."""


def ignore_lifecycle(event: object) -> None:
    """The lifecycle sink of a host that shows no agent progress."""
    del event


def ignore_diagnostic(line: str) -> None:
    """The diagnostic sink of a host that keeps no journal of refused agent calls."""
    del line


@dataclass(frozen=True, slots=True)
class CoreResources:
    """What the host already opened, as one value the composition reads."""

    run_id: str
    project: Project
    facts: RunFacts
    options: BaseModel
    workspaces: RuntimeWorkspaces
    evaluation: ClosableEvaluation
    """The evaluation executor, chosen by the wiring code for the run environment."""
    environment: CoreEnvironment
    roles: tuple[AgentRole, ...]
    role_models: Mapping[str, str]
    """The model the operator chose per role id where it differs from the run's default."""
    agent_turns: AgentTurnExecutor
    """Runs each role's keyed turns, on the provider that role's spec names."""
    invocation_slot: AgentInvocationStore
    configuration: AgentConfigurationResolver
    session_spec: SessionSpecFactory
    clock: SleepingClock
    """The run's clock: it places the deadline and later paces the loop on one timeline."""
    github: GitHubClient
    """Reaches GitHub, for operation owners that keep their records there."""
    threads: Threads
    """Locks the run's receipts and serves its agent evaluation calls."""
    blocking: BlockingRunner
    """Runs the request executors' synchronous calls off the event loop."""
    network: Network
    """Serves the in-turn agent evaluation socket."""
    measurement_observer: MeasurementObserver = field(default_factory=IgnoreMeasurement)
    measurement_decisions: MeasurementDecisions = field(default_factory=MeasurementDecisions)
    """Which decision each measurement answers; the same one names stage output by its gate."""
    """Hears each measurement stage start and end, for the host's event stream."""
    epoch: Callable[[], int | None] = lambda: None
    """Published writer identity; unjournaled hosts leave epoch selection to their runtime."""
    commit_observer: CommitObserver = field(default_factory=IgnoreCommits)
    """Hears each confirmed commit, for committed views and round events."""
    resuming: bool = False
    control: RunControlChannel | None = None
    agent_lifecycle: AgentExecutionLifecycleSink = ignore_lifecycle
    """Receives the start and end of each provider turn, for the host's event stream."""
    evaluation_diagnostics: Callable[[str], None] = ignore_diagnostic
    """Receives one line for each agent evaluation call the run refused as busy."""
    stateless_tool_servers: Mapping[str, ToolServerDescriptor] = field(default_factory=dict)
    """The server of each stateless tool a role may declare, keyed by tool id."""
    commands: Commands | None = None
    """Ordinary editor process execution in a run workspace, used by local validation."""
    capture_commands: CaptureCommands | None = None
    """Role-specific process execution used only by command-capturing operations."""
    local_validation_events: LocalValidationEvents | None = None
    """Hears each recipe's start and settled result, for the host's event stream."""


@dataclass(frozen=True, slots=True)
class CoreServices:
    """Everything the production core run loop needs, built once per host.

    ``state`` is the state of a run that has not started (the loop ignores it when the
    store already holds a recoverable envelope). ``bindings`` execute every request
    role, and ``registry`` inside them is the operation codec to hand the publication
    delivery. ``store`` is the run's durable record; ``publications`` and ``receipts``
    are its Project namespaces.
    """

    run_id: str
    search: SearchDef[Any]
    config: object
    """The config the search runs on."""
    state: CoreState
    bindings: CoreRuntimeBindings
    catalog: OperationCatalog
    store: StateStore
    publications: StateNamespace
    receipts: StateNamespace
    evaluation: ClosableEvaluation
    artifacts: ArtifactStore
    clock: SleepingClock
    agent_evaluation: AgentEvaluationBridge | None = None
    """The in-turn evaluation tool service, when the plan offers one; the run loop serves it."""
    profile_capture_withheld: bool = False
    """The plan would offer profile work, but the run environment cannot capture a profile."""
    """The operator force-stopped this run before (``record_stop_request``): it only ends."""
    control_inbox: ControlInbox | None = None
    """The run's durable control inbox, where ``vibesys control`` writes; the loop reads it."""

    async def close(self) -> None:
        """Release the evaluation executor; workspaces and the client close with the host."""
        await self.evaluation.close()


def build_core_services(policy: CorePolicy, resources: CoreResources) -> CoreServices:
    """Join one policy with the host's resources, or fail naming what is missing."""
    run_id = resources.run_id
    state_dir = resources.project.state
    receipts = state_dir.local_namespace(run_id, "receipts")
    publications = state_dir.portable_namespace(run_id, "publications")
    artifacts = ArtifactStore(state_dir.portable_namespace(run_id, "artifacts"))
    receipt_store = ReceiptStore(receipts, resources.threads)
    try:
        plan = policy.plan(_context(resources, artifacts))
        _validate_plan(plan)
        stateless = StatelessToolServers(resources.stateless_tool_servers)
        _refuse_unbridged_tools(
            resources.roles, bridged=plan.agent_evaluation is not None, stateless=stateless.tool_ids
        )
        evidence = ReceiptEvidenceLedger(receipt_store)
        bridge = _agent_bridge(plan, resources, receipt_store, evidence)
        catalog = _catalog(policy, plan, resources, artifacts, evidence)
        catalog.require_owned(plan.search.declaration)
        resolver = ProductionSessionResolver(
            ResolverInputs(
                control=resources.control,
                roles=resources.roles,
                schemas=plan.search.reply_schemas,
                workspaces=resources.workspaces,
                workspace_receipts=StoreWorkspaceReceipts(receipt_store),
                artifacts=artifacts,
                configuration=resources.configuration,
                session_spec=resources.session_spec,
                artifact_directories=policy.artifact_directories,
                tool_servers=CombinedToolServers(*(() if bridge is None else (bridge,)), stateless),
            )
        )
        state = _initial_state(plan, resources, catalog, suspendable=bridge is not None)
    except ContractError as error:
        raise CoreCompositionError(".".join(map(str, error.path)), error.detail) from error
    bindings = core_bindings(
        receipts=receipts,
        threads=resources.threads,
        blocking=resources.blocking,
        workspaces=resources.workspaces,
        measurement=MeasurementServices(
            resources.evaluation,
            observer=resources.measurement_observer,
            decisions=resources.measurement_decisions,
            control=resources.control,
        ),
        sessions=SessionServices(_agent_sessions(resources), resolver, yields=bridge),
        operations=catalog,
    )
    return CoreServices(
        run_id=run_id,
        search=plan.search,
        config=plan.config,
        state=state,
        bindings=replace(bindings, commits=resources.commit_observer, epoch=resources.epoch),
        catalog=catalog,
        store=resources.project.state_store(run_id),
        publications=publications,
        receipts=receipts,
        evaluation=resources.evaluation,
        artifacts=artifacts,
        clock=resources.clock,
        agent_evaluation=bridge,
        profile_capture_withheld=plan.profile_capture_withheld,
        control_inbox=resources.project.state.control_inbox(run_id),
    )


def _context(resources: CoreResources, artifacts: ArtifactStore) -> CoreRunContext:
    """The facts of this run the policy's plan reads."""
    commit = resources.workspaces.root.trusted_input_baseline
    if commit is None:
        detail = "the run's root workspace has no baseline revision to measure against"
        resource = "root workspace trusted input baseline"
        raise CoreCompositionError(resource, detail)
    environment = resources.environment
    return CoreRunContext(
        run_id=resources.run_id,
        options=resources.options,
        facts=resources.facts,
        baseline=revision_ref(commit),
        evaluation_plan=environment.evaluation_plan,
        environment=environment.view,
        artifacts=artifacts,
        evaluation_capacity=environment.evaluation_capacity,
        queue_allowance_seconds=environment.queue_allowance_seconds,
        observe_interval_seconds=environment.observe_interval_seconds,
        observe_backoff_cap_seconds=environment.observe_backoff_cap_seconds,
        queue_wait_budget_seconds=environment.queue_wait_budget_seconds,
        queue_notice_interval_seconds=environment.queue_notice_interval_seconds,
        request_retries=environment.request_retries,
        max_run_seconds=environment.max_run_seconds,
        cancellation_bound_seconds=environment.cancellation_bound_seconds,
        cleanup_bound_seconds=environment.cleanup_bound_seconds,
        reconciliation_bound_seconds=environment.reconciliation_bound_seconds,
        role_models=resources.role_models,
        prompt_variables=environment.prompt_variables,
    )


def _validate_plan(plan: CorePlan) -> None:
    """Reject a malformed plan naming the key; dataclass annotations are not enforced."""
    for schema, model in plan.search.reply_schemas.items():
        if not isinstance(schema, SchemaRef):
            raise CoreCompositionError(
                "plan.search.reply_schemas", f"key {schema!r} must be a SchemaRef"
            )
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise CoreCompositionError(
                f"plan.search.reply_schemas[{schema.name}]", "must be a BaseModel class"
            )
    if not plan.deadline_seconds > 0:
        raise CoreCompositionError(
            "plan.deadline_seconds", f"must be positive, got {plan.deadline_seconds!r}"
        )


def _initial_state(
    plan: CorePlan, resources: CoreResources, catalog: OperationCatalog, *, suspendable: bool
) -> CoreState:
    """The state of a run that has not started.

    The run's deadline is its budget counted from now on the run clock's timeline (seconds
    since the epoch), the same timeline the loop reads, so a stored deadline stays valid
    for every host that resumes the run.
    """
    return new_core_state(
        resources.run_id,
        plan.facts,
        plan.search.declaration,
        offered=catalog,
        startup=CoreStartup(
            deadline_at=resources.clock.now() + plan.deadline_seconds,
            duration_seconds=plan.deadline_seconds,
            limits=plan.limits,
            lifecycle=_lifecycle(resources, suspendable=suspendable),
            requirements=plan.requirements,
        ),
    )


def _lifecycle(resources: CoreResources, *, suspendable: bool) -> frozenset[LifecycleCapability]:
    """The lifecycle capabilities the host offers; a bridged tool lets a turn suspend."""
    offered = resources.environment.lifecycle
    if not suspendable:
        return offered
    suspend: frozenset[LifecycleCapability] = frozenset({"suspend"})
    return offered | suspend


def _refuse_unbridged_tools(
    roles: Iterable[AgentRole], *, bridged: bool, stateless: frozenset[str]
) -> None:
    """Admit only the bridged evaluation tool and stateless tools that are served.

    Any other tool may record into run state, so handing one to a core session could leave
    the run with two writers. The evaluation tool is bridged to core events when the plan
    carries an ``AgentEvaluationPolicy``. A tool declared stateless is admitted when the host
    provides its server; one nobody serves is refused rather than silently left out.
    """
    for role in roles:
        for tool in role.extra_tools:
            if bridged and tool.id == EVALUATION_TOOL_ID:
                continue
            resource = f"agent role {role.id!r} tool {tool.id!r}"
            if tool.stateless:
                if tool.id in stateless:
                    continue
                detail = "the tool is declared stateless but the host provided no server for it"
                raise CoreCompositionError(resource, detail)
            detail = (
                "agent tools that may write run state are not bridged to core requests, "
                "so a core run cannot give this role the tool"
            )
            raise CoreCompositionError(resource, detail)


def _agent_bridge(
    plan: CorePlan,
    resources: CoreResources,
    receipts: ReceiptStore,
    evidence: ReceiptEvidenceLedger,
) -> AgentEvaluationBridge | None:
    """The in-turn evaluation tool service, when the plan offers the tool."""
    if plan.agent_evaluation is None:
        return None
    return AgentEvaluationBridge(
        prepare_evaluation_socket(resources.project.root, resources.run_id),
        plan.agent_evaluation,
        ScopeWorkspaces(resources.workspaces, StoreWorkspaceReceipts(receipts)),
        AgentEvaluationRecords(OwedSubmissions(receipts), evidence),
        resources.evaluation_diagnostics,
        network=resources.network,
        threads=resources.threads,
    )


def _catalog(
    policy: CorePolicy,
    plan: CorePlan,
    resources: CoreResources,
    artifacts: ArtifactStore,
    evidence: ReceiptEvidenceLedger,
) -> OperationCatalog:
    registrations = {item.role: item.registration for item in policy.operations}
    ports = OperationPorts(
        renderer=TemplateRenderer(
            policy.prompt_templates, fallback_roots=policy.prompt_fallback_roots
        ),
        artifacts=artifacts,
        workspaces=resources.workspaces,
        ledger=resources.workspaces,
        evidence=evidence,
        commit_of=commit_of,
        retention_label=policy.retention_label,
        variables=plan.prompt_variables,
        commands=resources.commands,
        capture_commands=resources.capture_commands,
        replies=plan.search.reply_schemas,
        validation_passes=StateValidationPasses(
            resources.project.state.local_namespace(resources.run_id, "validation")
        ),
        local_validation_events=resources.local_validation_events,
    )
    owned = (
        ()
        if policy.owned_operations is None
        else policy.owned_operations(
            OwnedOperationResources(
                run_id=resources.run_id,
                project=resources.project,
                options=resources.options,
                variables=plan.prompt_variables,
                templates=policy.prompt_templates,
                clock=resources.clock,
                threads=resources.threads,
                github=resources.github,
            )
        )
    )
    return build_operation_catalog(
        registrations, production_owners(registrations, ports), extra=owned
    )


def turn_executor(client: AgentClientProtocol) -> AgentTurnExecutor:
    """``client`` as the keyed-turn executor a durable core session needs.

    Raises :class:`CoreCompositionError` for a client that is not one.
    """
    if not isinstance(client, AgentTurnExecutor):
        detail = "a durable core session needs a client that implements AgentTurnExecutor"
        resource = "agent client"
        raise CoreCompositionError(resource, detail)
    return client


def _agent_sessions(resources: CoreResources) -> ClientAgentSessions:
    reported = report_turns(
        resources.agent_turns, resources.agent_lifecycle, control=resources.control
    )
    return ClientAgentSessions(reported, resources.invocation_slot, threads=resources.threads)


def agent_session_spec(
    *,
    environments: Mapping[str, AgentExecutionEnvironment],
    specs: Mapping[str, AgentSpec],
    variables: Callable[[], Mapping[str, str]],
    configuration: AgentConfigurationResolver,
) -> SessionSpecFactory:
    """Provider session configuration for each role, from the agent environment of its provider.

    A role's own mounts (the evaluation tool socket, profiler files) come from its
    `configuration`; without them a confined agent cannot reach the tools it is offered.
    """

    def spec_for(role: AgentRole, workspace: Path) -> AgentSessionSpec:
        spec = specs[role.id]
        environment = environments[spec.provider]
        env = {} if environment.use_docker else dict(variables())
        return AgentSessionSpec(
            role=role.id,
            provider=spec.provider,
            workspace=workspace,
            policy=AgentExecutionPolicy(
                project_paths=environment.project_path_policy,
                host_resources=(*environment.host_resources, *configuration(role).resources),
                require_enforcement=not environment.use_docker,
                containerized=environment.use_docker,
            ),
            model=None if spec.backend is AgentBackend.SCRIPTED else spec.model,
            scripted=spec.backend is AgentBackend.SCRIPTED,
            mcp_servers=(),
            skills=environment.skill_source_dirs,
            environment=tuple(sorted(env.items())),
            reasoning_effort=spec.role_reasoning_efforts.get(role.id, spec.reasoning_effort),
        )

    return spec_for


__all__ = [
    "CoreCompositionError",
    "CoreEnvironment",
    "CoreResources",
    "CoreServices",
    "agent_session_spec",
    "build_core_services",
    "turn_executor",
]
