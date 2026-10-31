"""Assemble the evolve search as a core run from a run's resolved resources.

The search itself is `orchestration/evolve/search`: one flow, its roles and its pure helpers.
This module does the effects the search cannot: store the evaluation recipe, read the evaluation
identity, resolve the task's terms from the input bundle and the plan, bind the search's
operations to the runtime's production owners and read a run's committed record.
"""

from __future__ import annotations

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
    operation_catalog,
)
from vibesys.evolve_roles import CORE_ROLES
from vibesys.orchestration.evolve.core_policy.api import (
    project_snapshot,
    prompts,
    requirements_for,
    run_facts_variables,
    run_prompt_variables,
)
from vibesys.orchestration.evolve.launch import INTERFACE, EvolveLaunch
from vibesys.orchestration.evolve.search.api import OPERATIONS, EvolveSnapshot, budget, evolve
from vibesys.plugin_registration import (
    IterationSupport,
    OrchestrationRegistration,
    RuntimeRecordProjector,
)
from vibesys.prompts import CORE_PROTOCOL_DIR, PROMPTS_DIR, core_protocol_variables
from vs_core.api import FlowDriver, FlowState, MeasurementRecipe, RevisionRef, policy_of
from vs_core.api import RunFacts as CoreRunFacts
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
    operation_roles,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.run.contracts import PluginProjection
    from vs_core.api import TaskTerms
    from vs_runtime.api import CoreRunContext

_PLUGIN_ID = evolve.name

# The label of the snapshot that retains a verified revision.
RETENTION_LABEL = "verified"


def evolve_operation_catalog(ports: OperationPorts) -> OperationCatalog:
    """The operations the evolve search requires, each owned by the production owner of its role."""
    return operation_catalog(OPERATIONS, ports)


def evolve_projector() -> RuntimeRecordProjector[FlowState, EvolveSnapshot]:
    """Read an evolve run's published record from its committed runtime record."""

    def published(state: FlowState) -> EvolveSnapshot | None:
        return FlowDriver.published(state, EvolveSnapshot)

    def project(record: EvolveSnapshot, revision: int) -> PluginProjection:
        return project_snapshot(record, experiment_revision=revision)

    return RuntimeRecordProjector(
        plugin_id=_PLUGIN_ID,
        state_type=FlowState,
        operations=OPERATIONS.registry(),
        published=published,
        project=project,
    )


def _task_terms(
    evaluation: EvaluationInputs, context: CoreRunContext, launch: EvolveLaunch
) -> TaskTerms:
    """What the task fixes: resolved from the evaluation plan and the metric space."""
    return terms_of(
        evaluation.recipe,
        Gates(accuracy=evaluation.accuracy_configured, benchmark=evaluation.benchmark_configured),
        launch.metric_space,
        Measuring(
            stage_seconds={
                "build": evaluation.build_seconds,
                "accuracy": evaluation.accuracy_seconds,
                "benchmark": evaluation.benchmark_seconds,
            },
            queue_allowance=float(context.queue_allowance_seconds),
            benchmark_needs_accuracy=evaluation.benchmark_needs_accuracy,
            builds=evaluation.builds,
        ),
    )


def _core_plan(context: CoreRunContext) -> CorePlan:
    """The plan of one run: the search, its config and the task resolved from what the host opened."""
    launch = EvolveLaunch.model_validate(context.options)
    commit = context.baseline.git_commit
    if commit is None:
        message = "the evolve core policy needs a Git baseline revision"
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
    # The operator chooses how wide and how long to search; whether a passing child is profiled
    # follows the profiler the run chose, and where children work follows the run environment:
    # both are known only to the host.
    config = launch.search_config(
        profiling=context.facts.profiler_id != "none",
        workspace_mode=workspace_mode_for(
            isolated_candidates=context.environment.parallel_candidate_obstacle is None
        ),
    )
    models = {evolve.ids.role_id(role.name).root: role.name for role in evolve.roles}
    chosen = {models[role]: model for role, model in context.role_models.items() if role in models}
    return CorePlan(
        search=evolve.with_models(chosen),
        config=config,
        facts=facts,
        limits=limits_for(budget(config), context),
        deadline_seconds=run_deadline_at(context.max_run_seconds),
        prompt_variables=core_protocol_variables(policy_of(facts))
        | run_prompt_variables(
            objective=facts.objective,
            environment_notes=context.facts.environment_notes,
            agent_evaluation=True,
            domain_sections=domain_prompt_sections(
                context.facts, modality=launch.modality, interface=INTERFACE
            ),
            run_facts=run_facts_variables(
                interface=INTERFACE,
                reference_path=context.facts.reference_location,
                profiler_id=context.facts.profiler_id,
                profile_execution=context.facts.profile_execution.value,
                benchmark_command=context.facts.benchmark_command,
            ),
        ),
        requirements=requirements_for(terms),
        agent_evaluation=_agent_evaluation(facts),
    )


def _agent_evaluation(facts: CoreRunFacts) -> AgentEvaluationPolicy:
    """The plan a mutator or judge measures with: the run's configured gates, once each."""
    terms = facts.task
    if terms is None:
        message = "the evolve run's facts carry no task terms"
        raise ValueError(message)
    return agent_evaluation_policy(
        MeasurementRecipe.of(facts),
        accuracy="accuracy" in terms.measured,
        benchmark="benchmark" in terms.measured,
    )


def evolve_core_policy() -> CorePolicy:
    """The evolutionary search as a core run: its plan, operations and the roles that perform them."""
    return CorePolicy(
        plan=_core_plan,
        operations=tuple(
            CoreOperation(role, registration)
            for role, registration in operation_roles(OPERATIONS).items()
        ),
        retention_label=RETENTION_LABEL,
        prompt_templates=Path(prompts.__file__).parent,
        prompt_fallback_roots=(CORE_PROTOCOL_DIR, PROMPTS_DIR / "shared"),
        span_kinds=evolve.spans,
    )


def _project_max_rounds(options: BaseModel) -> int:
    """The operator's ``--max-generations``, which ``run_started`` reports as ``max_rounds``."""
    return EvolveLaunch.model_validate(options).max_generations


def evolve_core_registration() -> OrchestrationRegistration:
    """The evolve search registered as a core run."""
    plugin = OrchestrationPlugin(
        id=_PLUGIN_ID,
        agents=CORE_ROLES,
        options=EvolveLaunch,
        core=evolve_core_policy(),
    )
    return OrchestrationRegistration(
        plugin=plugin,
        projector=evolve_projector(),
        iteration_support=IterationSupport.PROJECTED,
        project_max_rounds=_project_max_rounds,
    )
