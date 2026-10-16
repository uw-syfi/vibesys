"""The serial designer's roadmap, reached through the roadmap's owner.

The roadmap is kept by the tracker library behind its `Roadmap` interface; the flow asks its
owner through `ctx.owners` and never learns where the items are stored. A request whose effect
stays unknown is sent again up to `ROADMAP_TRIES` times: a read is a query and a put is
idempotent, so neither can apply twice. A roadmap that cannot be read or written is a typed
`RoadmapUnavailable` that ends the design with its reason, because planning without it would
reseed directions the run already parked or abandoned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api.search import (
    Again,
    Failed,
    Flow,
    GaveUp,
    OperationRequest,
    RetryPolicy,
    Tries,
    Value,
)
from vs_issue_tracker.api import (
    PutRoadmapItems,
    ReadRoadmap,
    RoadmapItem,
    RoadmapItemsPut,
    RoadmapSnapshot,
    roadmap_operation_registrations,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

ROADMAP = roadmap_operation_registrations()
"""The roadmap's operations, owned by the tracker library (read, put items)."""

ROADMAP_TRIES = 3
"""How many times a roadmap request whose effect stayed unknown is sent in all."""


class RoadmapUnavailable(Value):
    """The roadmap could not be read or written; ``reason`` says which and why."""

    reason: str


async def _perform(
    ctx: Flow, request: OperationRequest, doing: str
) -> BaseModel | RoadmapUnavailable:
    seconds = ctx.task.terms.operation_seconds

    async def attempt(_tries: Tries[str]) -> BaseModel | Again[str]:
        outcome = await ctx.owners.perform(request, within=seconds)
        return Again(reason=outcome.reason.value) if isinstance(outcome, Failed) else outcome

    match await ctx.retry(attempt, RetryPolicy(max_tries=ROADMAP_TRIES)):
        case GaveUp(reasons=reasons):
            detail = f"no answer after {len(reasons)} tries ({reasons[-1]})"
            return RoadmapUnavailable(reason=f"{doing}: {detail}")
        case outcome:
            return outcome


def _refused(outcome: RoadmapSnapshot | RoadmapItemsPut, doing: str) -> RoadmapUnavailable:
    detail = f"{outcome.status}: {outcome.detail}" if outcome.detail else outcome.status
    return RoadmapUnavailable(reason=f"{doing}: {detail}")


def _unexpected(outcome: BaseModel, doing: str) -> RoadmapUnavailable:
    return RoadmapUnavailable(reason=f"{doing}: unexpected outcome {type(outcome).__name__}")


async def read_roadmap(ctx: Flow) -> tuple[RoadmapItem, ...] | RoadmapUnavailable:
    """Every roadmap item, as it is now."""
    doing = "reading the roadmap"
    match await _perform(ctx, ReadRoadmap(), doing):
        case RoadmapUnavailable() as unavailable:
            return unavailable
        case RoadmapSnapshot(status="succeeded", items=items):
            return items
        case RoadmapSnapshot() as refused:
            return _refused(refused, doing)
        case other:
            return _unexpected(other, doing)


async def put_roadmap(ctx: Flow, items: tuple[RoadmapItem, ...]) -> RoadmapUnavailable | None:
    """Create or replace ``items`` on the roadmap, as one batch; nothing to do when empty."""
    if not items:
        return None
    doing = "writing the roadmap"
    match await _perform(ctx, PutRoadmapItems(items=items), doing):
        case RoadmapUnavailable() as unavailable:
            return unavailable
        case RoadmapItemsPut(status="succeeded"):
            return None
        case RoadmapItemsPut() as refused:
            return _refused(refused, doing)
        case other:
            return _unexpected(other, doing)
