"""Validated authored-input contracts and bundle construction.

This is the product-owned interface for reading, validating, rendering, and
synthesizing VibeSys input bundles. Evaluator execution remains owned by
``vibesys.evaluators``.
"""

from vibesys.inputs._manifest import (
    MANIFEST_NAME,
    PROTOCOL_OUTPUT_FLAG,
    AgentInput,
    BenchmarkCommand,
    BenchmarkResult,
    EnvironmentInput,
    EvaluatorInput,
    InputBundle,
    InputCommand,
    InputManifest,
    ModalEnvironmentInput,
    ProfileGuidedInput,
    WorkspaceInput,
    WorkspaceSource,
    benchmark_output_argument,
    load_input_bundle,
    load_project_task,
    render_input_manifest,
)
from vibesys.inputs._synthesis import (
    EVALUATOR_SRC_DIRNAME,
    InputSynthesisError,
    SynthesizedInputSpec,
    synthesize_input_bundle,
)

__all__ = [
    "EVALUATOR_SRC_DIRNAME",
    "MANIFEST_NAME",
    "PROTOCOL_OUTPUT_FLAG",
    "AgentInput",
    "BenchmarkCommand",
    "BenchmarkResult",
    "EnvironmentInput",
    "EvaluatorInput",
    "InputBundle",
    "InputCommand",
    "InputManifest",
    "InputSynthesisError",
    "ModalEnvironmentInput",
    "ProfileGuidedInput",
    "SynthesizedInputSpec",
    "WorkspaceInput",
    "WorkspaceSource",
    "benchmark_output_argument",
    "load_input_bundle",
    "load_project_task",
    "render_input_manifest",
    "synthesize_input_bundle",
]
