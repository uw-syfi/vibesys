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

from vs_agent.api import (
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentTurnExecutor,
    ClientAgentSessions,
)
from vs_core.api import Capabilities, ContractError
from vs_evaluation.api import (
    EvaluationState,
    ExecutorPoll,
    PollPhase,
)
from vs_evaluation.api import (
    PollingEvaluationExecutor as PollingEvaluation,
)
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore, CoreRunContext, PollingEvaluationExecutor
from vs_runtime.api.core import (
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

    from pydantic import BaseModel

    from vs_agent.api import AgentClientProtocol, AgentInvocationStore, AgentSpec
    from vs_core.api import CoreState, LifecycleCapability, Strategy
    from vs_project.api import Project, StateNamespace, StateStore
    from vs_runtime.api import AgentRole, CorePolicy, RunFacts
    from vs_runtime.api.core import CoreRuntimeBindings, OperationCatalog, SessionSpecFactory
    from vs_runtime.api.infrastructure import (
        AgentConfigurationResolver,
        AgentExecutionEnvironment,
        RuntimeWorkspaces,
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
    """The run's evaluation environment, as facts the policy's factories read."""

    evaluator_digest: str
    workload_digest: str
    environment_digest: str
    evaluation_capacity: int
    queue_allowance_seconds: float
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

    async def close(self) -> None:
        """Release the evaluation executor; workspaces and the client close with the host."""
        await self.evaluation.close()


_ENDED = frozenset(
    {
        EvaluationState.SUCCEEDED,
        EvaluationState.FAILED,
        EvaluationState.CANCELED,
        EvaluationState.SUPERSEDED,
    }
)


class LocalPollingEvaluationExecutor(PollingEvaluationExecutor):
    """The local executor with the pure ``poll`` the core measurement requests call.

    Delete this class once ``vs_runtime.PollingEvaluationExecutor`` implements ``poll``
    itself (SW-3 handoff item for the executor owner); until then the core path would
    otherwise see every local poll as unknown.
    """

    async def poll(self, handle_id: str) -> ExecutorPoll:
        """Read process-local evidence once; never submit, resume or cancel."""
        observed = await self.inspect_only(handle_id)
        if observed is None:
            return ExecutorPoll(phase=PollPhase.UNSUBMITTED)
        if observed.state in _ENDED:
            return ExecutorPoll(phase=PollPhase.ENDED, terminal=observed)
        phase = PollPhase.QUEUED if observed.state is EvaluationState.QUEUED else PollPhase.RUNNING
        return ExecutorPoll(phase=phase)


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
        strategy = policy.strategy(resources.options)
        catalog = _catalog(policy, resources, artifacts, ReceiptEvidenceLedger(receipt_store))
        catalog.require_owned(strategy.declaration)
        resolver = ProductionSessionResolver(
            ResolverInputs(
                roles=resources.roles,
                schemas=policy.reply_schemas,
                workspaces=resources.workspaces,
                workspace_receipts=StoreWorkspaceReceipts(receipt_store),
                artifacts=artifacts,
                configuration=resources.configuration,
                session_spec=resources.session_spec,
                artifact_directories=policy.artifact_directories,
            )
        )
        state = _initial_state(policy, resources, strategy, catalog)
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
        strategy=strategy,
        state=state,
        bindings=bindings,
        catalog=catalog,
        store=resources.project.state_store(run_id),
        publications=publications,
        receipts=receipts,
        evaluation=resources.evaluation,
    )


def _initial_state(
    policy: CorePolicy,
    resources: CoreResources,
    strategy: Strategy[Any],
    catalog: OperationCatalog,
) -> CoreState:
    """The state of a run that has not started, from the policy's per-run factories."""
    commit = resources.workspaces.root.trusted_input_baseline
    if commit is None:
        detail = "the run's root workspace has no baseline revision to measure against"
        resource = "root workspace trusted input baseline"
        raise CoreCompositionError(resource, detail)
    environment = resources.environment
    context = CoreRunContext(
        run_id=resources.run_id,
        options=resources.options,
        facts=resources.facts,
        baseline=revision_ref(commit),
        evaluator_digest=environment.evaluator_digest,
        workload_digest=environment.workload_digest,
        environment_digest=environment.environment_digest,
        evaluation_capacity=environment.evaluation_capacity,
        queue_allowance_seconds=environment.queue_allowance_seconds,
    )
    state = new_core_state(
        resources.run_id,
        policy.run_facts(context),
        strategy.declaration,
        offered=Capabilities(
            lifecycle=environment.lifecycle, operations=catalog.offered_operations
        ),
        deadline_at=policy.deadline_at(context),
    )
    # ``new_core_state`` starts from core's default limits; the policy's bound replaces them.
    return state.model_copy(
        update={"run": state.run.model_copy(update={"limits": policy.limits(context)})}
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
    "LocalPollingEvaluationExecutor",
    "agent_session_spec",
    "build_core_services",
]
