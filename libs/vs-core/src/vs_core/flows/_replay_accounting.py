"""One coverage-aware fold of native and imported accounting evidence.

Canonical identity is (dimension, chargeable occurrence); an alias resolves to
the occurrence that declared it. Observations are cumulative: a higher owner
revision replaces a lower one, readings are never summed across revisions, and
the charged part never falls (a refund may lower the net). An aggregate is a
total over declared disjoint cells, never another occurrence: for aggregate
``A`` with cell baselines ``b_i`` at its watermark and latest readings ``v_i``,
its total is ``A + sum(v_i - b_i)``. Unknown quantities stay unknown; they are
never guessed as zero.

Every check runs when its evidence folds, so appending one record costs that
record's evidence, never a refold of the prefix.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from fractions import Fraction
from operator import attrgetter
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


def _amount(value: pb.AccountingQuantity | None) -> tuple[str, AccountingAmount]:
    match None if value is None else value.value:
        case Oneof("count", count):
            return "count", AccountingAmount(Fraction(count))
        case Oneof("amount", known):
            return "amount", AccountingAmount(Fraction(known))
        case Oneof("unknown", _):
            return "unknown", AccountingAmount(unknown=True)
        case _:
            problem = "accounting quantity is required"
            raise ReplayError(problem)


def quantity(
    value: pb.AccountingQuantity | None, refunded: pb.AccountingQuantity | None = None
) -> AccountingAmount:
    """Net charged minus refunded, derived once; exact for counts and amounts."""
    unit, charged = _amount(value)
    if refunded is None:
        return charged
    refund_unit, refund = _amount(refunded)
    if "unknown" not in {unit, refund_unit} and unit != refund_unit:
        problem = "an accounting refund must use its charge's unit"
        raise ReplayError(problem)
    if not charged.unknown and not refund.unknown and refund.known > charged.known:
        problem = "an accounting refund exceeds its charge"
        raise ReplayError(problem)
    return charged - refund


def _key(dimension: Dimension, occurrence: pb.AccountingOccurrence | None) -> Occurrence:
    if occurrence is None:
        problem = "accounting evidence requires its chargeable occurrence"
        raise ReplayError(problem)
    return dimension, occurrence.kind, occurrence.id


@dataclass(frozen=True)
class _Reading:
    revision: int
    origin_epoch: int
    charged: AccountingAmount
    amount: AccountingAmount


@dataclass(frozen=True)
class _Cell:
    coverage: str
    watermark: int
    baseline: AccountingAmount


@dataclass
class AccountingLedger:
    """Validated accounting evidence from a committed record prefix.

    Repeated evidence deduplicates; conflicting quantities at one revision, a
    charge that falls with the revision (whatever the arrival order or unknown
    readings between), republished imports with different content, ambiguous
    aliases and overlapping or negative-residual coverage fail validation.
    A fold that raises leaves the ledger unusable; callers discard it.
    """

    _readings: dict[Occurrence, tuple[_Reading, ...]] = field(default_factory=dict)
    _coverage: dict[str, bytes] = field(default_factory=dict)
    _cells: dict[Occurrence, _Cell] = field(default_factory=dict)
    _nested: dict[str, str] = field(default_factory=dict)
    _aliases: dict[Occurrence, Occurrence] = field(default_factory=dict)
    _imports: dict[str, bytes] = field(default_factory=dict)
    _parents: set[str] = field(default_factory=set)
    _starts: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_records(cls, records: Iterable[Record]) -> AccountingLedger:
        ledger = cls()
        for record in records:
            ledger.fold(record)
        return ledger

    def copy(self) -> AccountingLedger:
        """An independent ledger over the same prefix, for a staged append."""
        return AccountingLedger(
            self._readings.copy(),
            self._coverage.copy(),
            self._cells.copy(),
            self._nested.copy(),
            self._aliases.copy(),
            self._imports.copy(),
            self._parents.copy(),
            self._starts.copy(),
        )

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
            prior = self._imports.setdefault(evidence.import_id, published)
            if prior != published:
                problem = f"import {evidence.import_id} republished with different accounting"
                raise ReplayError(problem)
            if prior is not published:
                return
            self._declare(evidence.coverage)
        for observation in evidence.observations:
            if observation.origin_epoch > epoch:
                problem = "an accounting observation cannot originate in a later epoch"
                raise ReplayError(problem)
        self._alias(evidence.observations)
        for observation in evidence.observations:
            self._observe(observation)

    def _resolve(self, key: Occurrence) -> Occurrence:
        return self._aliases.get(key, key)

    def _observe(self, observation: pb.AccountingObservation) -> None:
        key = self._resolve(_key(observation.dimension, observation.occurrence))
        reading = _Reading(
            observation.revision,
            observation.origin_epoch,
            _amount(observation.charged)[1],
            quantity(observation.charged, observation.refunded),
        )
        series = self._readings.get(key, ())
        index = _check_series(key, series, reading)
        if index is None:
            return
        cell = self._cells.get(key)
        if cell is not None:
            _check_baseline(key, cell, reading)
        self._readings[key] = (*series[:index], reading, *series[index:])

    def _alias(self, observations: Iterable[pb.AccountingObservation]) -> None:
        """Declare a publication's aliases before any of its readings fold.

        An alias names one occurrence and is never itself an occurrence's own key,
        so the outcome does not depend on the order of a publication's observations.
        """
        declared = [
            (self._resolve(_key(o.dimension, o.occurrence)), o.aliases) for o in observations
        ]
        own = {key for key, _ in declared}
        for key, aliases in declared:
            for alias in aliases:
                name: Occurrence = (key[0], key[1], alias)
                if name == key:
                    continue
                if name in self._readings:
                    problem = f"accounting alias {alias} was read as its own occurrence first"
                    raise ReplayError(problem)
                if name in self._cells or name in own:
                    problem = f"accounting alias {alias} is another occurrence's own key"
                    raise ReplayError(problem)
                if self._aliases.setdefault(name, key) != key:
                    problem = f"accounting alias {alias} names two occurrences"
                    raise ReplayError(problem)

    def _declare(self, coverages: Iterable[pb.AccountingCoverage]) -> None:
        """Add one publication's aggregates; nested children are declared with or before it."""
        added: list[pb.AccountingCoverage] = []
        for coverage in coverages:
            value = coverage.to_binary()
            prior = self._coverage.setdefault(coverage.id, value)
            if prior != value:
                problem = f"conflicting accounting coverage {coverage.id}"
                raise ReplayError(problem)
            if prior is value:
                added.append(coverage)
        for coverage in added:
            self._cover(coverage)

    def _cover(self, coverage: pb.AccountingCoverage) -> None:
        aggregate = quantity(coverage.total)
        floor = aggregate.known
        for declared in coverage.cells:
            key = self._resolve(_key(coverage.dimension, declared.occurrence))
            if key in self._cells:
                problem = f"occurrence {key[2]} is covered by two aggregates"
                raise ReplayError(problem)
            cell = _Cell(coverage.id, coverage.watermark, quantity(declared.baseline))
            self._cells[key] = cell
            floor -= cell.baseline.known
            for reading in self._readings.get(key, ()):
                _check_baseline(key, cell, reading)
        for identity in coverage.nested:
            child = self._coverage.get(identity)
            if child is None:
                problem = f"accounting coverage {coverage.id} nests unknown {identity}"
                raise ReplayError(problem)
            nested = pb.AccountingCoverage.from_binary(child)
            if nested.dimension != coverage.dimension:
                problem = f"accounting coverage {coverage.id} nests unknown {identity}"
                raise ReplayError(problem)
            if self._nested.setdefault(identity, coverage.id) != coverage.id:
                problem = f"accounting coverage {identity} is nested twice"
                raise ReplayError(problem)
            floor -= quantity(nested.total).known
        self._check_acyclic(coverage.id)
        if not aggregate.unknown and floor < 0:
            problem = f"accounting coverage {coverage.id} has a negative residual"
            raise ReplayError(problem)

    def _check_acyclic(self, identity: str) -> None:
        seen = {identity}
        parent = self._nested.get(identity)
        while parent is not None:
            if parent in seen:
                problem = f"accounting coverage {identity} nests itself"
                raise ReplayError(problem)
            seen.add(parent)
            parent = self._nested.get(parent)

    def totals(self, *, origin_epoch: int | None = None) -> dict[Dimension, AccountingAmount]:
        """Lifetime totals, or only occurrences that originated in one native epoch.

        Imported coverage and imported occurrences (origin zero) never belong to
        a native epoch, so historical deltas update lifetime usage only.
        """
        coverages = {
            identity: pb.AccountingCoverage.from_binary(value)
            for identity, value in self._coverage.items()
        }
        totals: dict[Dimension, AccountingAmount] = {}
        if origin_epoch is None:
            for identity, coverage in coverages.items():
                if identity not in self._nested:
                    totals[coverage.dimension] = totals.get(
                        coverage.dimension, AccountingAmount()
                    ) + self._covered(coverage, coverages)
        for key, series in self._readings.items():
            if key in self._cells or self._logical_copy(key):
                continue
            reading = series[-1]
            if origin_epoch is None or reading.origin_epoch == origin_epoch:
                totals[key[0]] = totals.get(key[0], AccountingAmount()) + reading.amount
        return totals

    def _logical_copy(self, key: Occurrence) -> bool:
        """A logical parent's observation copies its accepted child; it is decode-only."""
        return key[1] == pb.AccountingOccurrenceKind.INVOCATION and key[2] in self._parents

    def _covered(
        self, coverage: pb.AccountingCoverage, coverages: dict[str, pb.AccountingCoverage]
    ) -> AccountingAmount:
        total = quantity(coverage.total)
        for declared in coverage.cells:
            key = self._resolve(_key(coverage.dimension, declared.occurrence))
            cell = self._cells[key]
            series = self._readings.get(key)
            if series is not None and series[-1].revision > cell.watermark:
                total += _delta(series[-1].amount, cell.baseline)
        for identity in coverage.nested:
            child = coverages[identity]
            total += self._covered(child, coverages) - quantity(child.total)
        return total


