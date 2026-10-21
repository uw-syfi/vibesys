"""Permanent lowering of retained serial round records, including pre-trace runs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

from pydantic import BeforeValidator, TypeAdapter

from vibesys.hypothesis import RoundRecord, parse_round_record
from vs_core.api.values import Reading
from vs_proto.api import core_leaves
from vs_proto.api import imported_facts as pb
from vs_runtime.api.core_values import to_proto

from ._source import decode

if TYPE_CHECKING:
    from vs_core.api import OperationRegistry

    from ._facts import FactSet
    from ._source import ImportSource


def _round(value: dict[str, Any]) -> RoundRecord:
    return parse_round_record(value)


_ROUND = TypeAdapter(Annotated[RoundRecord, BeforeValidator(_round)])


def import_round(source: ImportSource, registry: OperationRegistry, facts: FactSet) -> None:
    record = read_round(source, registry)
    lower_round(source, "$", record, facts)


def read_round(
    source: ImportSource,
    registry: OperationRegistry,
    *,
    selector: str = "$",
    data: bytes | None = None,
) -> RoundRecord:
    return decode(source, _ROUND, registry, selector=selector, data=data)


def lower_round(source: ImportSource, path: str, record: RoundRecord, facts: FactSet) -> None:
    """Never promote an implementer's number or manufacture absent sample identity."""
    if record.commit is not None:
        facts.add(source, f"{path}.commit", pb.ImportedCandidate(git_commit=record.commit))
    if record.perf_metric is None or record.perf_provenance != "framework":
        return
    reading = Reading(
        metric="headline",
        value=record.perf_metric,
        unit=record.perf_unit,
        direction=record.perf_direction,
    )
    encoded = cast("core_leaves.Reading", to_proto(reading))
    facts.add(
        source,
        f"{path}.perf_metric",
        pb.ImportedMeasurement(
            historical_reading=encoded,
            historical_commit=record.commit,
            historical_artifact=record.evaluation_artifact,
        ),
    )
