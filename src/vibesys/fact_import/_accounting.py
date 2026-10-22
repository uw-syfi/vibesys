"""Lower imported usage to native accounting evidence, or name why it cannot be.

Imported facts never become paid calls. Each imported reading keeps its
occurrence and origin (epoch zero). An aggregate becomes coverage over declared
cells; the retained formats never record which per-occurrence readings an
aggregate includes, so an aggregate beside per-occurrence readings of the same
dimension is unreconciled rather than guessed, summed or maxed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from protobuf import Oneof

from vs_proto.api import imported_facts as pb
from vs_proto.api import replay

from ._source import FactImportError

if TYPE_CHECKING:
    from collections.abc import Iterable

Dimension = replay.AccountingDimension

_DIMENSIONS = {
    "total_cost_usd": Dimension.MONEY_USD,
    "input_tokens": Dimension.INPUT_TOKENS,
    "cache_read_input_tokens": Dimension.CACHE_READ_TOKENS,
    "cache_creation_input_tokens": Dimension.CACHE_CREATION_TOKENS,
    "output_tokens": Dimension.OUTPUT_TOKENS,
    "reasoning_output_tokens": Dimension.REASONING_TOKENS,
    "duration_ms": Dimension.DURATION_MS,
    "admission": Dimension.ADMISSIONS,
    "attempt": Dimension.ATTEMPTS,
    "turn": Dimension.TURNS,
    "retry_budget_spent": Dimension.RETRY_BUDGET,
}
"""Every unit the owning decoders emit; any other unit is an importer defect."""

_INVOCATION_USAGE = frozenset(
    {
        Dimension.MONEY_USD,
        Dimension.INPUT_TOKENS,
        Dimension.CACHE_READ_TOKENS,
        Dimension.CACHE_CREATION_TOKENS,
        Dimension.OUTPUT_TOKENS,
        Dimension.REASONING_TOKENS,
        Dimension.DURATION_MS,
    }
)
"""Dimensions a still-running imported invocation can report after publication."""


@dataclass(frozen=True)
class AccountingUnreconciled:
    """An import prerequisite: overlapping sources whose inclusion is not recorded."""

    dimension: replay.AccountingDimension
    sources: tuple[str, ...]


def accounting_evidence(
    facts: pb.ImportedRunFacts, import_id: str
) -> replay.AccountingEvidence | AccountingUnreconciled:
    """Publish every imported usage once; overlapping aggregates block publication."""
    usages = [
        (fact.value.value, fact.imported_from)
        for fact in facts.facts
        if fact.value is not None and fact.value.field == "usage"
    ]
    obligations = sorted(
        fact.value.value.identity
        for fact in facts.facts
        if fact.value is not None
        and fact.value.field == "external_identity"
        and fact.value.value.kind == "invocation"
    )
    aggregates = {
        _dimension(usage) for usage, _ in usages if usage.scope == pb.ImportedUsageScope.AGGREGATE
    }
    for dimension in sorted(aggregates, key=lambda item: item.value):
        same = [(usage, origins) for usage, origins in usages if _dimension(usage) == dimension]
        if any(usage.scope != pb.ImportedUsageScope.AGGREGATE for usage, _ in same):
            return AccountingUnreconciled(dimension, _paths(origins for _, origins in same))
    evidence = replay.AccountingEvidence(import_id=import_id)
    for usage, _ in usages:
        dimension = _dimension(usage)
        if usage.scope == pb.ImportedUsageScope.AGGREGATE:
            evidence.coverage.append(_coverage(usage, dimension, obligations))
        else:
            evidence.observations.append(_observation(usage, dimension))
    return evidence


def _dimension(usage: pb.ImportedUsage) -> replay.AccountingDimension:
    dimension = _DIMENSIONS.get(usage.unit)
    if dimension is None:
        raise FactImportError(usage.identity, f"unsupported accounting unit {usage.unit}")
    return dimension


def _paths(groups: Iterable[Iterable[pb.ImportedFrom]]) -> tuple[str, ...]:
    return tuple(
        sorted({f"{origin.path}:{origin.selector}" for group in groups for origin in group})
    )


def _quantity(usage: pb.ImportedUsage) -> replay.AccountingQuantity:
    match usage.quantity:
        case Oneof("count", count):
            value = Oneof("count", int(count))
        case Oneof("amount", amount):
            value = Oneof("amount", amount)
        case _:
            value = Oneof("unknown", value=True)
    return replay.AccountingQuantity(value=value)


def _observation(
    usage: pb.ImportedUsage, dimension: replay.AccountingDimension
) -> replay.AccountingObservation:
    kind = (
        replay.AccountingOccurrenceKind.CHARGE
        if usage.scope == pb.ImportedUsageScope.CHARGE
        else replay.AccountingOccurrenceKind.INVOCATION
    )
    return replay.AccountingObservation(
        occurrence=replay.AccountingOccurrence(kind=kind, id=usage.identity),
        dimension=dimension,
        charged=_quantity(usage),
        refunded=int(usage.refunded_count or "0"),
        aliases=[usage.source_request] if usage.has_field("source_request") else [],
    )


def _coverage(
    usage: pb.ImportedUsage, dimension: replay.AccountingDimension, obligations: list[str]
) -> replay.AccountingCoverage:
    """Unfinished imported invocations may have been counted: their baseline is unknown."""
    total = _quantity(usage)
    refunded = int(usage.refunded_count or "0")
    if total.value is not None and total.value.field == "count":
        total = replay.AccountingQuantity(value=Oneof("count", total.value.value - refunded))
    cells = (
        [
            replay.AccountingCell(
                occurrence=replay.AccountingOccurrence(
                    kind=replay.AccountingOccurrenceKind.INVOCATION, id=identity
                ),
                baseline=replay.AccountingQuantity(value=Oneof("unknown", value=True)),
            )
            for identity in obligations
        ]
        if dimension in _INVOCATION_USAGE
        else []
    )
    return replay.AccountingCoverage(
        id=usage.identity, dimension=dimension, total=total, cells=cells
    )
