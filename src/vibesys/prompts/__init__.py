"""Prompt rendering API and package-owned prompt assets."""

from vibesys.prompts.plan_correction import render_plan_correction
from vibesys.prompts.renderer import (
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
    "PROMPTS_DIR",
    "BackendPromptRenderer",
    "ComputeBackendFragment",
    "CpuComputeBackendFragment",
    "CudaComputeBackendFragment",
    "MetalComputeBackendFragment",
    "RocmComputeBackendFragment",
    "TrainiumComputeBackendFragment",
    "get_backend_fragment",
    "render_plan_correction",
    "render_string",
    "render_template",
]
