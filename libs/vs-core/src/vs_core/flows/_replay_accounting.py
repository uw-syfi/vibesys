"""One coverage-aware fold of native and imported accounting evidence.

Canonical identity is (dimension, chargeable occurrence). Observations are
cumulative: a higher owner revision replaces a lower one, and readings are never
summed across revisions. An aggregate is a total over declared disjoint cells,
never another occurrence: for aggregate ``A`` with cell baselines ``b_i`` at its
watermark and latest readings ``v_i``, its total is ``A + sum(v_i - b_i)``.
Unknown quantities stay unknown; they are never guessed as zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING

from protobuf import Oneof

from vs_proto.api import replay as pb

from ._replay_sources import ReplayError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from vs_proto.api import Record, Span

Dimension = pb.AccountingDimension
type Occurrence = tuple[Dimension, pb.AccountingOccurrenceKind, str]

_NATIVE_TOKENS = (
    (Dimension.INPUT_TOKENS, "input"),
    (Dimension.CACHE_READ_TOKENS, "cache_read"),
    (Dimension.CACHE_CREATION_TOKENS, "cache_creation"),
    (Dimension.OUTPUT_TOKENS, "output"),
)


@dataclass(frozen=True)
class AccountingAmount:
    """A known part and whether any unknown quantity also contributes."""

    known: Fraction = Fraction(0)
    unknown: bool = False

    def __add__(self, other: AccountingAmount) -> AccountingAmount:
        return AccountingAmount(self.known + other.known, self.unknown or other.unknown)

    def __sub__(self, other: AccountingAmount) -> AccountingAmount:
        return AccountingAmount(self.known - other.known, self.unknown or other.unknown)


def quantity(value: pb.AccountingQuantity | None, refunded: int = 0) -> AccountingAmount:
    """Net charged minus refunded, derived once; exact for counts and amounts."""
    match None if value is None else value.value:
        case Oneof("count", count):
            result = AccountingAmount(Fraction(count))
        case Oneof("amount", known):
            result = AccountingAmount(Fraction(known))
        case Oneof("unknown", _):
            result = AccountingAmount(unknown=True)
        case _:
            problem = "accounting quantity is required"
            raise ReplayError(problem)
    if not result.unknown and refunded > result.known:
        problem = "an accounting refund exceeds its charge"
        raise ReplayError(problem)
    return result - AccountingAmount(Fraction(refunded))


def _key(dimension: Dimension, occurrence: pb.AccountingOccurrence | None) -> Occurrence:
    if occurrence is None:
        problem = "accounting evidence requires its chargeable occurrence"
        raise ReplayError(problem)
    return dimension, occurrence.kind, occurrence.id


@dataclass(frozen=True)
class _Reading:
    revision: int
    origin_epoch: int
    value: bytes

    @property
    def amount(self) -> AccountingAmount:
        observation = pb.AccountingObservation.from_binary(self.value)
        return quantity(observation.charged, observation.refunded)


@dataclass
class AccountingLedger:
    """Validated accounting evidence from a committed record prefix.

    Repeated evidence deduplicates; conflicting quantities at one revision,
    republished imports with different content, ambiguous aliases and
    overlapping or negative-residual coverage fail validation.
    """

    _readings: dict[Occurrence, _Reading] = field(default_factory=dict)
    _coverage: dict[str, bytes] = field(default_factory=dict)
    _aliases: dict[str, Occurrence] = field(default_factory=dict)
    _imports: dict[str, bytes] = field(default_factory=dict)
    _parents: set[str] = field(default_factory=set)
    _starts: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_records(cls, records: Iterable[Record]) -> AccountingLedger:
        ledger = cls()
        for record in records:
            ledger.fold(record)
        ledger.totals()
        return ledger

    def fold(self, record: Record) -> None:
        match record.body:
            case Oneof("span", change) if change.span is not None:
                span = change.span
                origin = self._starts.setdefault(span.id, record.epoch)
                if span.replay_physical is not None:
                    self._parents.add(span.replay_physical.logical)
                for observation in _native(span, record.seq, origin):
                    self._observe(observation)
            case Oneof("notice", notice) if (
                notice.detail is not None
                and notice.detail.type_url.rsplit("/", 1)[-1]
                == pb.AccountingEvidence.desc().type_name
            ):
                evidence = pb.AccountingEvidence.from_binary(notice.detail.value)
                self._evidence(evidence, record.epoch)
            case _:
                pass

    def published(self, import_id: str) -> pb.AccountingEvidence | None:
        """The import publication already in the prefix, if any."""
        value = self._imports.get(import_id)
        return None if value is None else pb.AccountingEvidence.from_binary(value)

    def _evidence(self, evidence: pb.AccountingEvidence, epoch: int) -> None:
        if evidence.has_field("import_id"):
            published = evidence.to_binary()
            if self._imports.setdefault(evidence.import_id, published) != published:
                problem = f"import {evidence.import_id} republished with different accounting"
                raise ReplayError(problem)
            for coverage in evidence.coverage:
                value = coverage.to_binary()
                if self._coverage.setdefault(coverage.id, value) != value:
                    problem = f"conflicting accounting coverage {coverage.id}"
                    raise ReplayError(problem)
        for observation in evidence.observations:
            if observation.origin_epoch > epoch:
                problem = "an accounting observation cannot originate in a later epoch"
                raise ReplayError(problem)
            self._observe(observation)

    def _observe(self, observation: pb.AccountingObservation) -> None:
        key = _key(observation.dimension, observation.occurrence)
        quantity(observation.charged, observation.refunded)
        reading = _Reading(observation.revision, observation.origin_epoch, observation.to_binary())
        prior = self._readings.get(key)
        if prior is not None and prior.origin_epoch != reading.origin_epoch:
            problem = f"occurrence {key[2]} changes its origin epoch"
            raise ReplayError(problem)
        if prior is not None and prior.revision == reading.revision and prior != reading:
            problem = f"conflicting accounting quantities for {key[2]} at one revision"
            raise ReplayError(problem)
        for alias in observation.aliases:
            if self._aliases.setdefault(alias, key) != key:
                problem = f"accounting alias {alias} names two occurrences"
                raise ReplayError(problem)
        if prior is None or reading.revision > prior.revision:
            self._readings[key] = reading

    def totals(self, *, origin_epoch: int | None = None) -> dict[Dimension, AccountingAmount]:
        """Lifetime totals, or only occurrences that originated in one native epoch.

        Imported coverage and imported occurrences (origin zero) never belong to
        a native epoch, so historical deltas update lifetime usage only.
        """
        coverages = {
            identity: pb.AccountingCoverage.from_binary(value)
            for identity, value in self._coverage.items()
        }
        cells, nested = _partition(coverages)
        totals: dict[Dimension, AccountingAmount] = {}
        if origin_epoch is None:
            for identity, coverage in coverages.items():
                if identity not in nested:
                    totals[coverage.dimension] = totals.get(
                        coverage.dimension, AccountingAmount()
                    ) + self._covered(coverage, coverages)
        for key, reading in self._readings.items():
            if key in cells or self._logical_copy(key):
                continue
            if origin_epoch is None or reading.origin_epoch == origin_epoch:
                totals[key[0]] = totals.get(key[0], AccountingAmount()) + reading.amount
        return totals

    def _logical_copy(self, key: Occurrence) -> bool:
        """A logical parent's observation copies its accepted child; it is decode-only."""
        return key[1] == pb.AccountingOccurrenceKind.INVOCATION and key[2] in self._parents

    def _covered(
        self, coverage: pb.AccountingCoverage, coverages: dict[str, pb.AccountingCoverage]
    ) -> AccountingAmount:
        aggregate = quantity(coverage.total)
        floor = aggregate.known
        total = aggregate
        for cell in coverage.cells:
            baseline = quantity(cell.baseline)
            floor -= baseline.known
            reading = self._readings.get(_key(coverage.dimension, cell.occurrence))
            if reading is not None and reading.revision > coverage.watermark:
                total += _delta(reading.amount, baseline)
        for identity in coverage.nested:
            child = coverages[identity]
            floor -= quantity(child.total).known
            total += self._covered(child, coverages) - quantity(child.total)
        if not aggregate.unknown and floor < 0:
            problem = f"accounting coverage {coverage.id} has a negative residual"
            raise ReplayError(problem)
        return total


