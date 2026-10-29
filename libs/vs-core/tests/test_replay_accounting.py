"""Native and imported accounting reconcile through one coverage-aware fold."""

from dataclasses import dataclass, field
from fractions import Fraction
from random import Random

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from protobuf import Oneof
from protobuf.wkt.google.protobuf.any_pb import Any
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import (
    AccountingAmount,
    AccountingLedger,
    ReplayError,
    ReplayResourceAccounting,
)
from vs_proto.api import Notice, NoticeCode, Record, Severity, ValidationError, replay, validate

from .test_replay_archive import decode, records

Dimension = replay.AccountingDimension
_INVOCATION = replay.AccountingOccurrenceKind.INVOCATION
_CHARGE = replay.AccountingOccurrenceKind.CHARGE


def count(value: int | None) -> replay.AccountingQuantity:
    return replay.AccountingQuantity(
        value=Oneof("unknown", value=True) if value is None else Oneof("count", value)
    )


def reading(
    occurrence: str,
    value: int | None,
    *,
    revision: int = 0,
    aliases: tuple[str, ...] = (),
    origin: int = 0,
) -> replay.AccountingObservation:
    return replay.AccountingObservation(
        occurrence=replay.AccountingOccurrence(kind=_INVOCATION, id=occurrence),
        dimension=Dimension.INPUT_TOKENS,
        revision=revision,
        charged=count(value),
        aliases=list(aliases),
        origin_epoch=origin,
    )


def charge(
    occurrence: str, value: int, dimension: Dimension, *, refunded: int = 0, alias: str = ""
) -> replay.AccountingObservation:
    return replay.AccountingObservation(
        occurrence=replay.AccountingOccurrence(kind=_CHARGE, id=occurrence),
        dimension=dimension,
        charged=count(value),
        refunded=count(refunded) if refunded else None,
        aliases=[alias] if alias else [],
    )


def coverage(
    identity: str, total: int, cells: dict[str, int | None], *, nested: tuple[str, ...] = ()
) -> replay.AccountingCoverage:
    return replay.AccountingCoverage(
        id=identity,
        dimension=Dimension.INPUT_TOKENS,
        total=count(total),
        cells=[
            replay.AccountingCell(
                occurrence=replay.AccountingOccurrence(kind=_INVOCATION, id=occurrence),
                baseline=count(baseline),
            )
            for occurrence, baseline in cells.items()
        ],
        nested=list(nested),
    )


def notice(evidence: replay.AccountingEvidence, *, seq: int = 1, epoch: int = 1) -> Record:
    return Record(
        seq=seq,
        epoch=epoch,
        at=Timestamp(seconds=1),
        clock=0,
        body=Oneof(
            "notice",
            Notice(
                severity=Severity.WARNING,
                code=NoticeCode.FRAMEWORK_WARNING,
                message="Accounting evidence.",
                detail=Any(type_url=evidence.desc().type_name, value=evidence.to_binary()),
            ),
        ),
    )


def published(
    *parts: replay.AccountingCoverage | replay.AccountingObservation, seq: int = 1
) -> Record:
    return notice(
        replay.AccountingEvidence(
            import_id="import-a",
            coverage=[part for part in parts if isinstance(part, replay.AccountingCoverage)],
            observations=[part for part in parts if isinstance(part, replay.AccountingObservation)],
        ),
        seq=seq,
    )


def delayed(*observations: replay.AccountingObservation, seq: int = 1) -> Record:
    return notice(replay.AccountingEvidence(observations=list(observations)), seq=seq)


def lifetime(*history: Record) -> dict[Dimension, AccountingAmount]:
    return AccountingLedger.from_records(history).totals()


