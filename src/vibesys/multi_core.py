"""Assemble the multi search as a core run from a run's resolved resources.

The search itself is `orchestration/multi/search`: one flow, its roles and its pure helpers.
This module does the effects the search cannot: store the evaluation recipe, read the evaluation
identity, resolve the task's terms from the input bundle and the plan, bind the search's
operations to the runtime's production owners and read a run's committed record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.candidates.api import (
    Gates,
    Measuring,
    limits_for,
    run_deadline_at,
    terms_of,
    workspace_mode_for,
)
from vibesys.core_wiring import (
    EvaluationInputs,
    ResolvedRun,
    agent_evaluation_policy,
    domain_prompt_sections,
    evaluation_inputs,
)
from vibesys.hypothesis import HypothesisState
from vibesys.multi_roles import CORE_ROLES
from vibesys.orchestration.multi.core_policy.api import (
    project_run_snapshot,
    prompts,
    requirements_for,
    run_prompt_variables,
)
from vibesys.orchestration.multi.launch import MultiLaunch, ProfileGuidedMultiLaunch
from vibesys.orchestration.multi.search.api import OPERATIONS, budget, multi
from vibesys.orchestration.profilers import ProfilerKind, profiler_definition
from vibesys.plugin_registration import (
    IterationSupport,
    OrchestrationRegistration,
    RuntimeRecordProjector,
)
from vibesys.prompts import CORE_PROTOCOL_DIR, PROMPTS_DIR, core_protocol_variables
from vibesys.serial_roadmap import roadmap_entries
from vibesys.serial_search.api import PROMPTS_DIR as SERIAL_SEARCH_PROMPTS
from vs_core.api import (
    FlowDriver,
    FlowState,
    MeasurementRecipe,
    OperationRegistry,
    RevisionRef,
    policy_of,
)
from vs_core.api import RunFacts as CoreRunFacts
from vs_runtime.api import (
    CoreOperation,
    CorePlan,
    CorePolicy,
    OrchestrationPlugin,
    ValidationRecipeArtifact,
)
from vs_runtime.api.core import (
    operation_roles,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.run.contracts import PluginProjection
    from vs_core.api import TaskTerms
    from vs_runtime.api import CoreRunContext

type MultiRunLaunch = MultiLaunch | ProfileGuidedMultiLaunch

# The label of the snapshot that retains a verified revision.
RETENTION_LABEL = "verified"


def _registry() -> OperationRegistry:
    """The codec of every operation a run records: the kit's and the roadmap's."""
    registry = multi.registry()
    if registry is None:
        message = "the multi search registers operations"
        raise ValueError(message)
    return registry


def multi_projector(plugin_id: str) -> RuntimeRecordProjector[FlowState, HypothesisState]:
    """Read a multi run's published record from its committed runtime record."""

    def published(state: FlowState) -> HypothesisState | None:
        return FlowDriver.published(state, HypothesisState)

    def project(record: HypothesisState, revision: int) -> PluginProjection:
        return project_run_snapshot(record, experiment_revision=revision)

    return RuntimeRecordProjector(
        plugin_id=plugin_id,
        state_type=FlowState,
        operations=_registry(),
        published=published,
        project=project,
    )


def _task_terms(
    evaluation: EvaluationInputs, context: CoreRunContext, launch: MultiRunLaunch
) -> TaskTerms:
    """What the task fixes: resolved from the evaluation plan, the metric space and the domain."""
    return terms_of(
        evaluation.recipe,
        Gates(accuracy=evaluation.accuracy_configured, benchmark=evaluation.benchmark_configured),
        launch.metric_space,
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
            profile_fields=(
                tuple(item.value for item in evaluation.profile_fields)
                if evaluation.profile_measurement
                else ()
            ),
        ),
    )


def _plan_for(launch_type: type[MultiRunLaunch]) -> Callable[[CoreRunContext], CorePlan]:
    def plan(context: CoreRunContext) -> CorePlan:
        launch = launch_type.model_validate(context.options)
        commit = context.baseline.git_commit
        if commit is None:
            message = "the multi core policy needs a Git baseline revision"
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
        terms = _task_terms(evaluation, context, launch)
        facts = CoreRunFacts(
            objective=context.facts.objective,
            baseline=RevisionRef.of_git_commit(commit),
            evaluator_digest=evaluation.evaluator_digest,
            workload_digest=evaluation.workload_digest,
            environment_digest=evaluation.environment_digest,
            dedicated_measurement_resources=evaluation.dedicated_measurement_resources,
            task=terms,
        )
        profiler_id = context.facts.profiler_id
        profiler = None if profiler_id == "none" else profiler_definition(ProfilerKind(profiler_id))
        config = launch.search_config(
            profiling=profiler_id != "none",
            workspace_mode=workspace_mode_for(
                isolated_candidates=context.environment.parallel_candidate_obstacle is None
            ),
        )
        models = {multi.ids.role_id(role.identity).root: role.identity for role in multi.roles}
        chosen = {
            models[role]: model for role, model in context.role_models.items() if role in models
        }
        return CorePlan(
            search=multi.with_models(chosen),
            config=config,
            facts=facts,
            limits=limits_for(budget(config), context),
            deadline_seconds=run_deadline_at(context.max_run_seconds),
            prompt_variables=core_protocol_variables(policy_of(facts))
            | run_prompt_variables(
                objective=facts.objective,
                environment_notes=context.facts.environment_notes,
                agent_evaluation=True,
                validation_recipe_schema=json.dumps(
                    ValidationRecipeArtifact.model_json_schema(), indent=2
                ),
                domain_sections=domain_prompt_sections(
                    context.facts, modality=launch.modality, interface=launch.interface
                ),
                profiler_id=profiler_id,
                profile_execution=context.facts.profile_execution.value,
                profiler_support_name=profiler.support_name if profiler else "",
                profiler_mcp_name=profiler.mcp_name if profiler else "",
                benchmark_command=context.facts.benchmark_command,
                reference_path=context.facts.reference_location,
                interface=launch.interface,
            ),
            requirements=requirements_for(terms),
            agent_evaluation=agent_evaluation_policy(
                MeasurementRecipe.of(facts),
                accuracy="accuracy" in terms.measured,
                benchmark="benchmark" in terms.measured,
            ),
        )

    return plan


def multi_core_policy(launch_type: type[MultiRunLaunch]) -> CorePolicy:
    """The multi search as a core run: its plan, operations and the roles that perform them."""
    return CorePolicy(
        plan=_plan_for(launch_type),
        operations=tuple(
            CoreOperation(role, registration)
            for role, registration in operation_roles(OPERATIONS).items()
        ),
        retention_label=RETENTION_LABEL,
        prompt_templates=Path(prompts.__file__).parent,
        prompt_fallback_roots=(SERIAL_SEARCH_PROMPTS, CORE_PROTOCOL_DIR, PROMPTS_DIR / "shared"),
        owned_operations=roadmap_entries,
        span_kinds=multi.spans,
    )


def multi_core_registration(
    plugin_id: str, launch_type: type[MultiRunLaunch]
) -> OrchestrationRegistration:
    """One multi preset registered as a core run."""
    plugin = OrchestrationPlugin(
        id=plugin_id,
        agents=CORE_ROLES,
        options=launch_type,
        core=multi_core_policy(launch_type),
    )
    return OrchestrationRegistration(
        plugin=plugin,
        projector=multi_projector(plugin_id),
        iteration_support=IterationSupport.PROJECTED,
        project_max_rounds=lambda options: launch_type.model_validate(options).max_rounds,
    )


__all__ = ["multi_core_registration"]
