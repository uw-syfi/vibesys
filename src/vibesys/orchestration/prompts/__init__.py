"""Prompt rendering API and package-owned prompt assets."""

from vibesys.orchestration.prompts.renderer import (
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
    "render_string",
    "render_template",
]
