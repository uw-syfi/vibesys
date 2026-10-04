"""Wave 1 intents reducer. This file is owned by the intents lane."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import Area, KernelNotImplementedError

if TYPE_CHECKING:
    from .types.intents import IntentsEvent, IntentsState
    from .types.kernel import AreaChange, IntentsContext


def advance_intent(
    state: IntentsState, context: IntentsContext, event: IntentsEvent
) -> AreaChange[IntentsState]:
    """Consume a typed event; kernel-only release rejects unimplemented logic."""
    del state, context
    raise KernelNotImplementedError(Area.INTENTS, event.kind)


def recover(
    state: IntentsState, context: IntentsContext, event: IntentsEvent
) -> AreaChange[IntentsState]:
    """Recovery is class-driven and belongs to the intents lane."""
    return advance_intent(state, context, event)
