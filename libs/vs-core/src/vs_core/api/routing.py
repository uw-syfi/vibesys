"""Read-only dispatch topology, independent of lifecycle implementation status.

Each event has one declared dispatch target. Some targets intentionally compose
several lifecycle leaves; this table describes dispatch ownership, not their
internal transitions. Callable module and qualified names identify these targets.
"""

from collections.abc import Mapping
from types import FunctionType, MappingProxyType
from typing import TypeAliasType, cast, get_args

from vs_core import attempts, evaluation, intents, sessions, settlement
from vs_core.scheduling import schedule
from vs_core.types.common import Area
from vs_core.types.kernel import Signal
from vs_core.types.scheduling import SchedulingEvent

type EventRoutes = Mapping[type[Signal], FunctionType]


def _scheduling_routes(event_contract: TypeAliasType) -> EventRoutes:
    """Project the annotated event union onto its single scheduling leaf."""
    event_union = get_args(event_contract.__value__)[0]
    return cast("EventRoutes", MappingProxyType(dict.fromkeys(get_args(event_union), schedule)))


# Preserve the exact immutable tables consumed by the area reducers. Scheduling
# is itself a leaf, so every member of its published event union reaches schedule.
EVENT_ROUTES: Mapping[Area, EventRoutes] = cast(
    "Mapping[Area, EventRoutes]",
    MappingProxyType(
        {
            Area.ATTEMPTS: attempts.EVENT_TO_SUBAREA,
            Area.SESSIONS: sessions.EVENT_TO_SUBAREA,
            Area.EVALUATION: evaluation.EVENT_TO_SUBAREA,
            Area.SETTLEMENT: settlement.EVENT_TO_SUBAREA,
            Area.INTENTS: intents.EVENT_TO_SUBAREA,
            Area.SCHEDULING: _scheduling_routes(SchedulingEvent),
        }
    ),
)

__all__ = ["EVENT_ROUTES"]
