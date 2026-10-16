"""The owner of a serial run's roadmap: the designer's planning items, kept in run state.

The single and multi searches ask to read the roadmap and to put the items a designer's plan
changed (`ReadRoadmap`, `PutRoadmapItems`). This owner performs both over a `Roadmap`. The
production roadmap is a `DocumentRoadmap` in the run's portable state, opened through
`Project`: it is kept with the run, survives a restart or a resume, and is VibeSys-owned
untracked state, so it never makes the project a dirty baseline for the next run. Another
`Roadmap` (a tracker's issues) would plug in here without the searches or the prompts
changing, since they see only typed items.

The designer never writes the roadmap itself: its role reads only, and its reply is the one
source of what changes. A put is idempotent, so a replay is safe, and `inspect` says a put
landed when the roadmap holds every item of the batch exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vs_issue_tracker.api import (
    DocumentRoadmap,
    PutRoadmapItems,
    ReadRoadmap,
    RoadmapItemsPut,
    RoadmapLoadError,
    RoadmapRejectedError,
    RoadmapSnapshot,
    roadmap_operation_registrations,
)
from vs_runtime.api.core import Applied, Inspection, NotApplied, OperationEntry

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_core.api import OperationRequest
    from vs_issue_tracker.api import Roadmap
    from vs_runtime.api import OwnedOperationResources
    from vs_runtime.api.core import ExecutionContext

ROADMAP_NAMESPACE = "roadmap"
"""The run-state namespace that holds the roadmap."""


class RoadmapOwner:
    """Perform and inspect the roadmap operations over one roadmap."""

    def __init__(self, roadmap: Roadmap) -> None:
        """Bind the roadmap the run opened."""
        self._roadmap = roadmap

    async def execute(self, request: OperationRequest, context: ExecutionContext) -> BaseModel:
        """Perform one operation; a storage failure propagates as an unknown effect."""
        del context
        return self.perform(request)

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        """Say from the roadmap whether a put landed; a read answers by running."""
        del context
        return self.observe(request)

    def perform(self, request: OperationRequest) -> RoadmapSnapshot | RoadmapItemsPut:
        """Perform one operation now: the body of `execute`."""
        if isinstance(request, ReadRoadmap):
            return self._read()
        put = cast("PutRoadmapItems", request)
        try:
            self._roadmap.put(put.items)
        except RoadmapRejectedError as refused:
            return RoadmapItemsPut(status="rejected", detail=refused.problem)
        except RoadmapLoadError as unreadable:
            return RoadmapItemsPut(status="failed", detail=str(unreadable))
        return RoadmapItemsPut(status="succeeded")

    def observe(self, request: OperationRequest) -> Inspection:
        """Whether the operation's effect is recorded now: the body of `inspect`."""
        if isinstance(request, ReadRoadmap):
            return Applied(self._read())
        put = cast("PutRoadmapItems", request)
        try:
            held = set(self._roadmap.items())
        except RoadmapLoadError:
            return NotApplied()
        if all(item in held for item in put.items):
            return Applied(RoadmapItemsPut(status="succeeded"))
        return NotApplied()

    def _read(self) -> RoadmapSnapshot:
        try:
            return RoadmapSnapshot(status="succeeded", items=self._roadmap.items())
        except RoadmapLoadError as unreadable:
            return RoadmapSnapshot(status="failed", detail=str(unreadable))


def roadmap_entries(resources: OwnedOperationResources) -> tuple[OperationEntry, ...]:
    """The roadmap operations, owned by an owner over the run's roadmap in its portable state."""
    namespace = resources.project.state.portable_namespace(resources.run_id, ROADMAP_NAMESPACE)
    owner = RoadmapOwner(DocumentRoadmap(namespace))
    return tuple(
        OperationEntry.owned(registration, owner)
        for registration in roadmap_operation_registrations()
    )


__all__ = ["ROADMAP_NAMESPACE", "RoadmapOwner", "roadmap_entries"]