def test_a_late_reading_of_a_covered_invocation_adds_only_its_growth() -> None:
    """Regression: aggregate 100 including invocation 30, later read at 40, is 110, never 140."""
    history = [
        *records(),
        published(
            coverage("agent:input_tokens", 100, {"invocation-a": 30}),
            charge("charge-a", 2, Dimension.TURNS, alias="req-a"),
            charge("charge-b", 3, Dimension.ATTEMPTS, refunded=1),
        ),
        delayed(reading("invocation-a", 40, revision=7)),
    ]
    for seq, record in enumerate(history, start=1):
        record.seq = seq
    decode(history)
    accounting = ReplayResourceAccounting.from_records(tuple(history), epoch=1)
    assert accounting.reported.input_tokens == 110
    assert accounting.reported.money_usd == 0.5
    assert accounting.epoch_reported.input_tokens == 0
    assert accounting.lifetime[Dimension.TURNS] == AccountingAmount(Fraction(2))
    assert accounting.lifetime[Dimension.ATTEMPTS] == AccountingAmount(Fraction(2))
    # The native result is paid with unreported tokens: known zero, never a guessed total.
    assert accounting.lifetime[Dimension.OUTPUT_TOKENS] == AccountingAmount(unknown=True)
    assert accounting.lifetime[Dimension.INPUT_TOKENS] == AccountingAmount(
        Fraction(110), unknown=True
    )


def test_an_unknown_baseline_makes_only_the_late_adjustment_unknown() -> None:
    publication = published(coverage("agent:input_tokens", 100, {"invocation-a": None}))
    assert lifetime(publication)[Dimension.INPUT_TOKENS] == AccountingAmount(Fraction(100))
    late = delayed(reading("invocation-a", 40, revision=3))
    assert lifetime(publication, late)[Dimension.INPUT_TOKENS] == AccountingAmount(
        Fraction(100), unknown=True
    )


def test_nested_aggregates_partition_like_cells() -> None:
    history = (
        published(
            coverage("run", 100, {"invocation-a": 10}, nested=("round",)),
            coverage("round", 50, {"invocation-b": 20}),
        ),
        delayed(reading("invocation-a", 15, revision=2), reading("invocation-b", 26, revision=2)),
    )
    assert lifetime(*history)[Dimension.INPUT_TOKENS] == AccountingAmount(Fraction(111))


@pytest.mark.parametrize(
    "history",
    [
        pytest.param(
            (delayed(reading("a", 1, revision=2)), delayed(reading("a", 2, revision=2))),
            id="conflicting quantities at one revision",
        ),
        pytest.param(
            (published(coverage("x", 5, {})), published(coverage("x", 6, {}))),
            id="republished import with different accounting",
        ),
        pytest.param(
            (published(coverage("x", 10, {"a": 1}), coverage("y", 10, {"a": 1})),),
            id="one cell covered twice",
        ),
        pytest.param((published(coverage("x", 10, {"a": 11})),), id="negative residual"),
        pytest.param(
            (published(coverage("x", 10, {}, nested=("x",))),), id="aggregate nests itself"
        ),
        pytest.param((published(coverage("x", 10, {}, nested=("missing",))),), id="unknown nested"),
        pytest.param(
            (
                published(coverage("x", 10, {"a": 5})),
                delayed(reading("a", 4, revision=1)),
            ),
            id="cumulative reading decreases",
        ),
        pytest.param(
            (
                delayed(reading("u", 50)),
                delayed(reading("u", None, revision=1), seq=2),
                delayed(reading("u", 10, revision=2), seq=3),
            ),
            id="charge falls across an unknown reading",
        ),
        pytest.param(
            (
                delayed(reading("u", 50, revision=5)),
                delayed(reading("u", 40, revision=3), seq=2),
                delayed(reading("u", 45, revision=1), seq=3),
            ),
            id="charge rises with a lower revision arriving later",
        ),
        pytest.param(
            (
                delayed(reading("a", 4, revision=1)),
                delayed(reading("a", 20, revision=2), seq=2),
                published(coverage("x", 10, {"a": 5}), seq=3),
            ),
            id="a covered reading below its baseline before the coverage arrived",
        ),
        pytest.param(
            (delayed(reading("a", 1, aliases=("r",)), reading("b", 1, aliases=("r",))),),
            id="alias names two occurrences",
        ),
        pytest.param(
            (
                delayed(reading("a", 1)),
                delayed(reading("a", 1, revision=1, origin=1), seq=2),
            ),
            id="occurrence changes its origin epoch",
        ),
        pytest.param(
            (delayed(charge("a", 1, Dimension.ATTEMPTS, refunded=2)),), id="refund exceeds charge"
        ),
    ],
)
def test_contradictory_evidence_fails_validation(history: tuple[Record, ...]) -> None:
    with pytest.raises(ReplayError):
        lifetime(*history)


