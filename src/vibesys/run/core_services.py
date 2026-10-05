"""Compose every service of one core run from the resources the host already opened.

The host opens a run's workspaces, evaluation, agent client and Project once. This
module joins them with a plugin's ``CorePolicy`` into ``CoreServices``: the initial
core state, the executor bindings, and the durable locations the production run loop
needs. Nothing here runs a loop or decides policy, and every path comes from
``Project``. A resource that is missing, or a strategy that needs something the host
cannot serve, fails at composition with the name of what is missing, never mid-run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from vs_agent.api import (
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentTurnExecutor,
    ClientAgentSessions,
)
from vs_core.api import ContractError, SchemaRef
from vs_evaluation.api import PollingEvaluationExecutor as PollingEvaluation
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore, CorePlan, CoreRunContext
from vs_runtime.api.core import (
    CoreStartup,
    OperationPorts,
    ProductionSessionResolver,
    ReceiptEvidenceLedger,
    ReceiptStore,
    ResolverInputs,
    SessionServices,
    StoreWorkspaceReceipts,
    build_operation_catalog,
    commit_of,
    core_bindings,
    new_core_state,
    production_owners,
    revision_ref,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from vs_agent.api import AgentClientProtocol, AgentInvocationStore, AgentSpec
    from vs_core.api import CoreState, LifecycleCapability, Strategy
    from vs_project.api import Project, StateNamespace, StateStore
    from vs_runtime.api import AgentRole, CorePolicy, RunFacts
    from vs_runtime.api.core import (
        CoreRuntimeBindings,
        OperationCatalog,
        RunClock,
        SessionSpecFactory,
    )
    from vs_runtime.api.infrastructure import (
        AgentConfigurationResolver,
        AgentExecutionEnvironment,
        RunEnvironmentView,
        RuntimeWorkspaces,
        TrustedEvaluationPlan,
    )


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
    max_run_seconds: int | None
    lifecycle: frozenset[LifecycleCapability]
    """Lifecycle capabilities this host offers the strategy."""


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
    agent_client: AgentClientProtocol
    invocation_slot: AgentInvocationStore
    configuration: AgentConfigurationResolver
    session_spec: SessionSpecFactory
    clock: RunClock
    """The run's clock: it places the deadline and later paces the loop on one timeline."""


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
    strategy: Strategy[Any]
    state: CoreState
    bindings: CoreRuntimeBindings
    catalog: OperationCatalog
    store: StateStore
    publications: StateNamespace
    receipts: StateNamespace
    evaluation: ClosableEvaluation
    artifacts: ArtifactStore
    clock: RunClock

    async def close(self) -> None:
        """Release the evaluation executor; workspaces and the client close with the host."""
        await self.evaluation.close()


def build_core_services(policy: CorePolicy, resources: CoreResources) -> CoreServices:
    """Join one policy with the host's resources, or fail naming what is missing."""
    _refuse_unbridged_tools(resources.roles)
    run_id = resources.run_id
    state_dir = resources.project.state
    receipts = state_dir.local_namespace(run_id, "receipts")
    publications = state_dir.portable_namespace(run_id, "publications")
    artifacts = ArtifactStore(state_dir.portable_namespace(run_id, "artifacts"))
    receipt_store = ReceiptStore(receipts)
    try:
        plan = policy.plan(_context(resources, artifacts))
        _validate_plan(plan)
        catalog = _catalog(policy, plan, resources, artifacts, ReceiptEvidenceLedger(receipt_store))
        catalog.require_owned(plan.strategy.declaration)
        resolver = ProductionSessionResolver(
            ResolverInputs(
                roles=resources.roles,
                schemas=plan.reply_schemas,
                workspaces=resources.workspaces,
                workspace_receipts=StoreWorkspaceReceipts(receipt_store),
                artifacts=artifacts,
                configuration=resources.configuration,
                session_spec=resources.session_spec,
                artifact_directories=policy.artifact_directories,
            )
        )
        state = _initial_state(plan, resources, catalog)
    except ContractError as error:
        raise CoreCompositionError(".".join(map(str, error.path)), error.detail) from error
    bindings = core_bindings(
        receipts=receipts,
        workspaces=resources.workspaces,
        evaluation=resources.evaluation,
        sessions=SessionServices(_agent_sessions(resources), resolver),
        operations=catalog,
    )
    return CoreServices(
        run_id=run_id,
        strategy=plan.strategy,
        state=state,
        bindings=bindings,
        catalog=catalog,
        store=resources.project.state_store(run_id),
        publications=publications,
        receipts=receipts,
        evaluation=resources.evaluation,
        artifacts=artifacts,
        clock=resources.clock,
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
        max_run_seconds=environment.max_run_seconds,
    )


