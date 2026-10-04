"""Parent options over core evidence: chronology is separate from fitness."""

from __future__ import annotations

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.vibesys.orchestration.dynamic.strategy._views import (
    empty_view,
    partial,
    snapshot,
    view_proving,
)

from vibesys.orchestration.dynamic.strategy.api import (
    ParentConflictError,
    ParentSnapshot,
    ingest,
    options,
    resolve,
)


def _catalog(rows: tuple[ParentSnapshot, ...]) -> tuple[ParentSnapshot, ...]:
    view = view_proving(*rows)
    catalog: tuple[ParentSnapshot, ...] = ()
    for row in rows:
        catalog = ingest(catalog, row, view)
    return catalog


def test_observed_regression_keeps_best_and_latest_exact_revisions() -> None:
    """Ports test_parent_catalog.py::test_observed_regression_keeps_best_and_latest_exact_revisions."""
    first, second = snapshot(79.835, 1), snapshot(72.564, 2)
    view = view_proving(first, second)
    catalog = _catalog((first, second))
    offered = options(catalog, view)
    assert len(offered) == 2
    assert next(row.snapshot for row in offered if row.best_partial) == first
    assert next(row.snapshot for row in offered if row.latest_verified) == second
    assert resolve(catalog, view, "source", first.revision.revision_id.root) == first
    assert resolve(catalog, view, "source") == second
    assert resolve(catalog, view, "foreign", first.revision.revision_id.root) is None
    assert resolve(catalog, view, "source", "missing") is None
    assert (
        tuple(ParentSnapshot.model_validate_json(r.model_dump_json()) for r in catalog) == catalog
    )


@given(st.permutations((0, 1, 2, 3)), st.lists(st.integers(min_value=0, max_value=3), max_size=10))
def test_order_and_replay_preserve_catalog_and_indexes(order: list[int], replay: list[int]) -> None:
    """Ports test_parent_catalog.py::test_order_and_replay_preserve_catalog_and_indexes."""
    rows = tuple(snapshot(v, n) for n, v in enumerate((79.835, 72.564, 83.0, 77.0), 1))
    view = view_proving(*rows)
    expected: tuple[ParentSnapshot, ...] = ()
    for row in rows:
        expected = ingest(expected, row, view)
    actual: tuple[ParentSnapshot, ...] = ()
    for index in (*order, *replay):
        actual = ingest(actual, rows[index], view)
    assert actual == expected
    assert options(actual, view) == options(expected, view)


def test_unretained_or_unproved_snapshot_supplies_no_authority() -> None:
    """Ports test_parent_catalog.py::test_ineligible_identity_and_unknown_chronology_supply_no_authority."""
    row = snapshot(79.835, 1)
    assert ingest((), row, view_proving(row, retained=False)) == ()
    assert ingest((), row, empty_view()) == ()
    forged = row.model_copy(update={"revision": snapshot(1.0, 9).revision})
    assert ingest((), forged, view_proving(row)) == ()
    unknown = row.model_copy(update={"submission_index": 0})
    catalog = ingest((), unknown, view_proving(row))
    assert resolve(catalog, view_proving(row), "source") is None
    assert resolve(catalog, view_proving(row), "source", row.revision.revision_id.root) is not None


@pytest.mark.parametrize("field", ["unit", "direction", "name", "target", "progress"])
def test_changed_partial_context_is_never_ranked_with_original(field: str) -> None:
    """Ports test_parent_catalog.py::test_changed_partial_context_is_never_ranked_with_original."""
    first, second = snapshot(79.835, 1), snapshot(72.564, 2)
    assert second.benchmark is not None
    assert second.benchmark.partial is not None
    changes = {
        "unit": {"unit": "requests/s"},
        "direction": {"direction": "min"},
        "name": {"name": "latency"},
        "target": {"target": 100.0},
        "progress": {"required": 100, "progress_unit": "requests"},
    }[field]
    changed = second.benchmark.model_copy(
        update={"partial": second.benchmark.partial.model_copy(update=changes)}
    )
    second = second.model_copy(update={"benchmark": changed})
    view = view_proving(first, second)
    rows = options(_catalog((first, second)), view)
    assert len(rows) == 2
    assert all(row.best_partial for row in rows)
    assert rows[0].comparison_key != rows[1].comparison_key


