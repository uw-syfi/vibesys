"""Lower legacy loop facts without following its saved phase or lifecycle transitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vs_core.api.values import Reading
from vs_proto.api import core_leaves
from vs_proto.api import imported_facts as pb
from vs_runtime.api.core_values import to_proto

from ._facts import usage, walk
from ._legacy_dynamic import (
    AgentLoopState,
    DependencyContinuation,
    DynamicProfile,
    DynamicWorkstream,
    EvaluationDependency,
    EvaluationResult,
    IntentStage,
    LifecycleIntent,
    MeasuredIteration,
    ProfilerDependency,
    SteerNote,
    VerifiedCandidate,
)

if TYPE_CHECKING:
    from ._facts import FactSet
    from ._source import ImportSource


def lower_legacy(source: ImportSource, value: object, facts: FactSet) -> None:
    for path, item in walk("$", value):
        _lower_value(source, path, item, facts)


def _lower_value(source: ImportSource, path: str, item: object, facts: FactSet) -> None:
    if isinstance(item, DynamicWorkstream):
        for revision in (item.parent_revision, item.candidate_revision, item.prior_revision):
            if revision:
                facts.add(source, path, pb.ImportedCandidate(git_commit=revision))
        spent = item.attempts if item.attempts is not None else item.budget.spent
        facts.add(
            source,
            path,
            usage(identity=f"workstream:{item.sequence}", unit="attempt", amount=spent),
        )
        if item.feedback:
            facts.add(
                source,
                f"{path}.feedback",
                pb.ImportedGuidance(
                    identity=f"workstream:{item.sequence}:feedback", text=item.feedback
                ),
            )
    elif isinstance(item, EvaluationResult | VerifiedCandidate):
        _measurement(source, path, item, facts)
    elif isinstance(item, MeasuredIteration):
        _reading(
            source,
            path,
            item.revision,
            Reading(
                metric=item.name,
                value=item.value,
                unit=item.unit,
                direction=item.direction.value,
            ),
            facts,
        )
    elif isinstance(item, SteerNote):
        facts.add(
            source,
            path,
            pb.ImportedGuidance(
                identity=path,
                text=item.text,
                delivered=item.delivered_to is not None,
                withdrawn=item.dropped is not None,
                possibly_read=item.reserved_to is not None,
            ),
        )
    elif isinstance(item, AgentLoopState):
        for unit, amount in (
            ("input_tokens", item.input_tokens),
            ("output_tokens", item.output_tokens),
        ):
            facts.add(source, path, usage(identity=f"agent:{unit}", unit=unit, amount=amount))
    else:
        _external(source, path, item, facts)


def _external(source: ImportSource, path: str, item: object, facts: FactSet) -> None:
    if isinstance(item, EvaluationDependency | ProfilerDependency):
        facts.external(source, path, "dependency", item.handle)
    elif isinstance(item, DependencyContinuation):
        facts.external(source, path, "session", item.session_key)
        facts.external(source, path, "invocation", item.yielded_invocation_id)
    elif isinstance(item, LifecycleIntent) and item.stage != IntentStage.COMPLETED:
        facts.external(source, path, item.kind.value, item.operation_id)
    elif (
        isinstance(item, DynamicProfile) and item.outcome is not None and item.outcome.operation_id
    ):
        facts.external(source, path, "profile", item.outcome.operation_id)


def _measurement(
    source: ImportSource, path: str, item: EvaluationResult | VerifiedCandidate, facts: FactSet
) -> None:
    facts.add(source, path, pb.ImportedCandidate(git_commit=item.revision))
    if item.metric_value is not None and item.metric_name:
        _reading(
            source,
            path,
            item.revision,
            Reading(
                metric=item.metric_name,
                value=item.metric_value,
                unit=item.metric_unit,
                direction=None if item.metric_direction is None else item.metric_direction.value,
            ),
            facts,
        )
    if isinstance(item, EvaluationResult):
        for name, value in sorted(item.metrics.items()):
            if name != item.metric_name:
                _reading(
                    source,
                    f"{path}.metrics.{name}",
                    item.revision,
                    Reading(metric=name, value=value),
                    facts,
                )


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
