"""The roadmap as core operations: read its items, put a batch of them.

A strategy on the vs-core lifecycle never calls a `Roadmap`. It declares what it wants as an
operation and a runtime owner performs it over the roadmap the run opened, so every change is
one durable request whose outcome returns as one event.

- `ReadRoadmap` is a query; the roadmap stays the source of truth.
- `PutRoadmapItems` is an idempotent write: putting a batch again leaves the same items, so a
  replay after a crash is safe, and the owner says the batch landed when the roadmap holds
  every item of it exactly.

Request and outcome models are immutable values that need only `vs_core`.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from vs_core.api import (
    LifecycleClass,
    OperationDescriptor,
    OperationRegistration,
    OperationRequest,
    SchemaRef,
    Value,
)
from vs_issue_tracker.operations import OutcomeStatus
from vs_issue_tracker.roadmap import RoadmapItem

ROADMAP_READ_KIND = "roadmap.read"
ROADMAP_PUT_KIND = "roadmap.put_items"


class RoadmapSnapshot(Value):
    """The roadmap's items at one moment, in the order each id was first put."""

    status: OutcomeStatus
    items: tuple[RoadmapItem, ...] = ()
    detail: str = ""


class RoadmapItemsPut(Value):
    """The outcome of a batch: succeeded, or rejected with the reason, or failed."""

    status: OutcomeStatus
    detail: str = ""


class ReadRoadmap(OperationRequest):
    """Read every roadmap item."""

    kind: Literal["roadmap.read"] = "roadmap.read"
    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = RoadmapSnapshot


class PutRoadmapItems(OperationRequest):
    """Create or replace each item by its id, as one batch; replaying it changes nothing more."""

    kind: Literal["roadmap.put_items"] = "roadmap.put_items"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = RoadmapItemsPut
    items: tuple[RoadmapItem, ...] = Field(min_length=1)


def _registration(
    kind: str, request: type[OperationRequest], outcome: type[BaseModel]
) -> OperationRegistration:
    stem = kind.removeprefix("roadmap.").replace("_", "-")
    lifecycle = request.model_fields["lifecycle"].default
    return OperationRegistration(
        descriptor=OperationDescriptor(
            kind=kind,
            request_schema=SchemaRef(name=f"roadmap.{stem}", version=1),
            outcome_schema=SchemaRef(name=f"roadmap.{stem}-outcome", version=1),
            lifecycle=lifecycle,
            inspect=lifecycle is not LifecycleClass.QUERY,
        ),
        request_model=request,
        outcome_model=outcome,
    )


def roadmap_operation_registrations() -> tuple[OperationRegistration, ...]:
    """The codec registrations of the two roadmap operations, in a fixed order."""
    return (
        _registration(ROADMAP_READ_KIND, ReadRoadmap, RoadmapSnapshot),
        _registration(ROADMAP_PUT_KIND, PutRoadmapItems, RoadmapItemsPut),
    )


__all__ = [
    "ROADMAP_PUT_KIND",
    "ROADMAP_READ_KIND",
    "PutRoadmapItems",
    "ReadRoadmap",
    "RoadmapItemsPut",
    "RoadmapSnapshot",
    "roadmap_operation_registrations",
]
