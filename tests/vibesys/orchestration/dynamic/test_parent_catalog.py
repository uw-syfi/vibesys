"""Immutable receipt catalogs preserve chronology independently of fitness."""

from __future__ import annotations

from functools import cache

from hypothesis import given
from hypothesis import strategies as st
from tests.support.evaluation_scenarios import ScenarioOutcome, ScenarioSpec, capture_projection

from vibesys.orchestration.dynamic.parents.api import (
    ParentCatalog,
    ParentSnapshot,
    ingest,
    options,
    resolve,
)
from vs_evaluation.api import EvidenceKind
from vs_evaluator_protocol.api import PartialMeasurement, Progress


@cache
def _snapshot(value: float, ordinal: int) -> ParentSnapshot:
    evaluation = capture_projection(
        ScenarioSpec(
            outcome=ScenarioOutcome.CORRECTNESS_FAIL,
            revision=f"revision-{ordinal}",
            patch=f"patch-{ordinal}",
            benchmark_failure=True,
            partial=PartialMeasurement(
                name="throughput",
                value=value,
                direction="max",
                unit="tokens/s",
                progress=Progress(completed=71 if ordinal == 1 else 65, required=72, unit="rounds"),
            ),
        )
    )
    accuracy = next(
        item for item in evaluation.trusted_evidence if item.kind is EvidenceKind.ACCURACY
    )
    benchmark = next(
        item for item in evaluation.trusted_evidence if item.kind is EvidenceKind.BENCHMARK
    )
    assert evaluation.handle_id is not None
    assert evaluation.content_digest is not None
    return ParentSnapshot(
        hypothesis_id="source",
        revision=evaluation.revision,
        content_digest=evaluation.content_digest,
        handle_id=evaluation.handle_id,
        submission_index=ordinal,
        accuracy=accuracy,
        benchmark=benchmark,
        retained=True,
    )


def test_observed_regression_keeps_best_and_latest_exact_revisions() -> None:
    first = _snapshot(79.835, 1)
    second = _snapshot(72.564, 2)
    catalog = ingest(ingest(ParentCatalog(), first), second)
    offered = options(catalog)
    assert len(offered) == 2
    assert next(row.snapshot for row in offered if row.best_partial) == first
    assert next(row.snapshot for row in offered if row.latest_verified) == second
    assert resolve(catalog, "source", first.revision) == first
    assert resolve(catalog, "source") == second
    assert resolve(catalog, "foreign", first.revision) is None
    assert resolve(catalog, "source", "missing") is None
    assert ParentCatalog.model_validate_json(catalog.model_dump_json()) == catalog


@given(st.permutations((0, 1, 2, 3)), st.lists(st.integers(min_value=0, max_value=3), max_size=10))
def test_order_and_replay_preserve_catalog_and_indexes(order: list[int], replay: list[int]) -> None:
    rows = tuple(
        _snapshot(value, ordinal) for ordinal, value in enumerate((79.835, 72.564, 83.0, 77.0), 1)
    )
    expected = ParentCatalog()
    for row in rows:
        expected = ingest(expected, row)
    actual = ParentCatalog()
    for index in (*order, *replay):
        actual = ingest(actual, rows[index])
    assert actual == expected
    assert options(actual) == options(expected)


def test_ineligible_identity_and_unknown_chronology_supply_no_authority() -> None:
    row = _snapshot(79.835, 1)
    assert ingest(ParentCatalog(), row.model_copy(update={"retained": False})).snapshots == ()
    assert (
        ingest(ParentCatalog(), row.model_copy(update={"content_digest": "f" * 64})).snapshots == ()
    )
    unknown = ingest(ParentCatalog(), row.model_copy(update={"submission_index": 0}))
    assert resolve(unknown, "source") is None
    assert resolve(unknown, "source", row.revision) is not None


@given(
    st.sampled_from(
        (
            "unit",
            "direction",
            "quantity",
            "stage",
            "evaluator",
            "workload",
            "environment",
            "protocol",
            "target",
        )
    )
)
def test_partial_comparison_key_excludes_only_candidate_identity(field: str) -> None:
    rows = options(ingest(ingest(ParentCatalog(), _snapshot(79.835, 1)), _snapshot(72.564, 2)))
    key = rows[0].comparison_key
    assert key is not None
    assert key == rows[1].comparison_key
    original = getattr(key, field)
    if field == "direction":
        changed: str | float | int = "min"
    elif field == "protocol":
        changed = 2
    elif field == "target":
        changed = 100.0
    else:
        changed = f"changed-{original}"
    assert key.model_copy(update={field: changed}) != key


def test_benchmark_settlement_extends_without_accuracy_replay_downgrade() -> None:
    settled = _snapshot(79.835, 1)
    accuracy_only = settled.model_copy(update={"benchmark": None})
    catalog = ingest(ingest(ParentCatalog(), accuracy_only), settled)
    assert ingest(catalog, accuracy_only) == catalog
    assert options(catalog)[0].best_partial
