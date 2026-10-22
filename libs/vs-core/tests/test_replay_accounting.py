"""Native and imported accounting reconcile through one coverage-aware fold."""

from fractions import Fraction
from random import Random

import pytest
from hypothesis import given
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
from vs_proto.api import Notice, NoticeCode, Record, Severity, replay

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
) -> replay.AccountingObservation:
    return replay.AccountingObservation(
        occurrence=replay.AccountingOccurrence(kind=_INVOCATION, id=occurrence),
        dimension=Dimension.INPUT_TOKENS,
        revision=revision,
        charged=count(value),
        aliases=list(aliases),
    )


def charge(
    occurrence: str, value: int, dimension: Dimension, *, refunded: int = 0, alias: str = ""
) -> replay.AccountingObservation:
    return replay.AccountingObservation(
        occurrence=replay.AccountingOccurrence(kind=_CHARGE, id=occurrence),
        dimension=dimension,
        charged=count(value),
        refunded=refunded,
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


def published(*parts: replay.AccountingCoverage | replay.AccountingObservation) -> Record:
    return notice(
        replay.AccountingEvidence(
            import_id="import-a",
            coverage=[part for part in parts if isinstance(part, replay.AccountingCoverage)],
            observations=[part for part in parts if isinstance(part, replay.AccountingObservation)],
        )
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
            (delayed(reading("a", 1, aliases=("r",)), reading("b", 1, aliases=("r",))),),
            id="alias names two occurrences",
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


_cells = st.lists(
    st.tuples(
        st.integers(0, 50),
        st.lists(st.integers(0, 50), max_size=3),
    ),
    max_size=4,
)


@given(
    cells=_cells,
    residual=st.integers(0, 100),
    uncovered=st.lists(st.integers(0, 50), max_size=3),
    order=st.randoms(use_true_random=False),
    repeats=st.integers(1, 3),
)
def test_lifetime_usage_is_conserved_across_order_duplicates_and_reimport(
    cells: list[tuple[int, list[int]]],
    residual: int,
    uncovered: list[int],
    order: Random,
    repeats: int,
) -> None:
    baselines = {f"covered-{index}": baseline for index, (baseline, _) in enumerate(cells)}
    aggregate = residual + sum(baselines.values())
    publication = published(
        coverage("agent", aggregate, dict(baselines)),
        *(reading(f"uncovered-{index}", value) for index, value in enumerate(uncovered)),
    )
    expected = aggregate + sum(uncovered)
    late: list[Record] = []
    for index, (baseline, growth) in enumerate(cells):
        value = baseline
        for revision, step in enumerate(growth, start=1):
            value += step
            late.append(delayed(reading(f"covered-{index}", value, revision=revision)))
        expected += value - baseline
    history = [publication] * repeats + late * repeats
    order.shuffle(history)
    assert lifetime(*history).get(Dimension.INPUT_TOKENS, AccountingAmount()) == (
        AccountingAmount(Fraction(expected))
    )