def test_the_archive_rejects_contradictory_accounting_before_reconstruction() -> None:
    history = [
        *records(),
        delayed(reading("a", 1, revision=2), seq=5),
        delayed(reading("a", 2, revision=2), seq=6),
    ]
    with pytest.raises(ReplayError, match="conflicting accounting"):
        decode(history)


def test_a_late_reading_through_an_alias_charges_its_occurrence_once() -> None:
    """Regression: charge c1 with alias req-7, then a late reading of req-7, is one admission."""
    late = replay.AccountingObservation(
        occurrence=replay.AccountingOccurrence(kind=_CHARGE, id="req-7"),
        dimension=Dimension.ADMISSIONS,
        revision=3,
        charged=count(1),
    )
    history = (published(charge("c1", 1, Dimension.ADMISSIONS, alias="req-7")), delayed(late))
    assert lifetime(*history)[Dimension.ADMISSIONS] == AccountingAmount(Fraction(1))


@pytest.mark.parametrize(
    "reverse", [pytest.param(False, id="alias first"), pytest.param(True, id="alias last")]
)
def test_an_alias_that_is_another_occurrences_key_fails_in_either_order(*, reverse: bool) -> None:
    """Regression: c1 with alias c2, beside c2, is ambiguous whichever the publication lists first."""
    parts = [
        charge("c1", 1, Dimension.ADMISSIONS, alias="c2"),
        charge("c2", 1, Dimension.ADMISSIONS),
    ]
    with pytest.raises(ReplayError, match="another occurrence's own key"):
        lifetime(published(*(parts[::-1] if reverse else parts)))


def test_a_reading_before_its_alias_was_declared_fails() -> None:
    early = replay.AccountingObservation(
        occurrence=replay.AccountingOccurrence(kind=_CHARGE, id="req-7"),
        dimension=Dimension.ADMISSIONS,
        revision=3,
        charged=count(1),
    )
    declaration = published(charge("c1", 1, Dimension.ADMISSIONS, alias="req-7"))
    with pytest.raises(ReplayError, match="read as its own occurrence first"):
        lifetime(delayed(early), declaration)


def test_an_occurrence_may_list_its_own_key_as_an_alias() -> None:
    history = delayed(reading("a", 7, aliases=("a",)))
    assert lifetime(history)[Dimension.INPUT_TOKENS] == AccountingAmount(Fraction(7))


def test_a_lower_revision_arriving_later_may_hold_a_lower_charge() -> None:
    history = (delayed(reading("u", 50, revision=5)), delayed(reading("u", 40, revision=3), seq=2))
    assert lifetime(*history)[Dimension.INPUT_TOKENS] == AccountingAmount(Fraction(50))


def test_an_uncovered_charge_cannot_fall_but_a_refund_lowers_its_net() -> None:
    """Regression: uncovered u at 50, then 10 at revision 1, fails instead of reporting 10."""
    first = delayed(reading("u", 50))
    with pytest.raises(ReplayError, match="cannot decrease"):
        lifetime(first, delayed(reading("u", 10, revision=1), seq=2))
    refunded = reading("u", 50, revision=1)
    refunded.refunded = count(40)
    assert lifetime(first, delayed(refunded, seq=2))[Dimension.INPUT_TOKENS] == AccountingAmount(
        Fraction(10)
    )


