"""Assemble the gates-only input check as a core run."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

# Import the leaf directly: importing it through the parent would create a dependency on the
# orchestration package that already depends on this core registration.
import vibesys.orchestration.input_check.prompts as prompts  # noqa: PLR0402  # lint-waiver: LW-264201 [PLR0402]; importing through the parent would create a dependency cycle with this registration module.
from vibesys.candidates.api import (
    Gates,
    Measuring,
    RunBudget,
    limits_for,
    run_deadline_at,
    terms_of,
)
from vibesys.core_wiring import EvaluationInputs, ResolvedRun, evaluation_inputs, operation_catalog
from vibesys.orchestration.input_check.launch import InputCheckLaunch
from vibesys.orchestration.input_check.operations import OPERATIONS
from vibesys.orchestration.input_check.search import (
    InputCheckConfig,
    InputCheckSnapshot,
    check_input,
)
from vibesys.plugin_registration import (
    IterationSupport,
    OrchestrationRegistration,
    RuntimeRecordProjector,
)
from vibesys.run.contracts import IterationTree, PluginProjection
from vs_core.api import EvidenceRequirements, FlowDriver, FlowState, RevisionRef
from vs_core.api import RunFacts as CoreRunFacts
from vs_runtime.api import CoreOperation, CorePlan, CorePolicy, OrchestrationPlugin
from vs_runtime.api.core import OperationCatalog, OperationPorts, RuntimeRecord, operation_roles

if TYPE_CHECKING:
    from vs_core.api import TaskTerms
    from vs_runtime.api import CoreRunContext

_PLUGIN_ID = check_input.name
_RETENTION_LABEL = "verified"
_ADOPTION_ROUNDS = 3


def input_check_operation_catalog(ports: OperationPorts) -> OperationCatalog:
    """Bind the input check's evidence decoding to the runtime's production owner."""
    return operation_catalog(OPERATIONS, ports)


def _task_terms(
    evaluation: EvaluationInputs, context: CoreRunContext, launch: InputCheckLaunch
) -> TaskTerms:
    """Resolve the gates and comparison space from the run's trusted task inputs."""
    return terms_of(
        evaluation.recipe,
        Gates(accuracy=evaluation.accuracy_configured, benchmark=evaluation.benchmark_configured),
        launch.metric_space,
        Measuring(
            stage_seconds={
                "accuracy": evaluation.accuracy_seconds,
                "benchmark": evaluation.benchmark_seconds,
                "profile": evaluation.profile_seconds,
            },
            queue_allowance=float(context.queue_allowance_seconds),
            benchmark_needs_accuracy=evaluation.benchmark_needs_accuracy,
            profile_fields=(),
        ),
    )


def _core_plan(context: CoreRunContext) -> CorePlan:
    """Build the zero-agent plan over the run's trusted evaluation stack."""
    launch = InputCheckLaunch.model_validate(context.options)
    commit = context.baseline.git_commit
    if commit is None:
        message = "the input check needs a Git baseline revision"
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
        task=terms,
    )
    return CorePlan(
        search=check_input,
        config=InputCheckConfig(),
        facts=facts,
        limits=limits_for(
            RunBudget(
                attempts=0,
                turns=0,
                parallel=1,
                refunds=0,
                adoption_rounds=_ADOPTION_ROUNDS,
            ),
            context,
        ),
        deadline_seconds=run_deadline_at(context.max_run_seconds),
        requirements=EvidenceRequirements(allow_empty_queue_success=True),
    )


def _projector() -> RuntimeRecordProjector[FlowState]:
    """Project the checked baseline for live and historical readers."""

    def project(state: FlowState, revision: int) -> PluginProjection:
        snapshot = FlowDriver.published(state, InputCheckSnapshot)
        payload = {} if snapshot is None else snapshot.model_dump(mode="json")
        return PluginProjection(
            payload=payload,
            iterations=IterationTree.from_nodes(()),
            rounds=(),
            experiment_revision=revision,
        )

    return RuntimeRecordProjector(
        plugin_id=_PLUGIN_ID,
        state_type=FlowState,
        record_type=RuntimeRecord[FlowState],
        operations=OPERATIONS.registry(),
        project=project,
    )


def input_check_core_registration() -> OrchestrationRegistration:
    """Register the no-agent input check as a built-in core run."""
    plugin = OrchestrationPlugin(
        id=_PLUGIN_ID,
        agents=(),
        options=InputCheckLaunch,
        core=CorePolicy(
            plan=_core_plan,
            operations=tuple(
                CoreOperation(role, registration)
                for role, registration in operation_roles(OPERATIONS).items()
            ),
            retention_label=_RETENTION_LABEL,
            prompt_templates=Path(prompts.__file__).parent,
        ),
    )
    return OrchestrationRegistration(
        plugin=plugin,
        projector=_projector(),
        iteration_support=IterationSupport.PROJECTED,
    )


__all__ = ["input_check_core_registration", "input_check_operation_catalog"]
