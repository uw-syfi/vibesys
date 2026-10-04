"""Wave 1 attempts reducer. This file is owned by the attempts lane."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import Area, KernelNotImplementedError

if TYPE_CHECKING:
    from .types.attempts import AttemptsEvent, AttemptsState
    from .types.kernel import AreaChange, AttemptsContext


def advance_attempt(
    state: AttemptsState, context: AttemptsContext, event: AttemptsEvent
) -> AreaChange[AttemptsState]:
    """Consume a typed event; kernel-only release rejects unimplemented logic."""
    del state, context
    raise KernelNotImplementedError(Area.ATTEMPTS, event.kind)