def test_a_money_refund_may_be_fractional() -> None:
    def money(value: float) -> replay.AccountingQuantity:
        return replay.AccountingQuantity(value=Oneof("amount", value))

    paid = replay.AccountingObservation(
        occurrence=replay.AccountingOccurrence(kind=_INVOCATION, id="a"),
        dimension=Dimension.MONEY_USD,
        charged=money(1.5),
        refunded=money(0.25),
    )
    assert lifetime(delayed(paid))[Dimension.MONEY_USD] == AccountingAmount(Fraction(5, 4))
    paid.refunded = count(1)
    with pytest.raises(ReplayError, match="unit"):
        lifetime(delayed(paid))


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param(
            replay.AccountingEvidence(
                import_id="import-a",
                observations=[
                    replay.AccountingObservation(
                        occurrence=replay.AccountingOccurrence(kind=_CHARGE, id="c1"),
                        dimension=Dimension.ADMISSIONS,
                        charged=count(1),
                        origin_epoch=1,
                    )
                ],
            ),
            id="import claims a native epoch",
        ),
        pytest.param(replay.AccountingEvidence(), id="no observation or coverage"),
        pytest.param(replay.AccountingEvidence(import_id="import-a"), id="empty import"),
    ],
)
def test_evidence_that_changes_nothing_or_claims_an_epoch_is_invalid(
    evidence: replay.AccountingEvidence,
) -> None:
    with pytest.raises(ValidationError):
        validate(evidence)
    history = [*records(), notice(evidence, seq=5)]
    with pytest.raises(ValidationError):
        decode(history)


_values = st.integers(0, 50)
_steps = st.lists(st.integers(0, 20), max_size=3)


def _revised(
    observation: replay.AccountingObservation, revision: int
) -> replay.AccountingObservation:
    observation.revision = revision
    return observation


@dataclass
class _History:
    """One import, its later evidence, and the totals a correct fold reports."""

    imported: list[replay.AccountingObservation] = field(default_factory=list)
    coverage: list[replay.AccountingCoverage] = field(default_factory=list)
    late: list[replay.AccountingObservation] = field(default_factory=list)
    aliased: list[replay.AccountingObservation] = field(default_factory=list)
    tokens: AccountingAmount = field(default_factory=AccountingAmount)
    attempts: AccountingAmount = field(default_factory=AccountingAmount)
    epoch: AccountingAmount = field(default_factory=AccountingAmount)

    def chain(self, identity: str, first: int, steps: list[int], *, origin: int = 0) -> int:
        """Append cumulative readings at revisions from one; return the latest value."""
        value = first
        for revision, step in enumerate(steps, start=1):
            value += step
            self.late.append(reading(identity, value, revision=revision, origin=origin))
        return value


def _aggregates(draw: st.DrawFn, history: _History) -> None:
    """A run aggregate nesting a round aggregate, each with cells read again later."""
    nested_total = 0
    for name, nested in (("round", ()), ("run", ("round",))):
        cells = draw(st.lists(st.tuples(st.none() | _values, _steps), max_size=3))
        baselines = {f"{name}-cell-{index}": base for index, (base, _) in enumerate(cells)}
        total = draw(_values) + sum(base or 0 for base in baselines.values()) + nested_total
        history.coverage.append(coverage(name, total, baselines, nested=nested))
        for identity, (base, steps) in zip(baselines, cells, strict=True):
            value = history.chain(identity, base or 0, steps)
            if steps:
                history.tokens += (
                    AccountingAmount(unknown=True)
                    if base is None
                    else AccountingAmount(Fraction(value - base))
                )
        nested_total = total
    history.tokens += AccountingAmount(Fraction(nested_total))


def _uncovered(draw: st.DrawFn, history: _History) -> None:
    """Imported invocations outside any aggregate, read again later, possibly unknown."""
    drawn = draw(st.lists(st.tuples(_values, _steps, st.booleans()), max_size=3))
    for index, (first, steps, unknown) in enumerate(drawn):
        identity = f"uncovered-{index}"
        history.imported.append(reading(identity, first))
        value = history.chain(identity, first, steps)
        if unknown:
            history.late.append(reading(identity, None, revision=len(steps) + 1))
        history.tokens += AccountingAmount(Fraction(0 if unknown else value), unknown=unknown)


