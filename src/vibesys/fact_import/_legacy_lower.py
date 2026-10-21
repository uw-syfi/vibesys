"""Lower schema-validated legacy values without interpreting their saved control state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from vs_core.api.values import Reading
from vs_proto.api import core_common, core_leaves
from vs_proto.api import imported_facts as pb
from vs_runtime.api.core_values import to_proto

from ._facts import usage

if TYPE_CHECKING:
    from ._facts import FactSet
    from ._source import ImportSource


def lower_legacy_value(
    source: ImportSource, path: str, name: str, item: dict[str, Any], facts: FactSet
) -> None:
    """Field names below belong to frozen read formats, never future strategy policy."""
    reader = _READERS.get(name)
    if reader is not None:
        reader(source, path, item, facts)


def _workstream(source: ImportSource, path: str, item: dict[str, Any], facts: FactSet) -> None:
    for key in ("parent_revision", "candidate_revision", "prior_revision"):
        if revision := item.get(key):
            facts.add(source, f"{path}.{key}", pb.ImportedCandidate(git_commit=revision))
    amount = item.get("attempts")
    if amount is None and item.get("budget") is not None:
        amount = item["budget"].get("spent")
    facts.add(
        source,
        path,
        usage(
            identity=f"workstream:{item['sequence']}",
            unit="attempt",
            amount=amount,
            scope=pb.ImportedUsageScope.AGGREGATE,
        ),
    )
    if feedback := item.get("feedback"):
        facts.add(
            source,
            f"{path}.feedback",
            pb.ImportedGuidance(identity=f"workstream:{item['sequence']}:feedback", text=feedback),
        )


def _measurement(source: ImportSource, path: str, item: dict[str, Any], facts: FactSet) -> None:
    revision = item["revision"]
    facts.add(source, path, pb.ImportedCandidate(git_commit=revision))
    if item.get("metric_value") is not None and item.get("metric_name"):
        _reading(
            source,
            path,
            revision,
            Reading(
                metric=item["metric_name"],
                value=item["metric_value"],
                unit=item.get("metric_unit"),
                direction=item.get("metric_direction"),
            ),
            facts,
        )
    for name, value in sorted(item.get("metrics", {}).items()):
        if name != item.get("metric_name"):
            _reading(
                source, f"{path}.metrics.{name}", revision, Reading(metric=name, value=value), facts
            )


def _iteration(source: ImportSource, path: str, item: dict[str, Any], facts: FactSet) -> None:
    _reading(
        source,
        path,
        item["revision"],
        Reading(
            metric=item["name"],
            value=item["value"],
            unit=item.get("unit"),
            direction=item["direction"],
        ),
        facts,
    )


def _steer(source: ImportSource, path: str, item: dict[str, Any], facts: FactSet) -> None:
    facts.add(
        source,
        path,
        pb.ImportedGuidance(
            identity=f"{path.rsplit('[', 1)[0]}:{item['note_sha256']}",
            text=item["text"],
            mode=core_common.InputMode.INTERRUPT
            if item["interrupt"]
            else core_common.InputMode.NEXT_TURN,
            delivered=item.get("delivered_to") is not None,
            disposed=item.get("dropped") is not None,
            possibly_read=item.get("reserved_to") is not None,
        ),
    )


def _agent(source: ImportSource, path: str, item: dict[str, Any], facts: FactSet) -> None:
    for unit in ("input_tokens", "output_tokens"):
        facts.add(
            source,
            path,
            usage(
                identity=f"agent:{unit}",
                unit=unit,
                amount=item.get(unit),
                scope=pb.ImportedUsageScope.AGGREGATE,
            ),
        )


def _dependency(source: ImportSource, path: str, item: dict[str, Any], facts: FactSet) -> None:
    facts.external(source, path, "dependency", item["handle"])


def _continuation(source: ImportSource, path: str, item: dict[str, Any], facts: FactSet) -> None:
    facts.external(source, path, "session", item["session_key"])
    facts.external(source, path, "invocation", item["yielded_invocation_id"])


def _intent(source: ImportSource, path: str, item: dict[str, Any], facts: FactSet) -> None:
    if item.get("stage") != "completed":
        facts.external(source, path, item["kind"], item["operation_id"])


def _profile(source: ImportSource, path: str, item: dict[str, Any], facts: FactSet) -> None:
    outcome = item.get("outcome")
    if outcome is not None and outcome.get("operation_id"):
        facts.external(source, path, "profile", outcome["operation_id"])


def _reading(
    source: ImportSource, path: str, revision: str, reading: Reading, facts: FactSet
) -> None:
    facts.add(
        source,
        path,
        pb.ImportedMeasurement(
            historical_commit=revision,
            historical_reading=cast("core_leaves.Reading", to_proto(reading)),
        ),
    )


_READERS = {
    "DynamicWorkstream": _workstream,
    "EvaluationResult": _measurement,
    "VerifiedCandidate": _measurement,
    "MeasuredIteration": _iteration,
    "SteerNote": _steer,
    "AgentLoopState": _agent,
    "EvaluationDependency": _dependency,
    "ProfilerDependency": _dependency,
    "DependencyContinuation": _continuation,
    "LifecycleIntent": _intent,
    "DynamicProfile": _profile,
}
