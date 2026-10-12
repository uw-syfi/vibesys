"""The observation core gives a turn it ends because nothing could carry its acquisition on.

Such a turn never reached an executor: no request carried it, so no executor holds anything for
it. Core ends it with an observation of its own (cancelled, released, with no children) that
names no request. Settlement of the attempt that owned the turn recognizes this observation as
proof that the turn is drained, since there is no dispatch to prove it with.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import EventId, Observation, ObservationStatus, RequestId

if TYPE_CHECKING:
    from .types.common import Seconds
    from .types.sessions import Invocation

STALL_DIAGNOSTIC = (
    "acquisition stalled: the turn was never charged or never reached its session, "
    "and no request was left to carry it"
)
_PREFIX = "acquisition-stalled:"


def stalled_acquisition(invocation: Invocation, observed_at: Seconds) -> Observation:
    """The cancelled, released observation that ends *invocation* as never started."""
    identity = invocation.invocation.invocation_id.root
    return Observation(
        event_id=EventId(root=f"{_PREFIX}{identity}"),
        request_id=RequestId(root=f"{_PREFIX}{identity}"),
        scope=invocation.scope,
        sequence=0,
        observed_at=observed_at,
        status=ObservationStatus.CANCELLED,
        terminal=True,
        released=True,
        children_complete=True,
        diagnostic=STALL_DIAGNOSTIC,
    )


def is_stalled_acquisition(invocation: Invocation) -> bool:
    """Whether *invocation* ended as a turn that never started (see ``stalled_acquisition``)."""
    observation = invocation.observation
    return (
        observation is not None
        and observation.request_id.root == f"{_PREFIX}{invocation.invocation.invocation_id.root}"
        and observation.status == ObservationStatus.CANCELLED
        and observation.scope == invocation.scope
    )
