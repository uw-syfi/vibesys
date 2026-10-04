"""Wave 1 scheduling reducer. This file is owned by the scheduling lane."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import Area, KernelNotImplementedError

if TYPE_CHECKING:
    from .types.kernel import AreaChange, SchedulingContext
    from .types.scheduling import SchedulingEvent, SchedulingState


def schedule(
    state: SchedulingState, context: SchedulingContext, event: SchedulingEvent
) -> AreaChange[SchedulingState]:
    """Consume a typed event; kernel-only release rejects unimplemented logic."""
    del state, context
    raise KernelNotImplementedError(Area.SCHEDULING, event.kind)
