"""Attempt retirement lifecycle implementation owned by its wave-1 slice."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import Area, KernelNotImplementedError

if TYPE_CHECKING:
    from .types.attempts import AttemptsEvent, AttemptsState
    from .types.kernel import AreaChange, AttemptsContext


def advance(
    state: AttemptsState, context: AttemptsContext, event: AttemptsEvent
) -> AreaChange[AttemptsState]:
    """Consume only wrapper-routed events, preserving sibling-owned state fields.

    Behavior remains explicitly unavailable until this lifecycle slice moves.
    """
    del state, context
    raise KernelNotImplementedError(Area.ATTEMPTS, event.kind, subarea="_attempt_retirement")
