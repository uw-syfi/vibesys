"""Session inputs lifecycle implementation owned by its wave-1 slice."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import Area, KernelNotImplementedError

if TYPE_CHECKING:
    from .types.kernel import AreaChange, SessionsContext
    from .types.sessions import SessionsEvent, SessionsState


def advance(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Consume only wrapper-routed events, preserving sibling-owned state fields.

    Behavior remains explicitly unavailable until this lifecycle slice moves.
    """
    del state, context
    raise KernelNotImplementedError(Area.SESSIONS, event.kind, subarea="_session_inputs")


def finish_run(state: SessionsState, context: SessionsContext) -> AreaChange[SessionsState]:
    """Record terminal receipts for remaining inputs after all ownership drains.

    The context is terminal. Preserve input payloads, existing receipts and all
    sibling fields. Emit only new terminal input receipts, with no requests or
    signals. RunEnded is published after this pure finalization returns.
    """
    del state, context
    raise KernelNotImplementedError(Area.SESSIONS, "finish_run", subarea="_session_inputs")
