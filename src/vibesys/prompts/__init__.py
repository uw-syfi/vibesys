"""Prompt rendering API and package-owned prompt assets."""

from vibesys.prompts.core_protocol import (
    ComparisonRule,
    CoreProtocolTemplate,
    WaitUnrecordedPrompt,
    core_protocol_variables,
)
from vibesys.prompts.plan_correction import render_plan_correction
from vibesys.prompts.renderer import (
    CORE_PROTOCOL_DIR,
    PROMPTS_DIR,
    BackendPromptRenderer,
    ComputeBackendFragment,
    CpuComputeBackendFragment,
    CudaComputeBackendFragment,
    MetalComputeBackendFragment,
    RocmComputeBackendFragment,
    TrainiumComputeBackendFragment,
    get_backend_fragment,
    render_string,
    render_template,
)

__all__ = [
    "CORE_PROTOCOL_DIR",
    "PROMPTS_DIR",
    "BackendPromptRenderer",
    "ComparisonRule",
    "ComputeBackendFragment",
    "CoreProtocolTemplate",
    "CpuComputeBackendFragment",
    "CudaComputeBackendFragment",
    "MetalComputeBackendFragment",
    "RocmComputeBackendFragment",
    "TrainiumComputeBackendFragment",
    "WaitUnrecordedPrompt",
    "core_protocol_variables",
    "get_backend_fragment",
    "render_plan_correction",
    "render_string",
    "render_template",
]
