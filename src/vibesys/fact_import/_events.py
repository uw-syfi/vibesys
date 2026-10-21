"""Facts recoverable from retained event journals, including runs without a trace."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import TypeAdapter

from vibesys.events import (
    AgentExecutionStartedData,
    CoreEvent,
    CoreEventType,
    FrameworkSource,
    InvocationStartedData,
    MeasurementComparedData,
    TurnUsageData,
    WinnerSelectedData,
    WorkspaceSnapshotData,
)
from vs_core.api.values import Reading
from vs_proto.api import core_leaves
from vs_proto.api import imported_facts as pb
from vs_runtime.api.core_values import to_proto

from ._facts import usage
from ._source import FactImportError, decode

if TYPE_CHECKING:
    from vs_core.api import OperationRegistry

    from ._facts import FactSet
    from ._source import ImportSource

_USAGE_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_cost_usd",
    "duration_ms",
)


def import_events(
    source: ImportSource, registry: OperationRegistry, facts: FactSet, run_id: str
) -> None:
    rows = [
        decode(source, TypeAdapter(CoreEvent), registry, selector=f"line[{index}]", data=line)
        for index, line in enumerate(source.data.splitlines(), start=1)
    ]
    sequences = [row.sequence for row in rows if row.sequence]
    if sequences and sequences != list(range(1, len(rows) + 1)):
        raise FactImportError(source.path, "sequence: expected a complete ordered legacy journal")
    ended = {
        row.execution_id
        for row in rows
        if row.type in (CoreEventType.INVOCATION_FINISHED, CoreEventType.AGENT_EXECUTION_FINISHED)
    }
    for index, row in enumerate(rows, start=1):
        if row.run_id and row.run_id != run_id:
            raise FactImportError(
                f"{source.path}:line[{index}].run_id", "source belongs to another run"
            )
        if (
            isinstance(
                row.data,
                TurnUsageData
                | WorkspaceSnapshotData
                | WinnerSelectedData
                | MeasurementComparedData,
            )
            and row.type.value != row.data.kind
        ):
            raise FactImportError(f"{source.path}:line[{index}].type", "does not match data.kind")
        _event(source, f"line[{index}]", row, facts)
        if (
            isinstance(row.data, AgentExecutionStartedData | InvocationStartedData)
            and row.execution_id
            and row.execution_id not in ended
        ):
            facts.external(source, f"line[{index}]", "invocation", row.execution_id)


def _event(source: ImportSource, path: str, row: CoreEvent, facts: FactSet) -> None:
    value = row.data
    if isinstance(value, TurnUsageData):
        identity = row.execution_id or value.span_id or f"{source.path}:{path}"
        for name in _USAGE_FIELDS:
            facts.add(
                source,
                f"{path}.data.{name}",
                usage(
                    identity=identity,
                    unit=name,
                    amount=getattr(value, name),
                    scope=pb.ImportedUsageScope.INVOCATION,
                ),
                identity=f"{identity}:{name}",
            )
    elif isinstance(value, WorkspaceSnapshotData):
        for revision in (value.commit, value.baseline):
            if revision:
                facts.add(source, path, pb.ImportedCandidate(git_commit=revision))
    elif isinstance(value, WinnerSelectedData):
        facts.add(source, path, pb.ImportedCandidate(git_commit=value.revision))
    elif isinstance(value, MeasurementComparedData) and value.source == FrameworkSource.GATES:
        _comparison(source, path, value, facts)


def _comparison(
    source: ImportSource, path: str, value: MeasurementComparedData, facts: FactSet
) -> None:
    for side, revision, amount in (
        ("candidate", value.candidate_commit, value.candidate_value),
        ("base", value.base_commit, value.base_value),
    ):
        reading = Reading(
            metric=value.metric, value=amount, unit=value.unit, direction=value.direction
        )
        facts.add(
            source,
            f"{path}.{side}",
            pb.ImportedMeasurement(
                historical_commit=revision,
                historical_reading=cast("core_leaves.Reading", to_proto(reading)),
            ),
        )
