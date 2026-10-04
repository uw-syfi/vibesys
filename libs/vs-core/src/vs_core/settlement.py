"""Wave 1 settlement reducer. This file is owned by the settlement lane."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import Area, KernelNotImplementedError

if TYPE_CHECKING:
    from .types.kernel import AreaChange, SettlementContext
    from .types.settlement import SettlementEvent, SettlementState


def settle(
    state: SettlementState, context: SettlementContext, event: SettlementEvent
) -> AreaChange[SettlementState]:
    """Consume a typed event; kernel-only release rejects unimplemented logic."""
    del state, context
    raise KernelNotImplementedError(Area.SETTLEMENT, event.kind)


def advance_adoption(
    state: SettlementState, context: SettlementContext, event: SettlementEvent
) -> AreaChange[SettlementState]:
    """Adoption shares settlement authority, never performs I/O."""
    return settle(state, context, event)
