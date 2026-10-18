"""Assemble the issue-queue search as a core run from a run's resolved resources.

The search itself is `orchestration/issue_queue/search`: one flow, its roles and its pure helpers.
This module does the effects the search cannot: store the evaluation recipe, read the evaluation
identity, resolve the task's terms from the evaluation plan, open the issue tracker the run
selected, bind the search's operations to the runtime's production owners, and declare the agent
roles a core run needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.candidates.api import Gates, Measuring, limits_for, run_deadline_at, terms_of
from vibesys.core_roles import (
    CONTINUATION,
    MEASURING_READER,
    MEASURING_WRITER,
    RoleKind,
    core_role,
)
from vibesys.core_wiring import (
    EvaluationInputs,
    ResolvedRun,
    agent_evaluation_policy,
    domain_prompt_sections,
    evaluation_inputs,
    operation_catalog,
    require_candidate_sandboxes,
)
from vibesys.issue_queue_board import board_entries
from vibesys.metrics import MetricSpace
from vibesys.orchestration.issue_queue.core_policy.api import (
    project_run_snapshot,
    prompts,
    requirements_for,
)
from vibesys.orchestration.issue_queue.launch import IssueQueueLaunch
from vibesys.orchestration.issue_queue.search.api import (
    IMPLEMENTER as IMPLEMENTER_ROLE,
)
from vibesys.orchestration.issue_queue.search.api import (
    JUDGE as JUDGE_ROLE,
)
from vibesys.orchestration.issue_queue.search.api import (
    OPERATIONS,
    QueueSnapshot,
    budget,
    issue_queue,
)
from vibesys.orchestration.issue_queue.search.api import (
    PERF_EVALUATOR as PERF_ROLE,
)
from vibesys.plugin_registration import (
    IterationSupport,
    OrchestrationRegistration,
    RuntimeRecordProjector,
)
from vibesys.prompts import CORE_PROTOCOL_DIR, core_protocol_variables
from vs_core.api import (
    FlowDriver,
    FlowState,
    MeasurementRecipe,
    OperationRegistry,
    RevisionRef,
    policy_of,
)
from vs_core.api import RunFacts as CoreRunFacts
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
    AgentEvaluationPolicy,
    OperationCatalog,
    OperationEntry,
    OperationPorts,
    RuntimeRecord,
    operation_roles,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.run.contracts import PluginProjection
    from vs_core.api import TaskTerms
    from vs_core.api.search import Role
    from vs_runtime.api import CoreRunContext

_PLUGIN_ID = issue_queue.name
# The label of the snapshot that retains a candidate.
RETENTION_LABEL = "verified"
_RENDERER = TemplateRenderer(Path(prompts.__file__).parent)


def _role(role: Role, template: str, kind: RoleKind) -> AgentRole:
    return core_role(
        _RENDERER,
        role_id=issue_queue.ids.role_id(role.name).root,
        system_template=template,
        kind=kind,
    )


IMPLEMENTER = _role(IMPLEMENTER_ROLE, "implementer_system_prompt.j2", MEASURING_WRITER)
JUDGE = _role(JUDGE_ROLE, "judge_system_prompt.j2", MEASURING_READER)
# The evaluator starts servers and writes its measurements in its own checkout, which is discarded
# when its attempt settles; it needs no evaluation tool. It reads captures through the stateless
# profiler tool, which holds no run state (so it is no second writer beside core) and which the
# host leaves out of the role in a run whose selected profiler is `none`.
PERF_EVALUATOR = _role(
    PERF_ROLE,
    "performance_system_prompt.j2",
    RoleKind(
        WorkspaceAccess.READ_WRITE,
        CONTINUATION | {AgentCapability.MCP_SERVERS},
        (AgentTool(id="profiler", stateless=True),),
    ),
)
CORE_ROLES = (IMPLEMENTER, JUDGE, PERF_EVALUATOR)


def _registry() -> OperationRegistry:
    """The codec of every operation a run records: the kit's and the board's."""
    registry = issue_queue.registry()
    if registry is None:
        message = "the issue-queue search registers operations"
        raise ValueError(message)
    return registry


def issue_queue_operation_catalog(
    ports: OperationPorts, board: tuple[OperationEntry, ...]
) -> OperationCatalog:
    """The operations the search requires: the kit's roles from their owners, and the board's."""
    catalog = operation_catalog(OPERATIONS, ports)
    return OperationCatalog(_registry(), (*catalog.entries, *board))


def issue_queue_projector() -> RuntimeRecordProjector[FlowState, QueueSnapshot]:
    """Read an issue-queue run's published record from its committed runtime record."""

    def published(state: FlowState) -> QueueSnapshot | None:
        return FlowDriver.published(state, QueueSnapshot)

    def project(record: QueueSnapshot, revision: int) -> PluginProjection:
        return project_run_snapshot(record, experiment_revision=revision)

    return RuntimeRecordProjector(
        plugin_id=_PLUGIN_ID,
        state_type=FlowState,
        record_type=RuntimeRecord[FlowState],
        operations=_registry(),
        published=published,
        project=project,
    )


