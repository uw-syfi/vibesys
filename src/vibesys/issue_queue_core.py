"""Assemble the issue-queue core policy from a run's resolved resources.

The pure policy lives in `orchestration/issue_queue/core_policy`. This module does the effects
it cannot: store the evaluation recipe, read the evaluation identity, open the issue tracker
the run selected, bind the strategy's operation roles to the runtime's production owners, and
declare the agent roles a core run needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.core_roles import (
    CONTINUATION,
    MEASURING_READER,
    MEASURING_WRITER,
    RoleKind,
    core_role,
)
from vibesys.core_wiring import (
    ResolvedRun,
    agent_evaluation_policy,
    evaluation_inputs,
    operation_catalog,
    require_candidate_sandboxes,
)
from vibesys.issue_queue_board import board_entries
from vibesys.orchestration.issue_queue.core_policy.api import (
    IssueQueueCorePolicy,
    PolicyInputs,
    RunBounds,
    build_core_policy,
    project_strategy_state,
    prompts,
)
from vibesys.orchestration.issue_queue.models import IssueQueueOptions
from vibesys.orchestration.issue_queue.strategy.api import (
    OPERATIONS,
    STATE_SCHEMA,
    IssueQueueStrategyState,
    Role,
    measurement_recipe,
    plain_operation_registry,
    role_id,
)
from vibesys.plugin_registration import OrchestrationRegistration, RuntimeRecordProjector
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentTool,
    CoreOperation,
    CorePlan,
    CorePolicy,
    OrchestrationPlugin,
    WorkspaceAccess,
)
from vs_runtime.api.core import (
    OperationCatalog,
    OperationEntry,
    OperationPorts,
    RuntimeRecord,
    operation_roles,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import CoreRunContext

_PLUGIN_ID = STATE_SCHEMA.name.removesuffix(".strategy-state")
# The label of the snapshot that retains a candidate.
RETENTION_LABEL = "verified"
_RENDERER = TemplateRenderer(Path(prompts.__file__).parent)


def _role(role: Role, template: str, kind: RoleKind) -> AgentRole:
    return core_role(
        _RENDERER, role_id=role_id(role.value).root, system_template=template, kind=kind
    )


IMPLEMENTER = _role(Role.IMPLEMENTER, "implementer_system_prompt.j2", MEASURING_WRITER)
JUDGE = _role(Role.JUDGE, "judge_system_prompt.j2", MEASURING_READER)
# The evaluator starts servers and writes its measurements in its own checkout, which is discarded
# when its attempt settles; it needs no evaluation tool. It reads captures through the stateless
# profiler tool, which holds no run state (so it is no second writer beside core) and which the
# host leaves out of the role in a run whose selected profiler is `none`.
PERF_EVALUATOR = _role(
    Role.PERF,
    "performance_system_prompt.j2",
    RoleKind(
        WorkspaceAccess.READ_WRITE,
        CONTINUATION | {AgentCapability.MCP_SERVERS},
        (AgentTool(id="profiler", stateless=True),),
    ),
)
CORE_ROLES = (IMPLEMENTER, JUDGE, PERF_EVALUATOR)


def resolve_core_policy(
    options: IssueQueueOptions, bounds: RunBounds, run: ResolvedRun
) -> IssueQueueCorePolicy:
    """The core policy of one run: stores the evaluation recipe, then builds the pure policy."""
    evaluation = evaluation_inputs(run)
    return build_core_policy(
        options,
        bounds,
        PolicyInputs(
            objective=run.facts.objective,
            baseline_commit=run.baseline_commit,
            evaluator_digest=evaluation.evaluator_digest,
            workload_digest=evaluation.workload_digest,
            environment_digest=evaluation.environment_digest,
            recipe=evaluation.recipe,
            accuracy_configured=evaluation.accuracy_configured,
            benchmark_configured=evaluation.benchmark_configured,
            accuracy_seconds=_whole(evaluation.accuracy_seconds),
            benchmark_seconds=_whole(evaluation.benchmark_seconds),
            profile_seconds=_whole(evaluation.profile_seconds),
        ),
    )


def _whole(seconds: float | None) -> int | None:
    return None if seconds is None else max(1, round(seconds))


def issue_queue_operation_catalog(
    ports: OperationPorts, board: tuple[OperationEntry, ...]
) -> OperationCatalog:
    """The operations the strategy requires: the kit's roles from their owners, and the board's."""
    catalog = operation_catalog(OPERATIONS, ports)
    return OperationCatalog(plain_operation_registry(), (*catalog.entries, *board))


def issue_queue_projector() -> RuntimeRecordProjector[IssueQueueStrategyState]:
    """Read an issue-queue run's strategy state from its committed runtime record."""
    return RuntimeRecordProjector(
        plugin_id=_PLUGIN_ID,
        state_type=IssueQueueStrategyState,
        record_type=RuntimeRecord[IssueQueueStrategyState],
        operations=plain_operation_registry(),
        project=lambda state, revision: project_strategy_state(state, experiment_revision=revision),
    )