def _validate_plan(plan: CorePlan) -> None:
    """Reject a malformed plan naming the key; dataclass annotations are not enforced."""
    for schema, model in plan.reply_schemas.items():
        if not isinstance(schema, SchemaRef):
            raise CoreCompositionError("plan.reply_schemas", f"key {schema!r} must be a SchemaRef")
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise CoreCompositionError(
                f"plan.reply_schemas[{schema.name}]", "must be a BaseModel class"
            )
    if not plan.deadline_seconds > 0:
        raise CoreCompositionError(
            "plan.deadline_seconds", f"must be positive, got {plan.deadline_seconds!r}"
        )


def _initial_state(
    plan: CorePlan, resources: CoreResources, catalog: OperationCatalog
) -> CoreState:
    """The state of a run that has not started.

    The run's deadline is its budget counted from now on the run clock's timeline (seconds
    since the epoch), the same timeline the loop reads, so a stored deadline stays valid
    for every host that resumes the run.
    """
    return new_core_state(
        resources.run_id,
        plan.facts,
        plan.strategy.declaration,
        offered=catalog,
        startup=CoreStartup(
            deadline_at=resources.clock.now() + plan.deadline_seconds,
            limits=plan.limits,
            lifecycle=resources.environment.lifecycle,
            requirements=plan.requirements,
        ),
    )


def _refuse_unbridged_tools(roles: Iterable[AgentRole]) -> None:
    """Agent tools are not served to core sessions yet (the evaluation-tool bridge).

    An agent tool talks to a legacy service that records into legacy state, so handing
    one to a core session would leave the run with two writers. Refusing here is explicit;
    the bridge lifts it.
    """
    for role in roles:
        for tool in role.extra_tools:
            detail = (
                "agent tools are not yet bridged to core requests, so a core run "
                "cannot give this role the tool"
            )
            resource = f"agent role {role.id!r} tool {tool.id!r}"
            raise CoreCompositionError(resource, detail)


def _catalog(
    policy: CorePolicy,
    plan: CorePlan,
    resources: CoreResources,
    artifacts: ArtifactStore,
    evidence: ReceiptEvidenceLedger,
) -> OperationCatalog:
    registrations = {item.role: item.registration for item in policy.operations}
    ports = OperationPorts(
        renderer=TemplateRenderer(policy.prompt_templates),
        artifacts=artifacts,
        workspaces=resources.workspaces,
        ledger=resources.workspaces,
        evidence=evidence,
        commit_of=commit_of,
        retention_label=policy.retention_label,
        variables=plan.prompt_variables,
    )
    return build_operation_catalog(registrations, production_owners(registrations, ports))


def _agent_sessions(resources: CoreResources) -> ClientAgentSessions:
    client = resources.agent_client
    if not isinstance(client, AgentTurnExecutor):
        detail = "a durable core session needs a client that implements AgentTurnExecutor"
        resource = "agent client"
        raise CoreCompositionError(resource, detail)
    return ClientAgentSessions(client, resources.invocation_slot)


def agent_session_spec(
    *,
    client: AgentClientProtocol,
    environment: AgentExecutionEnvironment,
    specs: Mapping[str, AgentSpec],
    variables: Callable[[], Mapping[str, str]],
) -> SessionSpecFactory:
    """Provider session configuration for each role, from the run's one agent environment."""

    def spec_for(role: AgentRole, workspace: Path) -> AgentSessionSpec:
        spec = specs[role.id]
        env = {} if environment.use_docker else dict(variables())
        return AgentSessionSpec(
            role=role.id,
            provider=client.provider or spec.provider,
            workspace=workspace,
            policy=AgentExecutionPolicy(
                project_paths=environment.project_path_policy,
                host_resources=environment.host_resources,
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
    "CoreCompositionError",
    "CoreEnvironment",
    "CoreResources",
    "CoreServices",
    "agent_session_spec",
    "build_core_services",
]
