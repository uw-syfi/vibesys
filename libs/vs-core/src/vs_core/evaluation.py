"""Wave 1 evaluation reducer. This file is owned by the evaluation lane."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import Area, KernelNotImplementedError

if TYPE_CHECKING:
    from .types.evaluation import EvaluationEvent, EvaluationState
    from .types.kernel import AreaChange, EvaluationContext


def advance_evaluation(
    state: EvaluationState, context: EvaluationContext, event: EvaluationEvent
) -> AreaChange[EvaluationState]:
    """Consume a typed event; kernel-only release rejects unimplemented logic."""
    del state, context
    raise KernelNotImplementedError(Area.EVALUATION, event.kind)