def _core_plan(context: CoreRunContext) -> CorePlan:
    """The plan of one run: the policy resolved from what the host opened for it."""
    require_candidate_sandboxes(context.environment, loop="plain")
    options = IssueQueueOptions.model_validate(context.options)
    commit = context.baseline.git_commit
    if commit is None:
        message = "the issue queue core policy needs a Git baseline revision"
        raise ValueError(message)
    bounds = RunBounds(
        queue_allowance_seconds=context.queue_allowance_seconds,
        observe_interval_seconds=context.observe_interval_seconds,
        observe_backoff_cap_seconds=context.observe_backoff_cap_seconds,
        max_run_seconds=context.max_run_seconds,
    )
    policy = resolve_core_policy(
        options,
        bounds,
        ResolvedRun(
            facts=context.facts,
            evaluation_plan=context.evaluation_plan,
            environment=context.environment,
            baseline_commit=commit,
            artifacts=context.artifacts,
        ),
    )
    return CorePlan(
        strategy=policy.strategy,
        reply_schemas=policy.reply_schemas,
        facts=policy.facts,
        limits=policy.limits,
        deadline_seconds=policy.deadline_at,
        prompt_variables={
            "facts": context.facts,
            "objective": policy.facts.objective,
            "agent_evaluation": True,
        },
        requirements=policy.requirements,
        agent_evaluation=agent_evaluation_policy(
            measurement_recipe(policy.strategy.config, policy.facts),
            accuracy=policy.strategy.config.accuracy_configured,
            benchmark=policy.strategy.config.benchmark_configured,
        ),
    )


def issue_queue_core_policy() -> CorePolicy:
    """The issue queue as a core run: its plan, operations and the roles that perform them."""
    return CorePolicy(
        plan=_core_plan,
        operations=tuple(
            CoreOperation(role, registration)
            for role, registration in operation_roles(OPERATIONS).items()
        ),
        retention_label=RETENTION_LABEL,
        prompt_templates=Path(prompts.__file__).parent,
        owned_operations=board_entries,
    )


def _project_max_rounds(options: BaseModel) -> int:
    """The operator's ``--max-rounds``, which ``run_started`` reports as ``max_rounds``."""
    return IssueQueueOptions.model_validate(options).max_rounds


def issue_queue_core_registration() -> OrchestrationRegistration:
    """The issue queue registered as a core run the built-in `plain` registration."""
    plugin = OrchestrationPlugin(
        id=_PLUGIN_ID,
        agents=CORE_ROLES,
        options=IssueQueueOptions,
        core=issue_queue_core_policy(),
    )
    return OrchestrationRegistration(
        plugin=plugin, projector=issue_queue_projector(), project_max_rounds=_project_max_rounds
    )
