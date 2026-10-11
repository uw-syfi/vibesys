"""Assemble the dynamic core policy from a run's resolved resources.

The pure policy lives in `orchestration/dynamic/core_policy`. This module does the
effects it cannot: store the evaluation recipe, read the evaluation identity,
bind the strategy's operation roles to the runtime's production owners and read
a run's committed record.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.dynamic_roles import CORE_ROLES
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.orchestration.dynamic.core_policy.api import (
    PolicyInputs,
    RunBounds,
    build_core_policy,
    project_strategy_state,
    prompts,
    run_prompt_variables,
)
from vibesys.orchestration.dynamic.models import DynamicOptions
from vibesys.orchestration.dynamic.strategy.api import (
    OPERATIONS,
    STATE_SCHEMA,
    DynamicStrategyState,
    dynamic_operation_registry,
    measurement_recipe,
)
from vibesys.plugin_registration import OrchestrationRegistration, RuntimeRecordProjector
from vibesys.run.evaluation_backend import semantic_evaluation_identity
from vs_core.api import ArtifactId, ArtifactRef
from vs_runtime.api import (
    CoreOperation,
    CorePlan,
    CorePolicy,
    OrchestrationPlugin,
)
from vs_runtime.api.core import (
    AgentEvaluationPolicy,
    OperationCatalog,
    OperationPorts,
    RuntimeRecord,
    build_operation_catalog,
    operation_roles,
    production_owners,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.orchestration.dynamic.core_policy.api import DynamicCorePolicy
    from vs_runtime.api import ArtifactStore, CoreRunContext, RunFacts
    from vs_runtime.api.infrastructure import RunEnvironmentView, TrustedEvaluationPlan

_PLUGIN_ID = STATE_SCHEMA.name.removesuffix(".strategy-state")


@dataclass(frozen=True)
class ResolvedRun:
    """What the host resolved about a run before the core starts."""

    facts: RunFacts
    evaluation_plan: TrustedEvaluationPlan
    environment: RunEnvironmentView
    baseline_commit: str
    artifacts: ArtifactStore


def resolve_core_policy(
    options: DynamicOptions, bounds: RunBounds, run: ResolvedRun
) -> DynamicCorePolicy:
    """The core policy of one run: stores the evaluation recipe, then builds the pure policy.

    The recipe is the evaluation plan as a content-addressed artifact, and the
    three digests are the ones the evaluation backend computes for the same run.
    """
    plan = run.evaluation_plan
    content = plan.model_dump_json().encode()
    receipt = run.artifacts.write(content)
    identity = semantic_evaluation_identity(plan, run.facts, run.environment)
    return build_core_policy(
        options,
        bounds,
        PolicyInputs(
            objective=run.facts.objective,
            baseline_commit=run.baseline_commit,
            evaluator_digest=identity.evaluator.value,
            workload_digest=identity.workload.value,
            environment_digest=identity.environment.value,
            recipe=ArtifactRef(
                artifact_id=ArtifactId(root="recipe:evaluation-plan"), digest=receipt.sha256
            ),
            accuracy_configured=run.facts.accuracy_configured,
            benchmark_configured=run.facts.benchmark_configured,
            profiling=run.facts.profiler_id != "none",
            profile_measurement=run.facts.profiler_id != "none"
            and plan.profile_command is not None,
            accuracy_seconds=plan.accuracy_timeout_seconds,
            benchmark_seconds=plan.benchmark_timeout_seconds,
            profile_seconds=plan.profile_timeout_seconds,
        ),
    )


def dynamic_operation_catalog(ports: OperationPorts) -> OperationCatalog:
    """The operations the dynamic strategy requires, each owned by the production owner of its role."""
    registrations = operation_roles(OPERATIONS)
    return build_operation_catalog(registrations, production_owners(registrations, ports))


def dynamic_projector() -> RuntimeRecordProjector[DynamicStrategyState]:
    """Read a dynamic run's strategy state from its committed runtime record."""
    return RuntimeRecordProjector(
        plugin_id=_PLUGIN_ID,
        state_type=DynamicStrategyState,
        record_type=RuntimeRecord[DynamicStrategyState],
        operations=dynamic_operation_registry(),
        project=lambda state, revision: project_strategy_state(state, experiment_revision=revision),
    )


# The label of the snapshot that retains a verified revision.
RETENTION_LABEL = "verified"


def _require_candidate_sandboxes(environment: RunEnvironmentView) -> None:
    """Refuse a run environment that cannot open isolated candidate sandboxes.

    Every workstream runs in its own candidate sandbox, so without them the input
    measurement would fail and the planner would be paid for work that cannot start.
    """
    obstacle = environment.parallel_candidate_obstacle
    if obstacle is not None:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="dynamic_run_environment_unsupported",
                stage="run_environment_validation",
                message=(
                    f"the dynamic loop cannot run on the {environment.env_kind!r} run "
                    "environment: it cannot open isolated candidate sandboxes (parallel "
                    f"candidate evaluation) because {obstacle}; choose a run environment "
                    "that can, such as docker, slurm or modal"
                ),
            )
        )


def _core_plan(context: CoreRunContext) -> CorePlan:
    """The plan of one run: the policy resolved from what the host opened for it."""
    _require_candidate_sandboxes(context.environment)
    options = DynamicOptions.model_validate(context.options)
    commit = context.baseline.git_commit
    if commit is None:
        message = "the dynamic core policy needs a Git baseline revision"
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
        prompt_variables=run_prompt_variables(
            objective=policy.facts.objective,
            environment_notes=context.facts.environment_notes,
            agent_evaluation=True,
        ),
        requirements=policy.requirements,
        agent_evaluation=_agent_evaluation(policy),
    )


def _agent_evaluation(policy: DynamicCorePolicy) -> AgentEvaluationPolicy:
    """The plan an implementer or judge measures with: the run's configured gates, once each.

    It reads the same digests, recipe and stage budgets as the strategy's own trusted
    measurement plans, so an agent measurement and a trusted one share one identity.
    """
    config = policy.strategy.config
    configured = tuple(
        name
        for name, enabled in (
            ("accuracy", config.accuracy_configured),
            ("benchmark", config.benchmark_configured),
        )
        if enabled
    )
    return AgentEvaluationPolicy.from_recipe(
        measurement_recipe(config, policy.facts).only(configured)
    )


def dynamic_core_policy() -> CorePolicy:
    """The dynamic search as a core run: its plan, operations and the roles that perform them."""
    return CorePolicy(
        plan=_core_plan,
        operations=tuple(
            CoreOperation(role, registration)
            for role, registration in operation_roles(OPERATIONS).items()
        ),
        retention_label=RETENTION_LABEL,
        prompt_templates=Path(prompts.__file__).parent,
    )


def _project_max_rounds(options: BaseModel) -> int:
    """The operator's ``--max-rounds``, which ``run_started`` reports as ``max_rounds``.

    The search schedules ``max_rounds * max_in_flight`` workstreams; that product is the
    search's own limit (``hypothesis_config``), not the number the operator asked for.
    """
    return DynamicOptions.model_validate(options).max_rounds


def dynamic_core_registration() -> OrchestrationRegistration:
    """The dynamic search registered as a core run the built-in `DYNAMIC` registration."""
    plugin = OrchestrationPlugin(
        id=_PLUGIN_ID,
        agents=CORE_ROLES,
        options=DynamicOptions,
        core=dynamic_core_policy(),
    )
    return OrchestrationRegistration(
        plugin=plugin, projector=dynamic_projector(), project_max_rounds=_project_max_rounds
    )
