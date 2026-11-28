"""Trusted input generator and PyTorch oracle for ready whole-model cases."""

from __future__ import annotations

import importlib

import torch

from ..cases import Case
from .oracle import fp32_reference


Inputs = dict[str, torch.Tensor]
Outputs = dict[str, torch.Tensor]

TASK_MODULES = {
    "dense_step": "dense", "moe_step": "moe", "quant_step": "gemma",
    "spec_target_step": "eagle3", "gptoss_step": "gptoss",
    "hybrid_step": "hybrid", "vl_decode_step": "vision",
    "spec_full_iteration": "speculative",
    "distributed_step": "distributed",
    "deepseek_v32_step": "frontier", "glm52_step": "frontier",
    "kimi_k3_step": "kimi", "glm53_flash_step": "glm53", "kimi_k3_layer": "kimi_layer",
    "tts_frame_step": "csm", "vla_action_step": "pi05",
    "world_frame_step": "waypoint", "megamoe_layer": "megamoe",
    "dsv41_dspark_step": "dsv41",
}


def make_inputs(case: Case, seed: int, device: str = "cpu") -> Inputs:
    if not case.ready:
        raise NotImplementedError(f"{case.id}: {case.note}")
    if case.family not in TASK_MODULES:
        raise NotImplementedError(f"no input generator for {case.id}")
    module = importlib.import_module(f"{__package__}.{TASK_MODULES[case.family]}")
    return module.make_inputs(case, seed, device)


def _module(case: Case):
    if not case.ready:
        raise NotImplementedError(f"{case.id}: {case.note}")
    if case.family not in TASK_MODULES:
        raise NotImplementedError(f"no PyTorch oracle for {case.id}")
    return importlib.import_module(f"{__package__}.{TASK_MODULES[case.family]}")


def reference(case: Case, t: Inputs, **options) -> Outputs:
    return _module(case).reference(case, t, **options)


def oracle(case: Case, t: Inputs, **options) -> Outputs:
    """The reference's operator graph without BF16 activation rounding."""
    return fp32_reference(_module(case), case, t, **options)


def set_trial_scenario(case: Case, t: Inputs, trial: int) -> str:
    """Let a task adjust trial inputs (e.g. forced acceptance); returns the scenario name."""
    module = _module(case)
    if hasattr(module, "set_trial_scenario"):
        return module.set_trial_scenario(case, t, trial)
    return "seeded"


def regrade(case: Case, t: Inputs, expected: Outputs, exact: Outputs,
            actual) -> tuple[Outputs, Outputs]:
    """Let a task re-derive reference outputs that follow the candidate's own picks."""
    module = _module(case)
    if hasattr(module, "regrade"):
        return module.regrade(case, t, expected, exact, actual,
                              lambda c, v, **o: oracle(c, v, **o))
    return expected, exact
