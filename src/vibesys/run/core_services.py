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
    from vs_project.api import Project, StateNamespace, StateStore
    from vs_runtime.api import AgentRole, Commands, CorePolicy, RunFacts
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
    request_retries: RequestRetries
    max_run_seconds: int | None
    cancellation_bound_seconds: int
    cleanup_bound_seconds: int
    reconciliation_bound_seconds: int
    lifecycle: frozenset[LifecycleCapability]
    """Lifecycle capabilities this host offers the strategy."""


def ignore_lifecycle(event: object) -> None:
    """The lifecycle sink of a host that shows no agent progress."""
    del event


def ignore_diagnostic(line: str) -> None:
    """The diagnostic sink of a host that keeps no journal of refused agent calls."""
    del line


@dataclass(frozen=True, slots=True)
class CoreAgentResources:
    """Agent-session resources, absent as a group from a zero-agent run."""

    client: AgentClientProtocol
    invocation_slot: AgentInvocationStore
    configuration: AgentConfigurationResolver
    session_spec: SessionSpecFactory
    lifecycle: AgentExecutionLifecycleSink = ignore_lifecycle
    """Receives provider-turn lifecycle observations for the host's event stream."""


@dataclass(frozen=True, slots=True)
class CoreResources:
    """What the host already opened, as one value the composition reads.

    ``agents`` is one optional capability bundle: a policy with no declared
    roles composes without an agent environment, provider client, or invocation
    journal. A policy with roles must supply the whole bundle.
    """

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
    agents: CoreAgentResources | None
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
    """Hears each measurement stage start and end, for the host's event stream."""
    commit_observer: CommitObserver = field(default_factory=IgnoreCommits)
    """Hears each confirmed commit, for committed views and round events."""
    evaluation_diagnostics: Callable[[str], None] = ignore_diagnostic
    """Receives one line for each agent evaluation call the run refused as busy."""
    stateless_tool_servers: Mapping[str, ToolServerDescriptor] = field(default_factory=dict)
    """The server of each stateless tool a role may declare, keyed by tool id."""
    commands: Commands | None = None
    """Sandboxed process execution in a run workspace, which a command-capturing operation uses."""

    def __post_init__(self) -> None:
        """Reject a role declaration whose session capability bundle disagrees."""
        if not self.roles and self.agents is not None:
            detail = "a run with no declared agent roles must not open them"
            error = CoreCompositionError("agent resources", detail)
            raise error
        if self.roles and self.agents is None:
            detail = "a run with declared agent roles needs session resources"
            error = CoreCompositionError("agent resources", detail)
            raise error


@dataclass(frozen=True, slots=True)
class _SessionComposition:
    """Shared resources for resolving and executing the run's declared roles."""

    receipts: ReceiptStore
    artifacts: ArtifactStore
    bridge: AgentEvaluationBridge | None
    stateless: StatelessToolServers


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
        sessions = _session_services(
            policy,
            plan,
            resources,
            _SessionComposition(receipt_store, artifacts, bridge, stateless),
        )
        state = _initial_state(plan, resources, catalog, suspendable=bridge is not None)
    except ContractError as error:
        raise CoreCompositionError(".".join(map(str, error.path)), error.detail) from error
    bindings = core_bindings(
        receipts=receipts,
        threads=resources.threads,
        blocking=resources.blocking,
        workspaces=resources.workspaces,
        measurement=MeasurementServices(resources.evaluation, resources.measurement_observer),
        sessions=sessions,
        operations=catalog,
    )
    return CoreServices(
        run_id=run_id,
        search=plan.search,
        config=plan.config,
        state=state,
        bindings=replace(bindings, commits=resources.commit_observer),
        catalog=catalog,
        store=resources.project.state_store(run_id),
        publications=publications,
        receipts=receipts,
        evaluation=resources.evaluation,
        artifacts=artifacts,
        clock=resources.clock,
        agent_evaluation=bridge,
        profile_capture_withheld=plan.profile_capture_withheld,
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
        request_retries=environment.request_retries,
        max_run_seconds=environment.max_run_seconds,
        cancellation_bound_seconds=environment.cancellation_bound_seconds,
        cleanup_bound_seconds=environment.cleanup_bound_seconds,
        reconciliation_bound_seconds=environment.reconciliation_bound_seconds,
        role_models=resources.role_models,
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
        replies=plan.search.reply_schemas,
        validation_passes=StateValidationPasses(
            resources.project.state.local_namespace(resources.run_id, "validation")
        ),
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


def _session_services(
    policy: CorePolicy,
    plan: CorePlan,
    resources: CoreResources,
    composition: _SessionComposition,
) -> SessionServices | None:
    """Bind declared agent roles, or make the session role explicitly unavailable."""
    agent_resources = resources.agents
    if agent_resources is None:
        return None
    resolver = ProductionSessionResolver(
        ResolverInputs(
            roles=resources.roles,
            schemas=plan.search.reply_schemas,
            workspaces=resources.workspaces,
            workspace_receipts=StoreWorkspaceReceipts(composition.receipts),
            artifacts=composition.artifacts,
            configuration=agent_resources.configuration,
            session_spec=agent_resources.session_spec,
            artifact_directories=policy.artifact_directories,
            tool_servers=CombinedToolServers(
                *(() if composition.bridge is None else (composition.bridge,)),
                composition.stateless,
            ),
        )
    )
    return SessionServices(
        _agent_sessions(agent_resources, resources.threads),
        resolver,
        yields=composition.bridge,
    )


def _agent_sessions(resources: CoreAgentResources, threads: Threads) -> ClientAgentSessions:
    client = resources.client
    if not isinstance(client, AgentTurnExecutor):
        detail = "a durable core session needs a client that implements AgentTurnExecutor"
        resource = "agent client"
        raise CoreCompositionError(resource, detail)
    reported = report_turns(client, resources.lifecycle)
    return ClientAgentSessions(reported, resources.invocation_slot, threads=threads)


def agent_session_spec(
    *,
    client: AgentClientProtocol,
    environment: AgentExecutionEnvironment,
    specs: Mapping[str, AgentSpec],
    variables: Callable[[], Mapping[str, str]],
    configuration: AgentConfigurationResolver,
) -> SessionSpecFactory:
    """Provider session configuration for each role, from the run's one agent environment.

    A role's own mounts (the evaluation tool socket, profiler files) come from its
    `configuration`; without them a confined agent cannot reach the tools it is offered.
    """

    def spec_for(role: AgentRole, workspace: Path) -> AgentSessionSpec:
        spec = specs[role.id]
        env = {} if environment.use_docker else dict(variables())
        return AgentSessionSpec(
            role=role.id,
            provider=client.provider or spec.provider,
            workspace=workspace,
            policy=AgentExecutionPolicy(
                project_paths=environment.project_path_policy,
                host_resources=(*environment.host_resources, *configuration(role).resources),
                require_enforcement=not environment.use_docker,
                containerized=environment.use_docker,
            ),
            model=client.model_for_kind(role.id),
            mcp_servers=(),
            skills=environment.skill_source_dirs,
            environment=tuple(sorted(env.items())),
            reasoning_effort=spec.role_reasoning_efforts.get(role.id, spec.reasoning_effort),
        )

    return spec_for


__all__ = [
    "CoreAgentResources",
    "CoreCompositionError",
    "CoreEnvironment",
    "CoreResources",
    "CoreServices",
    "agent_session_spec",
    "build_core_services",
]
