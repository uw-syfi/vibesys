"""Which steps an environment refuses to run because it cannot clear them."""

from vs_runtime._step_clear import AGENT_STEP_KINDS, StepClearUnsupportedError

__all__ = ["AGENT_STEP_KINDS", "StepClearUnsupportedError"]
