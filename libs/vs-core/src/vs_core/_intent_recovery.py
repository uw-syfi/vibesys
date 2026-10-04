"""Intent recovery lifecycle implementation owned by its wave-1 slice."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import Area, KernelNotImplementedError

if TYPE_CHECKING:
    from .types.intents import IntentsEvent, IntentsState
    from .types.kernel import AreaChange, IntentsContext


def advance(
    state: IntentsState, context: IntentsContext, event: IntentsEvent
) -> AreaChange[IntentsState]:
    """Consume only wrapper-routed events, preserving sibling-owned state fields.

    Behavior remains explicitly unavailable until this lifecycle slice moves.
    """
    del state, context
    raise KernelNotImplementedError(Area.INTENTS, event.kind, subarea="_intent_recovery")