def _delta(latest: AccountingAmount, baseline: AccountingAmount) -> AccountingAmount:
    """A late cumulative reading transfers only its growth past the covered baseline."""
    if latest.unknown or baseline.unknown:
        return AccountingAmount(unknown=True)
    if latest.known < baseline.known:
        problem = "a cumulative accounting reading cannot decrease"
        raise ReplayError(problem)
    return latest - baseline


def _partition(
    coverages: dict[str, pb.AccountingCoverage],
) -> tuple[set[Occurrence], set[str]]:
    """Every cell and nested aggregate belongs to exactly one covering aggregate."""
    cells: set[Occurrence] = set()
    nested: dict[str, str] = {}
    for coverage in coverages.values():
        for cell in coverage.cells:
            key = _key(coverage.dimension, cell.occurrence)
            if key in cells:
                problem = f"occurrence {key[2]} is covered by two aggregates"
                raise ReplayError(problem)
            cells.add(key)
        for identity in coverage.nested:
            child = coverages.get(identity)
            if child is None or child.dimension != coverage.dimension:
                problem = f"accounting coverage {coverage.id} nests unknown {identity}"
                raise ReplayError(problem)
            if nested.setdefault(identity, coverage.id) != coverage.id:
                problem = f"accounting coverage {identity} is nested twice"
                raise ReplayError(problem)
    for identity in nested:
        seen = {identity}
        parent = nested.get(identity)
        while parent is not None:
            if parent in seen:
                problem = f"accounting coverage {identity} nests itself"
                raise ReplayError(problem)
            seen.add(parent)
            parent = nested.get(parent)
    return cells, set(nested)


def _native(span: Span, revision: int, origin: int) -> Iterator[pb.AccountingObservation]:
    """Readings of one observed replay span; a paid result never reports a guessed zero.

    The occurrence keeps the epoch that started it, even when observed after Resume.
    """
    observed = span.replay_observation
    if span.replay is None or observed is None:
        return
    occurrence = pb.AccountingOccurrence(kind=pb.AccountingOccurrenceKind.INVOCATION, id=span.id)
    usage = observed.usage

    def reading(dimension: Dimension, value: pb.AccountingQuantity) -> pb.AccountingObservation:
        return pb.AccountingObservation(
            occurrence=occurrence,
            dimension=dimension,
            revision=revision,
            charged=value,
            origin_epoch=origin,
        )

    if observed.has_field("cost_usd"):
        money = pb.AccountingQuantity(value=Oneof("amount", observed.cost_usd))
    else:
        money = _unreported(paid=observed.paid, zero=Oneof("amount", 0.0))
    yield reading(Dimension.MONEY_USD, money)
    for dimension, name in _NATIVE_TOKENS:
        if usage is not None and usage.has_field(name):
            tokens = pb.AccountingQuantity(value=Oneof("count", getattr(usage, name)))
        else:
            tokens = _unreported(paid=observed.paid, zero=Oneof("count", 0))
        yield reading(dimension, tokens)


def _unreported(*, paid: bool, zero: Oneof) -> pb.AccountingQuantity:
    return pb.AccountingQuantity(value=Oneof("unknown", value=True) if paid else zero)
