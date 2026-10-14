"""The catalog of parents keeps chronology and fitness apart, and trusts only what evidence proves.

A snapshot enters the catalog only when the evidence core recorded for it proves its accuracy
passed on exactly its revision. What the planner is offered is derived from that evidence alone:
the latest verified revision of each hypothesis (chronology), and for each comparable context the
best observed partial (fitness). A failed benchmark's partial may be offered as a parent; it is
never a winner (adoption is the search's ranking, not this catalog's).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._values import (
    revision,
    snapshot,
    snapshots,
)

from vibesys.orchestration.dynamic.search.api import (
    ParentConflictError,
    ParentSnapshot,
    ingest,
    options,
    resolve,
)
from vs_core.api import ObservationStatus
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from random import Random

type Catalog = tuple[ParentSnapshot, ...]
VALUES = st.floats(-1000, 1000, allow_nan=False, allow_infinity=False)


def build(*rows: ParentSnapshot) -> Catalog:
    catalog: Catalog = ()
    for row in rows:
        catalog = ingest(catalog, row)
    return catalog


def distinct(rows: list[ParentSnapshot]) -> list[ParentSnapshot]:
    """The rows with one per receipt identity (hypothesis and revision)."""
    return list({(row.hypothesis_id, row.revision): row for row in rows}.values())


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    rows=st.lists(snapshots(), min_size=1, max_size=8).map(distinct),
    order=st.randoms(use_true_random=False),
    replays=st.integers(0, 6),
)
def test_the_catalog_and_its_options_do_not_depend_on_arrival_order_or_replay(
    rows: list[ParentSnapshot], order: Random, replays: int
) -> None:
    shuffled = list(rows)
    order.shuffle(shuffled)
    replayed = [*shuffled, *shuffled[:replays]]
    assert build(*replayed) == build(*rows)
    assert options(build(*replayed)) == options(build(*rows))
    assert len(build(*rows)) == len(rows)


@settings(max_examples=examples(pr=8, nightly=100))
@given(rows=st.lists(snapshots(), min_size=1, max_size=8).map(distinct))
def test_each_hypothesis_has_one_latest_verified_revision_the_highest_submission(
    rows: list[ParentSnapshot],
) -> None:
    offered = options(build(*rows))
    for hypothesis in {row.hypothesis_id for row in rows}:
        mine = [item for item in offered if item.snapshot.hypothesis_id == hypothesis]
        latest = [item for item in mine if item.latest_verified]
        assert len(latest) == 1
        assert latest[0].snapshot.submission_index == max(
            item.snapshot.submission_index for item in mine
        )
        assert resolve(build(*rows), hypothesis) == latest[0].snapshot


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    values=st.lists(VALUES, min_size=1, max_size=10),
    direction=st.sampled_from(["max", "min"]),
)
def test_the_best_observed_partial_is_the_extreme_one_in_the_declared_direction(
    values: list[float], direction: Literal["max", "min"]
) -> None:
    rows = [
        snapshot(f"h{index}", 1, value=value, direction=direction)
        for index, value in enumerate(values)
    ]
    offered = options(build(*rows))
    (best,) = [item for item in offered if item.best_partial]
    assert best.snapshot.benchmark is not None
    assert best.snapshot.benchmark.partial is not None
    assert best.snapshot.benchmark.partial.value == (
        max(values) if direction == "max" else min(values)
    )


def test_a_regression_keeps_the_best_and_the_latest_revision_as_different_parents() -> None:
    first, second = snapshot("source", 1, value=79.835), snapshot("source", 2, value=72.564)
    catalog = build(first, second)
    offered = options(catalog)
    assert next(row.snapshot for row in offered if row.best_partial) == first
    assert next(row.snapshot for row in offered if row.latest_verified) == second
    assert resolve(catalog, "source", first.revision.revision_id.root) == first
    assert resolve(catalog, "source") == second


@settings(max_examples=examples(pr=8, nightly=100))
@given(hypothesis=st.sampled_from("abc"), revision_id=st.text("xyz", min_size=1, max_size=3))
def test_a_selector_the_catalog_does_not_hold_resolves_to_nothing_never_to_the_input(
    hypothesis: str, revision_id: str
) -> None:
    catalog = build(snapshot("a", 1, value=1.0), snapshot("b", 1))
    assert resolve(catalog, hypothesis, revision_id) is None
    assert resolve(catalog, "foreign") is None
    assert resolve((), hypothesis) is None


def _unproven(row: ParentSnapshot, flaw: str) -> ParentSnapshot:
    ledger = {ref.key: ref for ref in row.proof}
    accuracy = ledger[row.accuracy.key]
    match flaw:
        case "no proof":
            return row.model_copy(update={"proof": ()})
        case "other revision":
            bad = accuracy.model_copy(update={"candidate": revision("elsewhere")})
        case "untrusted":
            bad = accuracy.model_copy(update={"provenance": "self-report"})
        case "failed observation":
            bad = accuracy.model_copy(update={"status": ObservationStatus.FAILED})
        case _:
            return row.model_copy(
                update={"accuracy": row.accuracy.model_copy(update={"passed": False})}
            )
    return row.model_copy(update={"proof": (bad, *(r for r in row.proof if r != accuracy))})


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    flaw=st.sampled_from(
        ["no proof", "other revision", "untrusted", "failed observation", "accuracy failed"]
    ),
    row=snapshots(),
)
def test_a_snapshot_the_recorded_evidence_does_not_prove_gives_no_authority(
    flaw: str, row: ParentSnapshot
) -> None:
    unproven = _unproven(row, flaw)
    assert ingest((), unproven) == ()
    assert options((unproven,)) == ()
    assert resolve((unproven,), row.hypothesis_id, row.revision.revision_id.root) is None


@settings(max_examples=examples(pr=8, nightly=100))
@given(row=snapshots())
def test_a_snapshot_of_unknown_chronology_is_buildable_but_never_the_latest(
    row: ParentSnapshot,
) -> None:
    unknown = row.model_copy(update={"submission_index": 0})
    catalog = build(unknown)
    assert resolve(catalog, row.hypothesis_id) is None
    assert resolve(catalog, row.hypothesis_id, row.revision.revision_id.root) == unknown
    assert not any(item.latest_verified for item in options(catalog))


def _with_partial_context(row: ParentSnapshot, changed: str) -> ParentSnapshot:
    benchmark = row.benchmark
    assert benchmark is not None
    assert benchmark.partial is not None
    if changed in {"evaluator", "workload", "environment"}:
        digest = {f"{changed}_digest": f"different-{changed}"}
        return row.model_copy(
            update={"proof": tuple(r.model_copy(update=digest) for r in row.proof)}
        )
    if changed == "stage":
        return row.model_copy(
            update={"benchmark": benchmark.model_copy(update={"stage": "warmup"})}
        )
    if changed == "protocol":
        return row.model_copy(update={"benchmark": benchmark.model_copy(update={"protocol": 2})})
    changes = {
        "unit": "requests/s",
        "direction": "min",
        "name": "latency",
        "target": 100.0,
        "progress_unit": "requests",
        "required": 100,
    }
    partial = benchmark.partial.model_copy(update={changed: changes[changed]})
    return row.model_copy(update={"benchmark": benchmark.model_copy(update={"partial": partial})})


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    changed=st.sampled_from(
        [
            "unit",
            "direction",
            "name",
            "target",
            "progress_unit",
            "required",
            "stage",
            "protocol",
            "evaluator",
            "workload",
            "environment",
        ]
    ),
    values=st.tuples(VALUES, VALUES),
)
def test_partials_measured_in_a_different_context_are_never_ranked_against_each_other(
    changed: str, values: tuple[float, float]
) -> None:
    first = snapshot("a", 1, value=values[0])
    second = _with_partial_context(snapshot("b", 2, value=values[1]), changed)
    rows = options(build(first, second))
    assert len(rows) == 2
    assert all(row.best_partial for row in rows)
    assert rows[0].comparison_key != rows[1].comparison_key


def test_a_partial_without_a_unit_is_buildable_but_has_no_comparison_authority() -> None:
    row = snapshot("a", 1, value=79.835)
    assert row.benchmark is not None
    assert row.benchmark.partial is not None
    unitless = row.model_copy(
        update={
            "benchmark": row.benchmark.model_copy(
                update={"partial": row.benchmark.partial.model_copy(update={"unit": None})}
            )
        }
    )
    (offered,) = options(build(unitless))
    assert offered.comparison_key is None
    assert not offered.best_partial
    assert resolve(build(unitless), "a", row.revision.revision_id.root) == unitless


@settings(max_examples=examples(pr=8, nightly=100))
@given(row=snapshots())
def test_a_later_benchmark_extends_the_accuracy_receipt_and_replaying_the_bare_one_never_downgrades(
    row: ParentSnapshot,
) -> None:
    bare = row.model_copy(update={"benchmark": None})
    catalog = build(bare, row)
    assert catalog == (row,)
    assert ingest(catalog, bare) == catalog


@settings(max_examples=examples(pr=8, nightly=100))
@given(row=snapshots(), other=VALUES)
def test_a_second_benchmark_for_one_receipt_is_a_conflict_not_an_overwrite(
    row: ParentSnapshot, other: float
) -> None:
    changed = snapshot(row.hypothesis_id, row.submission_index, value=other, digest="x")
    assume(row.benchmark is not None and changed != row)
    assert changed.revision == row.revision
    assert changed.accuracy == row.accuracy
    with pytest.raises(ParentConflictError):
        ingest(build(row), changed)


def test_equal_partials_prefer_further_progress_then_the_stable_receipt() -> None:
    near, far = snapshot("a", 1, value=10.0), snapshot("b", 2, value=10.0)
    for order in ((near, far), (far, near)):
        best = next(row.snapshot for row in options(build(*order)) if row.best_partial)
        assert best == near  # ordinal 1 completed 71 of 72 rounds, ordinal 2 only 65


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    row=snapshots(),
    changed=st.sampled_from(["evaluator_digest", "workload_digest", "environment_digest"]),
)
def test_a_benchmark_measured_in_another_context_than_the_accuracy_is_not_buildable(
    row: ParentSnapshot, changed: str
) -> None:
    assume(row.benchmark is not None)
    assert row.benchmark is not None
    key = row.benchmark.key
    other = tuple(
        ref.model_copy(update={changed: "elsewhere"}) if ref.key == key else ref
        for ref in row.proof
    )
    assert ingest((), row.model_copy(update={"proof": other})) == ()
