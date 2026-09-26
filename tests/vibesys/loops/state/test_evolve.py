"""Model-only tests for strict evolve-loop persisted state."""

from typing import Any

import pytest
from pydantic import ValidationError

from vibesys.loops.state.api import (
    IndividualRecord,
    PopulationSnapshot,
    parse_population_snapshot,
    serialize_population_snapshot,
)


def _seed(*, id_: int = 1, generation: int = 0) -> IndividualRecord:
    return IndividualRecord(
        id=id_,
        generation=generation,
        parent_id=None,
        commit=f"commit-{id_}",
        perf_metric=10.0,
        perf_unit="ops/s",
        metrics={"throughput": 10.0},
        passed=True,
        summary="passing seed",
    )


def _child(*, id_: int = 2, parent_id: int = 1) -> IndividualRecord:
    return IndividualRecord(
        id=id_,
        generation=1,
        parent_id=parent_id,
        inspiration_ids=(),
        commit=f"commit-{id_}",
        perf_metric=12.5,
        perf_unit="ops/s",
        metrics={"throughput": 12.5, "latency_ms": 2.25},
        passed=True,
        summary="faster child",
        feedback="approved",
        policy_parent_id="policy-1",
        policy_target_island=0,
    )


def test_population_snapshot_codec_round_trips_all_current_individual_fields() -> None:
    snapshot = PopulationSnapshot(individuals=(_seed(), _child()))

    payload = serialize_population_snapshot(snapshot)

    assert payload == {
        "version": 1,
        "individuals": [
            {
                "id": 1,
                "generation": 0,
                "parent_id": None,
                "inspiration_ids": [],
                "commit": "commit-1",
                "perf_metric": 10.0,
                "perf_unit": "ops/s",
                "metrics": {"throughput": 10.0},
                "passed": True,
                "summary": "passing seed",
                "feedback": "",
                "policy_parent_id": None,
                "policy_target_island": None,
            },
            {
                "id": 2,
                "generation": 1,
                "parent_id": 1,
                "inspiration_ids": [],
                "commit": "commit-2",
                "perf_metric": 12.5,
                "perf_unit": "ops/s",
                "metrics": {"throughput": 12.5, "latency_ms": 2.25},
                "passed": True,
                "summary": "faster child",
                "feedback": "approved",
                "policy_parent_id": "policy-1",
                "policy_target_island": 0,
            },
        ],
    }
    assert parse_population_snapshot(payload) == snapshot


def test_individual_record_applies_declared_defaults() -> None:
    record = IndividualRecord(id=1, generation=0, parent_id=None)

    assert record.inspiration_ids == ()
    assert record.metrics == {}
    assert record.passed is False
    assert record.summary == ""
    assert record.feedback == ""


@pytest.mark.parametrize("metric", [float("nan"), float("inf"), float("-inf")])
def test_individual_record_rejects_non_finite_metrics(metric: float) -> None:
    with pytest.raises(ValidationError, match="finite number"):
        IndividualRecord(
            id=1,
            generation=0,
            parent_id=None,
            perf_metric=metric,
        )
    with pytest.raises(ValidationError, match="finite number"):
        IndividualRecord(
            id=1,
            generation=0,
            parent_id=None,
            metrics={"throughput": metric},
        )


def test_individual_record_rejects_coercion_unknown_fields_and_invalid_ids() -> None:
    string_id: dict[str, Any] = {"id": "1", "generation": 0, "parent_id": None}
    with pytest.raises(ValidationError):
        IndividualRecord(**string_id)
    with pytest.raises(ValidationError):
        IndividualRecord.model_validate(
            {"id": 1, "generation": 0, "parent_id": None, "unknown": True}
        )
    with pytest.raises(ValidationError):
        IndividualRecord(id=0, generation=0, parent_id=None)


def test_individual_record_validates_internal_references_and_commit() -> None:
    with pytest.raises(ValidationError, match="parent_id cannot reference"):
        IndividualRecord(id=1, generation=0, parent_id=1)
    with pytest.raises(ValidationError, match="inspiration_ids must be unique"):
        IndividualRecord(id=3, generation=1, parent_id=None, inspiration_ids=(1, 1))
    with pytest.raises(ValidationError, match="cannot reference the individual itself"):
        IndividualRecord(id=3, generation=1, parent_id=None, inspiration_ids=(3,))
    with pytest.raises(ValidationError, match="parent cannot also be an inspiration"):
        IndividualRecord(id=3, generation=1, parent_id=1, inspiration_ids=(1,))
    with pytest.raises(ValidationError, match="passing individual requires a commit"):
        IndividualRecord(id=1, generation=0, parent_id=None, passed=True)
    with pytest.raises(ValidationError, match="perf_unit requires perf_metric"):
        IndividualRecord(id=1, generation=0, parent_id=None, perf_unit="ops/s")


def test_individual_record_rejects_blank_identifiers_and_metric_names() -> None:
    with pytest.raises(ValidationError, match="optional identifiers must not be blank"):
        IndividualRecord(id=1, generation=0, parent_id=None, commit=" ")
    with pytest.raises(ValidationError, match="metric names must not be empty"):
        IndividualRecord(id=1, generation=0, parent_id=None, metrics={" ": 1.0})


def test_population_snapshot_rejects_duplicate_and_unordered_ids() -> None:
    with pytest.raises(ValidationError, match="individual ids must be unique"):
        PopulationSnapshot(individuals=(_seed(), _seed()))
    with pytest.raises(ValidationError, match="ordered by increasing id"):
        PopulationSnapshot(individuals=(_seed(id_=2), _seed(id_=1)))


def test_population_snapshot_validates_lineage_references() -> None:
    with pytest.raises(ValidationError, match="references missing individual 9"):
        PopulationSnapshot(individuals=(_seed(), _child(parent_id=9)))

    failed = IndividualRecord(id=1, generation=0, parent_id=None, passed=False)
    with pytest.raises(ValidationError, match="references failed individual 1"):
        PopulationSnapshot(individuals=(failed, _child()))

    later_generation = _seed(generation=2)
    with pytest.raises(ValidationError, match="references a later generation"):
        PopulationSnapshot(individuals=(later_generation, _child()))

    future_parent = IndividualRecord(id=1, generation=1, parent_id=2)
    with pytest.raises(ValidationError, match="must reference an earlier individual id"):
        PopulationSnapshot(individuals=(future_parent, _seed(id_=2, generation=0)))


def test_population_snapshot_rejects_wrong_version_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        parse_population_snapshot({"version": 2, "individuals": []})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_population_snapshot({"version": 1, "individuals": [], "extra": True})
