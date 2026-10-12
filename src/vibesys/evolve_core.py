"""Assemble the evolve core policy from a run's resolved resources.

The pure policy lives in `orchestration/evolve/core_policy`. This module does the effects it
cannot: store the evaluation recipe, read the evaluation identity, choose the workspace mode the
run environment supports, bind the strategy's operation roles to the runtime's production owners
and read a run's committed record.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.core_wiring import (
    ResolvedRun,
    agent_evaluation_policy,
    domain_prompt_sections,
    evaluation_inputs,
    operation_catalog,
)
from vibesys.evolve_roles import CORE_ROLES
from vibesys.orchestration.evolve.core_policy.api import (
    EvolveCorePolicy,
    PolicyInputs,
    RunBounds,
    build_core_policy,
    project_strategy_state,
    prompts,
    run_prompt_variables,
)
from vibesys.orchestration.evolve.models import EvolveOptions
from vibesys.orchestration.evolve.strategy.api import (
    OPERATIONS,
    EvolveStrategyState,
    evolve_operation_registry,
    measurement_recipe,
)
from vibesys.plugin_registration import OrchestrationRegistration, RuntimeRecordProjector
from vibesys.prompts import CORE_PROTOCOL_DIR, PROMPTS_DIR
from vs_runtime.api import CoreOperation, CorePlan, CorePolicy, OrchestrationPlugin
from vs_runtime.api.core import (
    OperationCatalog,
    OperationPorts,
    RuntimeRecord,
    operation_roles,
)

if TYPE_CHECKING:
    from vs_runtime.api import CoreRunContext

# The label of the snapshot that retains a verified revision.
RETENTION_LABEL = "verified"


def resolve_core_policy(
    options: EvolveOptions, bounds: RunBounds, run: ResolvedRun, *, isolated_candidates: bool
) -> EvolveCorePolicy:
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
            isolated_candidates=isolated_candidates,
            domain_id=run.facts.domain_id,
            profiler_id=run.facts.profiler_id,
            profile_execution=run.facts.profile_execution.value,
            benchmark_command=run.facts.benchmark_command,
            reference_path=run.facts.reference_location,
            accuracy_seconds=evaluation.accuracy_seconds,
            benchmark_seconds=evaluation.benchmark_seconds,
        ),
    )


def evolve_operation_catalog(ports: OperationPorts) -> OperationCatalog:
    """The operations the evolve strategy requires, each owned by the production owner of its role."""
    return operation_catalog(OPERATIONS, ports)


def evolve_projector(plugin_id: str) -> RuntimeRecordProjector[EvolveStrategyState]:
    """Read an evolve run's strategy state from its committed runtime record."""
    return RuntimeRecordProjector(
        plugin_id=plugin_id,
        state_type=EvolveStrategyState,
        record_type=RuntimeRecord[EvolveStrategyState],
        operations=evolve_operation_registry(),
        project=lambda state, revision: project_strategy_state(state, experiment_revision=revision),
    )


def _plan(context: CoreRunContext) -> CorePlan:
    options = EvolveOptions.model_validate(context.options)
    commit = context.baseline.git_commit
    if commit is None:
        message = "the evolve core policy needs a Git baseline revision"
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
        isolated_candidates=context.environment.parallel_candidate_obstacle is None,
    )
    config = policy.strategy.config
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
            domain_sections=domain_prompt_sections(
                context.facts, modality=config.modality, interface=config.interface
            ),
        ),
        requirements=policy.requirements,
        agent_evaluation=agent_evaluation_policy(
            measurement_recipe(config, policy.facts),
            accuracy=config.accuracy_configured,
            benchmark=config.benchmark_configured,
        ),
    )


def evolve_core_policy() -> CorePolicy:
    """The evolutionary search as a core run: its plan, operations and the roles that perform them."""
    return CorePolicy(
        plan=_plan,
        operations=tuple(
            CoreOperation(role, registration)
            for role, registration in operation_roles(OPERATIONS).items()
        ),
        retention_label=RETENTION_LABEL,
        prompt_templates=Path(prompts.__file__).parent,
        prompt_fallback_roots=(CORE_PROTOCOL_DIR, PROMPTS_DIR / "shared"),
    )


def evolve_core_registration(plugin_id: str) -> OrchestrationRegistration:
    """The evolve search registered as a core run."""
    plugin = OrchestrationPlugin(
        id=plugin_id,
        agents=CORE_ROLES,
        options=EvolveOptions,
        core=evolve_core_policy(),
    )
    return OrchestrationRegistration(
        plugin=plugin,
        projector=evolve_projector(plugin_id),
        project_max_rounds=lambda options: (
            EvolveOptions.model_validate(options.model_dump()).max_generations
        ),
    )
