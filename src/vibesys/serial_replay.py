"""Native request codecs for the serial searches' existing effect owners.

Roadmap and attribution execution, cleanup and physical recovery stay with
RoadmapOwner and CaptureCommandOwner. These codecs only cross their retained
legacy value boundary; new replay requests and outcomes are protobuf messages.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vs_core.api import CaptureCommandBase, CommandCapture, RevisionRef
from vs_issue_tracker.api import (
    PutRoadmapItems,
    ReadRoadmap,
    RoadmapItem,
    RoadmapItemKind,
    RoadmapItemsPut,
    RoadmapSnapshot,
    RoadmapStatus,
)
from vs_proto.api import serial_effects as pb
from vs_runtime.api.core_values import from_proto
from vs_runtime.api.replay import ReplayOperationBinding

if TYPE_CHECKING:
    from protobuf import Message
    from pydantic import BaseModel

_STATUS = {
    "succeeded": pb.SerialEffectStatus.SUCCEEDED,
    "rejected": pb.SerialEffectStatus.REJECTED,
    "failed": pb.SerialEffectStatus.FAILED,
}
_KIND = {RoadmapItemKind.MAJOR: pb.RoadmapKind.MAJOR, RoadmapItemKind.MINOR: pb.RoadmapKind.MINOR}
_PROGRESS = {
    RoadmapStatus.TODO: pb.RoadmapProgress.TODO,
    RoadmapStatus.IN_PROGRESS: pb.RoadmapProgress.IN_PROGRESS,
    RoadmapStatus.DONE: pb.RoadmapProgress.DONE,
    RoadmapStatus.PARKED: pb.RoadmapProgress.PARKED,
    RoadmapStatus.ABANDONED: pb.RoadmapProgress.ABANDONED,
}


def roadmap_item(value: RoadmapItem) -> pb.SerialRoadmapItem:
    """The exact native spelling of an existing owner item."""
    return pb.SerialRoadmapItem(
        id=value.id,
        kind=_KIND[value.kind],
        title=value.title,
        status=_PROGRESS[value.status],
        major=value.major,
        note=value.note,
    )


def _item(value: pb.SerialRoadmapItem) -> RoadmapItem:
    return RoadmapItem(
        id=value.id,
        kind={native: legacy for legacy, native in _KIND.items()}[value.kind],
        title=value.title,
        status={native: legacy for legacy, native in _PROGRESS.items()}[value.status],
        major=value.major if value.has_field("major") else None,
        note=value.note,
    )


def _read(value: Message) -> ReadRoadmap:
    del value
    return ReadRoadmap()


def _put(value: Message) -> PutRoadmapItems:
    asked = cast("pb.PutSerialRoadmap", value)
    return PutRoadmapItems(items=tuple(_item(item) for item in asked.items))


def _snapshot(value: BaseModel) -> pb.SerialRoadmapRead:
    result = cast("RoadmapSnapshot", value)
    return pb.SerialRoadmapRead(
        status=_STATUS[result.status],
        items=[roadmap_item(item) for item in result.items],
        detail=result.detail,
    )


def _put_result(value: BaseModel) -> pb.SerialRoadmapPut:
    result = cast("RoadmapItemsPut", value)
    return pb.SerialRoadmapPut(status=_STATUS[result.status], detail=result.detail)


def _captured(value: BaseModel) -> pb.SerialAttributionCaptured:
    result = cast("CommandCapture", value)
    return pb.SerialAttributionCaptured(
        status=_STATUS[result.status],
        exit_code=result.exit_code,
        output=result.output,
        truncated=result.truncated,
        detail=result.detail,
    )


def serial_replay_bindings(
    capture: type[CaptureCommandBase], *, within_seconds: float
) -> tuple[ReplayOperationBinding, ...]:
    """Bind native roadmap and attribution requests to their existing owner kinds.

    ``within_seconds`` is the caller's declared bound. The captured command's
    own timeout is checked against it before new dispatch. Inspection decodes
    the original request under its retained bound, so a tighter current policy
    cannot prevent recovery of an already executed capture.
    """

    def decode(value: Message) -> CaptureCommandBase:
        asked = cast("pb.CaptureSerialAttribution", value)
        if asked.revision is None:
            problem = "capture attribution requires a revision"
            raise ValueError(problem)
        revision = from_proto(asked.revision)
        if not isinstance(revision, RevisionRef):
            problem = "capture attribution revision must be a RevisionRef"
            raise TypeError(problem)
        return capture(
            kind=kind,
            revision=revision,
            argv=tuple(asked.argv),
            output_argument=asked.output_argument,
            timeout_seconds=asked.timeout_seconds,
        )

    def check(value: Message) -> None:
        asked = cast("pb.CaptureSerialAttribution", value)
        if asked.timeout_seconds > within_seconds:
            problem = "capture attribution timeout_seconds exceeds the declared bound"
            raise ValueError(problem)

    kind = capture.model_fields["kind"].default
    return (
        ReplayOperationBinding(
            "roadmap.read",
            "roadmap.read",
            pb.ReadSerialRoadmap,
            pb.SerialRoadmapRead,
            _read,
            _snapshot,
            within_seconds,
            paid=False,
        ),
        ReplayOperationBinding(
            "roadmap.put_items",
            "roadmap.put_items",
            pb.PutSerialRoadmap,
            pb.SerialRoadmapPut,
            _put,
            _put_result,
            within_seconds,
            paid=False,
        ),
        ReplayOperationBinding(
            "attribution.capture",
            kind,
            pb.CaptureSerialAttribution,
            pb.SerialAttributionCaptured,
            decode,
            _captured,
            within_seconds,
            paid=True,
            dispatch_check=check,
        ),
    )


__all__ = ["roadmap_item", "serial_replay_bindings"]
