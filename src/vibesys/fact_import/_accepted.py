"""Shared facts from owner-decoded, evidence-keyed readings in retained snapshots.

An absent protocol is the original AcceptedReading v1 default, pinned by the golden.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from vs_core.api.values import EvidenceId, EvidenceKey, Reading, RequestId
from vs_proto.api import core_leaves, core_vocabulary
from vs_proto.api import imported_facts as pb
from vs_runtime.api.core_values import to_proto

if TYPE_CHECKING:
    from collections.abc import Mapping


def accepted_measurement(value: Mapping[str, Any]) -> pb.ImportedMeasurement:
    partial = value.get("partial")
    return pb.ImportedMeasurement(
        accepted_reading=pb.ImportedAcceptedReading(
            key=cast(
                "core_leaves.EvidenceKey",
                to_proto(
                    EvidenceKey(
                        source_request=RequestId(root=value["source_request"]["root"]),
                        evidence_id=EvidenceId(root=value["evidence_id"]["root"]),
                    )
                ),
            ),
            kind=core_vocabulary.EvidenceKind[value["kind"].upper()],
            passed=value["passed"],
            stage=value["stage"],
            protocol=str(value.get("protocol", 1)),
            readings=[_reading(row) for row in value.get("metrics", ())],
            partial=None if partial is None else _partial(partial),
            feedback=value.get("feedback", ""),
            unmet_requirement=value.get("unmet_requirement"),
        )
    )


def _reading(value: Mapping[str, Any]) -> core_leaves.Reading:
    return cast(
        "core_leaves.Reading",
        to_proto(
            Reading(
                metric=value["name"],
                value=value["value"],
                unit=value.get("unit"),
                direction=value["direction"],
            )
        ),
    )


def _partial(value: Mapping[str, Any]) -> pb.ImportedPartialMeasurement:
    return pb.ImportedPartialMeasurement(
        reading=_reading(value),
        target=value.get("target"),
        completed=None if value.get("completed") is None else str(value["completed"]),
        required=None if value.get("required") is None else str(value["required"]),
        unit=value.get("progress_unit"),
    )