def _task_terms(evaluation: EvaluationInputs, context: CoreRunContext) -> TaskTerms:
    """What the task fixes: resolved from the evaluation plan. The queue ranks nothing itself."""
    return terms_of(
        evaluation.recipe,
        Gates(accuracy=evaluation.accuracy_configured, benchmark=evaluation.benchmark_configured),
        MetricSpace(),
        Measuring(
            stage_seconds={
                "build": evaluation.build_seconds,
                "accuracy": evaluation.accuracy_seconds,
                "benchmark": evaluation.benchmark_seconds,
                "profile": evaluation.profile_seconds,
            },
            queue_allowance=float(context.queue_allowance_seconds),
            benchmark_needs_accuracy=evaluation.benchmark_needs_accuracy,
            builds=evaluation.builds,
        ),
    )


def _core_plan(context: CoreRunContext) -> CorePlan:
    """The plan of one run: the search, its config and the task resolved from what the host opened."""
    require_candidate_sandboxes(context.environment, loop="plain")
    launch = IssueQueueLaunch.model_validate(context.options)
    commit = context.baseline.git_commit
    if commit is None:
        message = "the issue queue core policy needs a Git baseline revision"
        raise ValueError(message)
    evaluation = evaluation_inputs(
        ResolvedRun(
            facts=context.facts,
            evaluation_plan=context.evaluation_plan,
            environment=context.environment,
            baseline_commit=commit,
            artifacts=context.artifacts,
        )
    )
    terms = _task_terms(evaluation, context)
    facts = CoreRunFacts(
        objective=context.facts.objective,
        baseline=RevisionRef.of_git_commit(commit),
        evaluator_digest=evaluation.evaluator_digest,
        workload_digest=evaluation.workload_digest,
        environment_digest=evaluation.environment_digest,
        dedicated_measurement_resources=evaluation.dedicated_measurement_resources,
        task=terms,
    )
    config = launch.search_config()
    models = {issue_queue.ids.role_id(role.name).root: role.name for role in issue_queue.roles}
    chosen = {models[role]: model for role, model in context.role_models.items() if role in models}
    return CorePlan(
        search=issue_queue.with_models(chosen),
        config=config,
        facts=facts,
        limits=limits_for(budget(config), context),
        deadline_seconds=run_deadline_at(context.max_run_seconds),
        prompt_variables=core_protocol_variables(policy_of(facts))
        | {
            "facts": context.facts,
            "objective": facts.objective,
            "agent_evaluation": True,
            "domain_sections": domain_prompt_sections(context.facts, modality=launch.modality),
        },
        requirements=requirements_for(),
        agent_evaluation=_agent_evaluation(facts),
    )


def _agent_evaluation(facts: CoreRunFacts) -> AgentEvaluationPolicy:
    """The plan an implementer or judge measures with: the run's configured gates, once each."""
    terms = facts.task
    if terms is None:
        message = "the issue-queue run's facts carry no task terms"
        raise ValueError(message)
    return agent_evaluation_policy(
        MeasurementRecipe.of(facts),
        accuracy="accuracy" in terms.measured,
        benchmark="benchmark" in terms.measured,
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
        prompt_fallback_roots=(CORE_PROTOCOL_DIR,),
        owned_operations=board_entries,
        span_kinds=issue_queue.spans,
    )


def _project_max_rounds(options: BaseModel) -> int:
    """The operator's ``--max-rounds``, which ``run_started`` reports as ``max_rounds``."""
    return IssueQueueLaunch.model_validate(options).max_rounds


def issue_queue_core_registration() -> OrchestrationRegistration:
    """The issue queue registered as a core run the built-in `plain` registration."""
    plugin = OrchestrationPlugin(
        id=_PLUGIN_ID,
        agents=CORE_ROLES,
        options=IssueQueueLaunch,
        core=issue_queue_core_policy(),
    )
    return OrchestrationRegistration(
        plugin=plugin,
        projector=issue_queue_projector(),
        project_max_rounds=_project_max_rounds,
        iteration_support=IterationSupport.PROJECTED,
    )