def _check_series(key: Occurrence, series: tuple[_Reading, ...], reading: _Reading) -> int | None:
    """Where the reading belongs among an occurrence's readings by revision, None if repeated.

    Whatever the arrival order, a known charge is at least every known charge of a
    lower revision and at most every known charge of a higher one; unknown readings
    between them constrain nothing.
    """
    if series and series[0].origin_epoch != reading.origin_epoch:
        problem = f"occurrence {key[2]} changes its origin epoch"
        raise ReplayError(problem)
    index = bisect_left(series, reading.revision, key=attrgetter("revision"))
    if index < len(series) and series[index].revision == reading.revision:
        if series[index] != reading:
            problem = f"conflicting accounting quantities for {key[2]} at one revision"
            raise ReplayError(problem)
        return None
    if not reading.charged.unknown:
        floor = next(
            (r.charged.known for r in reversed(series[:index]) if not r.charged.unknown), None
        )
        ceiling = next((r.charged.known for r in series[index:] if not r.charged.unknown), None)
        charged = reading.charged.known
        if (floor is not None and charged < floor) or (ceiling is not None and charged > ceiling):
            problem = f"a cumulative accounting charge for {key[2]} cannot decrease"
            raise ReplayError(problem)
    return index


def _check_baseline(key: Occurrence, cell: _Cell, reading: _Reading) -> None:
    """A reading past the watermark has charged at least its covered net baseline."""
    if (
        reading.revision > cell.watermark
        and not reading.charged.unknown
        and not cell.baseline.unknown
        and reading.charged.known < cell.baseline.known
    ):
        problem = f"a cumulative accounting charge for {key[2]} cannot decrease"
        raise ReplayError(problem)


def _delta(latest: AccountingAmount, baseline: AccountingAmount) -> AccountingAmount:
    """A late cumulative reading transfers only its change past the covered baseline."""
    if latest.unknown or baseline.unknown:
        return AccountingAmount(unknown=True)
    return latest - baseline


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