def _charges(draw: st.DrawFn, history: _History) -> None:
    """Imported charges with refunds, read again through their request alias."""
    refunds = st.lists(st.integers(0, 9), min_size=1, max_size=3)
    for index, (charged, drawn) in enumerate(
        draw(st.lists(st.tuples(st.integers(1, 9), refunds), max_size=3))
    ):
        refunded = [min(refund, charged) for refund in drawn]
        history.imported.append(
            charge(
                f"charge-{index}",
                charged,
                Dimension.ATTEMPTS,
                refunded=refunded[0],
                alias=f"request-{index}",
            )
        )
        history.aliased += [
            _revised(
                charge(f"request-{index}", charged, Dimension.ATTEMPTS, refunded=refund), revision
            )
            for revision, refund in enumerate(refunded[1:], start=1)
        ]
        history.attempts += AccountingAmount(Fraction(charged - refunded[-1]))


@st.composite
def _histories(draw: st.DrawFn) -> _History:
    history = _History()
    _aggregates(draw, history)
    _uncovered(draw, history)
    _charges(draw, history)
    for index, (first, steps) in enumerate(draw(st.lists(st.tuples(_values, _steps), max_size=2))):
        value = history.chain(f"native-{index}", 0, [first, *steps], origin=1)
        history.tokens += AccountingAmount(Fraction(value))
        history.epoch += AccountingAmount(Fraction(value))
    return history


def _insert(order: Random, history: list[Record], records: list[Record], *, after: int) -> None:
    for record in records:
        history.insert(order.randint(after + 1, len(history)), record)


@given(
    history=_histories(),
    order=st.randoms(use_true_random=False),
    repeats=st.integers(1, 2),
    falling=st.none() | st.tuples(st.integers(1, 50), st.integers(1, 50)),
)
def test_lifetime_and_epoch_usage_are_conserved_for_every_evidence_shape(
    history: _History, order: Random, repeats: int, falling: tuple[int, int] | None
) -> None:
    """Refunds, unknowns, nested aggregates, aliases, late and native readings fold once.

    Readings through an alias follow the import that declares it; all other evidence
    arrives in any order, repeated. A falling cumulative charge always fails.
    """
    publication = published(*history.coverage, *history.imported)
    records = [publication] * repeats + [delayed(item) for item in history.late] * repeats
    order.shuffle(records)
    declared = records.index(publication)
    _insert(order, records, [delayed(item) for item in history.aliased] * repeats, after=declared)
    if falling is not None:
        peak, drop = falling
        assume(drop <= peak)
        steps = [
            delayed(reading("falling", peak)),
            delayed(reading("falling", peak - drop, revision=1)),
        ]
        _insert(order, records, steps, after=-1)
        with pytest.raises(ReplayError, match="cannot decrease"):
            lifetime(*records)
        return
    ledger = AccountingLedger.from_records(records)
    totals = ledger.totals()
    assert totals.get(Dimension.INPUT_TOKENS, AccountingAmount()) == history.tokens
    assert totals.get(Dimension.ATTEMPTS, AccountingAmount()) == history.attempts
    epoch = ledger.totals(origin_epoch=1)
    assert epoch.get(Dimension.INPUT_TOKENS, AccountingAmount()) == history.epoch
    assert Dimension.ATTEMPTS not in epoch


@given(history=_histories(), order=st.randoms(use_true_random=False))
def test_a_reading_through_an_alias_before_its_declaration_fails_closed(
    history: _History, order: Random
) -> None:
    assume(history.aliased)
    publication = published(*history.coverage, *history.imported)
    records = [delayed(item) for item in history.late]
    order.shuffle(records)
    records.insert(order.randint(0, len(records)), publication)
    for item in history.aliased:
        records.insert(order.randint(0, records.index(publication)), delayed(item))
    with pytest.raises(ReplayError, match="read as its own occurrence first"):
        lifetime(*records)
