"""Wave 1 sessions reducer. This file is owned by the sessions lane."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import Area, KernelNotImplementedError

if TYPE_CHECKING:
    from .types.kernel import AreaChange, SessionsContext
    from .types.sessions import SessionsEvent, SessionsState


def advance_session(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Consume a typed event; kernel-only release rejects unimplemented logic."""
    del state, context
    raise KernelNotImplementedError(Area.SESSIONS, event.kind)
