"""Continuations lifecycle implementation owned by its wave-1 slice."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import Area, KernelNotImplementedError

if TYPE_CHECKING:
    from .types.evaluation import EvaluationEvent, EvaluationState
    from .types.kernel import AreaChange, EvaluationContext


def advance(
    state: EvaluationState, context: EvaluationContext, event: EvaluationEvent
) -> AreaChange[EvaluationState]:
    """Consume only wrapper-routed events, preserving sibling-owned state fields.

    Behavior remains explicitly unavailable until this lifecycle slice moves.
    """
    del state, context
    raise KernelNotImplementedError(Area.EVALUATION, event.kind, subarea="_continuations")
