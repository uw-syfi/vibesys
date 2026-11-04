"""Assemble the dynamic search as a core run from a run's resolved resources.

The search itself is `orchestration/dynamic/search`: one flow, its roles and its pure helpers.
This module does the effects the search cannot: store the evaluation recipe, read the evaluation
identity, resolve the task's terms from the input bundle and the plan, bind the search's
operations to the runtime's production owners and read a run's committed record.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.candidates.api import Gates, Measuring, limits_for, run_deadline_at, terms_of
from vibesys.core_wiring import (
    EvaluationInputs,
    ResolvedRun,
    agent_evaluation_policy,
    domain_prompt_sections,
    evaluation_inputs,
    operation_catalog,
    require_candidate_sandboxes,
)
from vibesys.dynamic_handoff import handoff_entries
from vibesys.dynamic_roles import CORE_ROLES
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.orchestration.dynamic.core_policy.api import (
    project_run_snapshot,
    prompts,
    requirements_for,
    run_prompt_variables,
)
from vibesys.orchestration.dynamic.launch import DynamicLaunch
from vibesys.orchestration.dynamic.search.api import (
    OPERATIONS,
    RunSnapshot,
    budget,
    dynamic,
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
from vs_runtime.api import (
    CoreOperation,
    CorePlan,
    CorePolicy,
    OrchestrationPlugin,
)
from vs_runtime.api.core import (
    AgentEvaluationPolicy,
    OperationCatalog,
    OperationEntry,
    OperationPorts,
    operation_roles,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.run.contracts import PluginProjection
    from vs_core.api import TaskTerms
    from vs_runtime.api import CoreRunContext

_PLUGIN_ID = dynamic.name


def _registry() -> OperationRegistry:
    """The codec of every operation a run records: the kit's and the handoff write."""
    registry = dynamic.registry()
    if registry is None:
        message = "the dynamic search registers operations"
        raise ValueError(message)
    return registry


def dynamic_operation_catalog(
    ports: OperationPorts, handoff: tuple[OperationEntry, ...]
) -> OperationCatalog:
    """The operations the search requires: the kit's from their roles' owners, and the handoff's."""
    catalog = operation_catalog(OPERATIONS, ports)
    return OperationCatalog(_registry(), (*catalog.entries, *handoff))


def dynamic_projector() -> RuntimeRecordProjector[FlowState, RunSnapshot]:
    """Read a dynamic run's published record from its committed runtime record."""

    def published(state: FlowState) -> RunSnapshot | None:
        return FlowDriver.published(state, RunSnapshot)

    def project(record: RunSnapshot, revision: int) -> PluginProjection:
        return project_run_snapshot(record, experiment_revision=revision)

    return RuntimeRecordProjector(
        plugin_id=_PLUGIN_ID,
        state_type=FlowState,
        operations=_registry(),
        published=published,
        project=project,
    )


# The label of the snapshot that retains a verified revision.
RETENTION_LABEL = "verified"


def _task_terms(
    evaluation: EvaluationInputs, context: CoreRunContext, launch: DynamicLaunch
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


def _visible_comparisons(context: CoreRunContext) -> tuple[Mapping[str, object], ...]:
    """The comparison readings the host offers the planner: name and number, as data."""
    offered = context.prompt_variables.get("comparisons", ())
    if not isinstance(offered, (list, tuple)):
        return ()
    return tuple(row for row in offered if isinstance(row, Mapping))


def _core_plan(context: CoreRunContext) -> CorePlan:
    """The plan of one run: the search, its config and the task resolved from what the host opened."""
    require_candidate_sandboxes(context.environment, loop="dynamic")
    launch = DynamicLaunch.model_validate(context.options)
    commit = context.baseline.git_commit
    if commit is None:
        message = "the dynamic core policy needs a Git baseline revision"
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
    # The operator chooses how wide and how long to search; whether to profile follows the
    # evaluation plan (a profile command and a chosen profiler), which only the host knows.
    if evaluation.profile_capture_withheld:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="dynamic_profiling_unsupported",
                stage="run_environment_validation",
                message=(
                    "profiling is on (a profile command and a profiler are chosen) but the "
                    f"{context.environment.env_kind!r} run environment offers no profile capture; "
                    "choose a profiler of 'none' or a run environment that can capture a profile"
                ),
            )
        )
    config = launch.search_config(
        profiling=evaluation.profile_measurement, evaluation_capacity=context.evaluation_capacity
    )
    models = {dynamic.ids.role_id(role.name).root: role.name for role in dynamic.roles}
    chosen = {models[role]: model for role, model in context.role_models.items() if role in models}
    return CorePlan(
        search=dynamic.with_models(chosen),
        config=config,
        facts=facts,
        limits=limits_for(budget(config), context),
        deadline_seconds=run_deadline_at(context.max_run_seconds),
        prompt_variables=core_protocol_variables(policy_of(facts))
        | run_prompt_variables(
            objective=facts.objective,
            environment_notes=context.facts.environment_notes,
            agent_evaluation=True,
            agent_git_writable=context.facts.agent_git_writable,
            comparisons=_visible_comparisons(context),
            domain_sections=domain_prompt_sections(
                context.facts, modality=launch.modality, interface=launch.interface
            ),
        ),
        requirements=requirements_for(terms),
        agent_evaluation=_agent_evaluation(facts),
    )


def _agent_evaluation(facts: CoreRunFacts) -> AgentEvaluationPolicy:
    """The plan an implementer or judge measures with: the run's configured gates, once each."""
    terms = facts.task
    if terms is None:
        message = "the dynamic run's facts carry no task terms"
        raise ValueError(message)
    return agent_evaluation_policy(
        MeasurementRecipe.of(facts),
        accuracy="accuracy" in terms.measured,
        benchmark="benchmark" in terms.measured,
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
        prompt_fallback_roots=(CORE_PROTOCOL_DIR,),
        owned_operations=handoff_entries,
        span_kinds=dynamic.spans,
    )


def _project_max_rounds(options: BaseModel) -> int:
    """The operator's ``--max-rounds``, which ``run_started`` reports as ``max_rounds``.

    The search schedules ``max_rounds * max_in_flight`` workstreams; that product is the
    search's own limit (``start_budget``), not the number the operator asked for.
    """
    return DynamicLaunch.model_validate(options).max_rounds


def dynamic_core_registration() -> OrchestrationRegistration:
    """The dynamic search registered as a core run the built-in `DYNAMIC` registration."""
    plugin = OrchestrationPlugin(
        id=_PLUGIN_ID,
        agents=CORE_ROLES,
        options=DynamicLaunch,
        core=dynamic_core_policy(),
    )
    return OrchestrationRegistration(
        plugin=plugin,
        projector=dynamic_projector(),
        project_max_rounds=_project_max_rounds,
        iteration_support=IterationSupport.PROJECTED,
    )
