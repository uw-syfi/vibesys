"""Validated authored-input contracts and bundle construction.

This is the product-owned interface for reading, validating, rendering, and
synthesizing VibeSys input bundles. Lower libraries own evaluator package and
execution mechanics.
"""

from vibesys.inputs._manifest import (
    MANIFEST_NAME,
    PROTOCOL_OUTPUT_FLAG,
    AgentInput,
    BenchmarkCommand,
    BenchmarkResult,
    BuildCommand,
    EnvironmentInput,
    EvaluatorInput,
    InputBundle,
    InputCommand,
    InputManifest,
    InputManifestParseError,
    ModalEnvironmentInput,
    ProfileGuidedInput,
    StageDeclaration,
    StageKind,
    WorkspaceInput,
    WorkspaceSource,
    benchmark_output_argument,
    load_input_manifest,
    load_project_task,
    load_task,
    render_input_manifest,
)
from vibesys.inputs._objective import with_operator_constraints
from vibesys.inputs._plan import (
    MAX_STEPS,
    DeviationPolicy,
    PlanStep,
    StepEvaluation,
    TaskPlan,
    outline_problems,
)
from vibesys.inputs._synthesis import (
    EVALUATOR_SRC_DIRNAME,
    InputSynthesisError,
    SynthesizedInputSpec,
    synthesize_input_bundle,
)
from vs_project.api import (
    OBJECTIVES_NAME,
    ObjectiveAxisInput,
    ObjectivesInput,
    ParetoInput,
    load_objectives,
)

__all__ = [
    "EVALUATOR_SRC_DIRNAME",
    "MANIFEST_NAME",
    "MAX_STEPS",
    "OBJECTIVES_NAME",
    "PROTOCOL_OUTPUT_FLAG",
    "AgentInput",
    "BenchmarkCommand",
    "BenchmarkResult",
    "BuildCommand",
    "DeviationPolicy",
    "EnvironmentInput",
    "EvaluatorInput",
    "InputBundle",
    "InputCommand",
    "InputManifest",
    "InputManifestParseError",
    "InputSynthesisError",
    "ModalEnvironmentInput",
    "ObjectiveAxisInput",
    "ObjectivesInput",
    "ParetoInput",
    "PlanStep",
    "ProfileGuidedInput",
    "StageDeclaration",
    "StageKind",
    "StepEvaluation",
    "SynthesizedInputSpec",
    "TaskPlan",
    "WorkspaceInput",
    "WorkspaceSource",
    "benchmark_output_argument",
    "load_input_manifest",
    "load_objectives",
    "load_project_task",
    "load_task",
    "outline_problems",
    "render_input_manifest",
    "synthesize_input_bundle",
    "with_operator_constraints",
]
