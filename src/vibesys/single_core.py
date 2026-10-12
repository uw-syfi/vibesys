"""Assemble the single core policy from a run's resolved resources.

The pure policy lives in `orchestration/single/core_policy`. This module does the effects it
cannot: store the evaluation recipe, read the evaluation identity, choose the workspace mode the
run environment supports, bind the strategy's operation roles to the runtime's production owners
and read a run's committed record.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.core_wiring import (
    ResolvedRun,
    agent_evaluation_policy,
    domain_prompt_sections,
    evaluation_inputs,
    operation_catalog,
)
from vibesys.orchestration.single.core_policy.api import (
    PolicyInputs,
    RunBounds,
    SingleCorePolicy,
    build_core_policy,
    project_strategy_state,
    prompts,
    run_prompt_variables,
)
from vibesys.orchestration.single.models import ProfileGuidedSingleOptions, SingleOptions
from vibesys.orchestration.single.strategy.api import (
    OPERATIONS,
    SingleStrategyState,
    measurement_recipe,
    single_operation_registry,
)
from vibesys.plugin_registration import OrchestrationRegistration, RuntimeRecordProjector
from vibesys.prompts import CORE_PROTOCOL_DIR
from vibesys.serial_search.api import PROMPTS_DIR as SERIAL_SEARCH_PROMPTS
from vibesys.single_roles import CORE_ROLES
from vs_runtime.api import CoreOperation, CorePlan, CorePolicy, OrchestrationPlugin
from vs_runtime.api.core import (
    OperationCatalog,
    OperationPorts,
    RuntimeRecord,
    operation_roles,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vs_runtime.api import CoreRunContext

type SingleRunOptions = SingleOptions | ProfileGuidedSingleOptions

# The label of the snapshot that retains a verified revision.
RETENTION_LABEL = "verified"
# The directories the designer's run-scoped turn may write.
ARTIFACT_DIRECTORIES = ("roadmap",)


def resolve_core_policy(
    options: SingleRunOptions, bounds: RunBounds, run: ResolvedRun, *, isolated_candidates: bool
) -> SingleCorePolicy:
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
            profiler_id=run.facts.profiler_id,
            accuracy_seconds=evaluation.accuracy_seconds,
            benchmark_seconds=evaluation.benchmark_seconds,
        ),
    )


def single_operation_catalog(ports: OperationPorts) -> OperationCatalog:
    """The operations the single strategy requires, each owned by the production owner of its role."""
    return operation_catalog(OPERATIONS, ports)


def single_projector(plugin_id: str) -> RuntimeRecordProjector[SingleStrategyState]:
    """Read a single run's strategy state from its committed runtime record."""
    return RuntimeRecordProjector(
        plugin_id=plugin_id,
        state_type=SingleStrategyState,
        record_type=RuntimeRecord[SingleStrategyState],
        operations=single_operation_registry(),
        project=lambda state, revision: project_strategy_state(state, experiment_revision=revision),
    )


def _plan_for(options_type: type[SingleRunOptions]) -> Callable[[CoreRunContext], CorePlan]:
    def plan(context: CoreRunContext) -> CorePlan:
        options = options_type.model_validate(context.options)
        commit = context.baseline.git_commit
        if commit is None:
            message = "the single core policy needs a Git baseline revision"
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
                    context.facts, modality=None, interface=config.interface
                ),
            ),
            requirements=policy.requirements,
            agent_evaluation=agent_evaluation_policy(
                measurement_recipe(config, policy.facts),
                accuracy=config.accuracy_configured,
                benchmark=config.benchmark_configured,
            ),
        )

    return plan


def single_core_policy(options_type: type[SingleRunOptions]) -> CorePolicy:
    """The single search as a core run: its plan, operations and the roles that perform them."""
    return CorePolicy(
        plan=_plan_for(options_type),
        operations=tuple(
            CoreOperation(role, registration)
            for role, registration in operation_roles(OPERATIONS).items()
        ),
        retention_label=RETENTION_LABEL,
        prompt_templates=Path(prompts.__file__).parent,
        prompt_fallback_roots=(SERIAL_SEARCH_PROMPTS, CORE_PROTOCOL_DIR),
        artifact_directories=ARTIFACT_DIRECTORIES,
    )


def _project_max_rounds(options: BaseModel) -> int:
    """The operator's ``--max-rounds``, which ``run_started`` reports as ``max_rounds``."""
    return config_free_max_rounds(options)


def config_free_max_rounds(options: BaseModel) -> int:
    """The round budget of either preset's validated options."""
    return SingleOptions.model_validate(options.model_dump()).max_rounds


def single_core_registration(
    plugin_id: str, options_type: type[SingleRunOptions]
) -> OrchestrationRegistration:
    """One single preset registered as a core run."""
    plugin = OrchestrationPlugin(
        id=plugin_id,
        agents=CORE_ROLES,
        options=options_type,
        core=single_core_policy(options_type),
        memory_paths=ARTIFACT_DIRECTORIES,
    )
    return OrchestrationRegistration(
        plugin=plugin,
        projector=single_projector(plugin_id),
        project_max_rounds=lambda options: (
            options_type.model_validate(options.model_dump()).max_rounds
        ),
    )


__all__ = ["asdict", "single_core_registration"]
