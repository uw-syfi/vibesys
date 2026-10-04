"""Immutable receipt catalogs preserve chronology independently of fitness."""

from __future__ import annotations

from functools import cache

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.evaluation_scenarios import ScenarioOutcome, ScenarioSpec, capture_projection

from vibesys.orchestration.dynamic.parents.api import (
    ParentCatalog,
    ParentSnapshot,
    ingest,
    options,
    resolve,
)
from vs_evaluation.api import ContentDigest, EvidenceKind, EvidenceOutcome
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
            "name",
            "target",
            "stage",
            "evaluator",
            "workload",
            "environment",
            "progress",
        )
    )
)
def test_changed_partial_context_is_never_ranked_with_original(field: str) -> None:
    first = _snapshot(79.835, 1)
    second = _snapshot(72.564, 2)
    benchmark = second.benchmark
    assert benchmark is not None
    partial = benchmark.partial_measurement
    assert partial is not None
    if field in {"evaluator", "workload", "environment"}:
        fingerprints = benchmark.fingerprints.model_copy(
            update={field: ContentDigest.sha256(f"different-{field}".encode())}
        )
        second = second.model_copy(
            update={
                "accuracy": second.accuracy.model_copy(update={"fingerprints": fingerprints}),
                "benchmark": benchmark.model_copy(update={"fingerprints": fingerprints}),
            }
        )
    elif field == "stage":
        second = second.model_copy(
            update={"benchmark": benchmark.model_copy(update={"stage_name": "warmup"})}
        )
    else:
        changes = {
            "unit": "requests/s",
            "direction": "min",
            "name": "latency",
            "target": 100.0,
            "progress": Progress(completed=65, required=100, unit="requests"),
        }
        second = second.model_copy(
            update={
                "benchmark": benchmark.model_copy(
                    update={
                        "partial_measurement": partial.model_copy(update={field: changes[field]})
                    }
                )
            }
        )
    rows = options(ingest(ingest(ParentCatalog(), first), second))
    assert len(rows) == 2
    assert all(row.best_partial for row in rows)
    assert rows[0].comparison_key != rows[1].comparison_key


def test_unknown_unit_is_buildable_without_comparison_authority() -> None:
    snapshot = _snapshot(79.835, 1)
    benchmark = snapshot.benchmark
    assert benchmark is not None
    partial = benchmark.partial_measurement
    assert partial is not None
    unknown = snapshot.model_copy(
        update={
            "benchmark": benchmark.model_copy(
                update={"partial_measurement": partial.model_copy(update={"unit": None})}
            )
        }
    )
    row = options(ingest(ParentCatalog(), unknown))[0]
    assert row.comparison_key is None
    assert not row.best_partial
    assert resolve(ingest(ParentCatalog(), unknown), "source", snapshot.revision) == unknown


@given(
    st.lists(
        st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=12,
    ),
    st.sampled_from(("max", "min")),
)
def test_best_observed_partial_is_monotone_in_declared_direction(
    values: list[float], direction: str
) -> None:
    first = _snapshot(79.835, 1)
    second = _snapshot(72.564, 2)
    catalog = ParentCatalog()
    observed = []
    for index, value in enumerate(values):
        template = first if index % 2 == 0 else second
        benchmark = template.benchmark
        assert benchmark is not None
        partial = benchmark.partial_measurement
        assert partial is not None
        handle = f"evaluation-{index}"
        snapshot = template.model_copy(
            update={
                "handle_id": handle,
                "revision": f"revision-{index}",
                "submission_index": index + 1,
                "accuracy": template.accuracy.model_copy(update={"evaluation_id": handle}),
                "benchmark": benchmark.model_copy(
                    update={
                        "evaluation_id": handle,
                        "partial_measurement": partial.model_copy(
                            update={"value": value, "direction": direction}
                        ),
                    }
                ),
            }
        )
        catalog = ingest(catalog, snapshot)
        observed.append(value)
        best = next(row.snapshot for row in options(catalog) if row.best_partial)
        assert best.benchmark is not None
        assert best.benchmark.partial_measurement is not None
        assert best.benchmark.partial_measurement.value == (
            max(observed) if direction == "max" else min(observed)
        )


def test_failed_accuracy_never_builds_even_with_better_partial() -> None:
    snapshot = _snapshot(79.835, 1)
    failed = snapshot.model_copy(
        update={
            "accuracy": snapshot.accuracy.model_copy(update={"outcome": EvidenceOutcome.FAILED})
        }
    )
    assert options(ingest(ParentCatalog(), failed)) == ()


def test_benchmark_settlement_extends_without_accuracy_replay_downgrade() -> None:
    settled = _snapshot(79.835, 1)
    accuracy_only = settled.model_copy(update={"benchmark": None})
    catalog = ingest(ingest(ParentCatalog(), accuracy_only), settled)
    assert ingest(catalog, accuracy_only) == catalog
    assert options(catalog)[0].best_partial


def test_parent_records_reject_unknown_feature_keys_and_mutation() -> None:
    snapshot = _snapshot(79.835, 1)
    with pytest.raises(ValidationError, match="unknown_features"):
        ParentSnapshot.model_validate({**snapshot.model_dump(), "unknown_features": ["batching"]})
    for field_name in ParentSnapshot.model_fields:
        with pytest.raises(ValidationError, match="frozen"):
            setattr(snapshot, field_name, getattr(snapshot, field_name))
    catalog = ingest(ParentCatalog(), snapshot)
    with pytest.raises(ValidationError, match="unexpected"):
        ParentCatalog.model_validate({**catalog.model_dump(), "unexpected": True})


def test_equal_observed_partials_choose_comparable_progress_then_stable_receipt() -> None:
    first = _snapshot(10.0, 1)
    second = _snapshot(10.0, 2)
    catalog = ingest(ingest(ParentCatalog(), second), first)
    assert next(row.snapshot for row in options(catalog) if row.best_partial) == first
    benchmark = second.benchmark
    assert benchmark is not None
    partial = benchmark.partial_measurement
    assert partial is not None
    assert first.benchmark is not None
    first_partial = first.benchmark.partial_measurement
    assert first_partial is not None
    tied = second.model_copy(
        update={
            "benchmark": benchmark.model_copy(
                update={
                    "partial_measurement": partial.model_copy(
                        update={"progress": first_partial.progress}
                    )
                }
            )
        }
    )
    catalog = ingest(ingest(ParentCatalog(), tied), first)
    expected = first if first.benchmark.evidence_id < benchmark.evidence_id else tied
    assert next(row.snapshot for row in options(catalog) if row.best_partial) == expected