def test_unknown_unit_is_buildable_without_comparison_authority() -> None:
    """Ports test_parent_catalog.py::test_unknown_unit_is_buildable_without_comparison_authority."""
    row = snapshot(79.835, 1)
    assert row.benchmark is not None
    assert row.benchmark.partial is not None
    unknown = row.model_copy(
        update={
            "benchmark": row.benchmark.model_copy(
                update={"partial": row.benchmark.partial.model_copy(update={"unit": None})}
            )
        }
    )
    view = view_proving(unknown)
    offered = options(_catalog((unknown,)), view)
    assert offered[0].comparison_key is None
    assert not offered[0].best_partial
    assert resolve(_catalog((unknown,)), view, "source", row.revision.revision_id.root) == unknown


@given(
    st.lists(
        st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=12,
    ),
    st.sampled_from(("max", "min")),
)
def test_best_observed_partial_is_monotone_in_declared_direction(
    values: list[float], direction: Literal["max", "min"]
) -> None:
    """Ports test_parent_catalog.py::test_best_observed_partial_is_monotone_in_declared_direction."""
    rows: list[ParentSnapshot] = []
    for index, value in enumerate(values, 1):
        base = snapshot(value, index)
        assert base.benchmark is not None
        new_partial = partial(value, completed=71, direction=direction)
        rows.append(
            base.model_copy(
                update={"benchmark": base.benchmark.model_copy(update={"partial": new_partial})}
            )
        )
        view = view_proving(*rows)
        catalog = _catalog(tuple(rows))
        best = next(row.snapshot for row in options(catalog, view) if row.best_partial)
        assert best.benchmark is not None
        assert best.benchmark.partial is not None
        observed = values[:index]
        assert best.benchmark.partial.value == (
            max(observed) if direction == "max" else min(observed)
        )


def test_failed_accuracy_never_builds_even_with_better_partial() -> None:
    """Ports test_parent_catalog.py::test_failed_accuracy_never_builds_even_with_better_partial."""
    row = snapshot(79.835, 1)
    failed = row.model_copy(update={"accuracy": row.accuracy.model_copy(update={"passed": False})})
    assert options(ingest((), failed, view_proving(failed)), view_proving(failed)) == ()


def test_benchmark_settlement_extends_without_accuracy_replay_downgrade() -> None:
    """Ports test_parent_catalog.py::test_benchmark_settlement_extends_without_accuracy_replay_downgrade."""
    settled = snapshot(79.835, 1)
    accuracy_only = settled.model_copy(update={"benchmark": None})
    view = view_proving(settled)
    catalog = ingest(ingest((), accuracy_only, view), settled, view)
    assert ingest(catalog, accuracy_only, view) == catalog
    assert options(catalog, view)[0].best_partial


def test_conflicting_benchmark_receipt_for_one_identity_raises() -> None:
    row = snapshot(79.835, 1)
    other = snapshot(70.0, 1)
    view = view_proving(row)
    catalog = ingest((), row, view)
    with pytest.raises(ParentConflictError):
        ingest(catalog, other, view)


def test_parent_records_reject_unknown_keys_and_mutation() -> None:
    """Ports test_parent_catalog.py::test_parent_records_reject_unknown_feature_keys_and_mutation."""
    row = snapshot(79.835, 1)
    with pytest.raises(ValidationError, match="unknown_features"):
        ParentSnapshot.model_validate({**row.model_dump(), "unknown_features": ["batching"]})
    for field_name in ParentSnapshot.model_fields:
        with pytest.raises(ValidationError, match="frozen"):
            setattr(row, field_name, getattr(row, field_name))


def test_equal_observed_partials_choose_comparable_progress_then_stable_receipt() -> None:
    """Ports test_parent_catalog.py::test_equal_observed_partials_choose_comparable_progress_then_stable_receipt."""
    first, second = snapshot(10.0, 1), snapshot(10.0, 2)
    view = view_proving(first, second)
    catalog = _catalog((second, first))
    assert next(row.snapshot for row in options(catalog, view) if row.best_partial) == first
