"""The shape of the child lease that run envelopes up to version 43 hold in ``records.children``.

Core no longer has the type (#3475 R5): no run wrote a lease once the restart reconciliation was
gone. A retained run saved earlier may still hold one, and what it names and whether it was
released are facts the import keeps, so the import reads the lease through this frozen model
instead of core's. Only what the import reads is typed; the observations stay as JSON.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, JsonValue

from vs_core.api import RequestId, ResourceId


class LegacyObservationWatermark(BaseModel):
    """The highest observation of one source of a child lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_request: RequestId
    observation: dict[str, JsonValue]


class LegacyChildLease(BaseModel):
    """A sub-resource a job listed, with the observations of the requests that found it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: ResourceId
    scope: dict[str, JsonValue]
    source_requests: tuple[RequestId, ...]
    parent_resources: tuple[ResourceId, ...] = ()
    observation: dict[str, JsonValue] | None = None
    observation_watermarks: tuple[LegacyObservationWatermark, ...] = ()
    watermark_history_complete: bool = False

    @property
    def released(self) -> bool:
        """Whether every source reported the resource released, with its history complete."""
        return self.watermark_history_complete and all(
            mark.observation.get("released") is True for mark in self.observation_watermarks
        )
