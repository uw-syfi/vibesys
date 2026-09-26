"""Model-only tests for strict plain-loop persisted state."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vibesys.loops.state.api import (
    PlainLoopCursor,
    PlainPerformanceRecord,
    PlainPerformanceSnapshot,
    parse_plain_loop_cursor,
    parse_plain_performance_snapshot,
    serialize_plain_loop_cursor,
    serialize_plain_performance_snapshot,
)


def _performance_record(iteration: int = 1) -> PlainPerformanceRecord:
    return PlainPerformanceRecord(
        iteration=iteration,
        timestamp=datetime(2026, 8, 12, 1, 2, 3, tzinfo=UTC),
        throughput_trend="improved",
        latency_trend="mixed",
        metrics={
            "load_levels": [
                {
                    "target_rate": 8.0,
                    "throughput": {"request_throughput": 7.5},
                }
            ],
            "extra": {"gpu_memory_bytes": 1024},
        },
        new_issue_ids=(2, 4),
    )


def test_plain_cursor_codec_round_trips_current_schema() -> None:
    cursor = PlainLoopCursor(
        round_idx=3,
        phase="judge",
        current_issue_id=7,
        bootstrap_done=True,
    )

    payload = serialize_plain_loop_cursor(cursor)

    assert payload == {
        "version": 1,
        "round_idx": 3,
        "phase": "judge",
        "current_issue_id": 7,
        "bootstrap_done": True,
    }
    assert parse_plain_loop_cursor(payload) == cursor


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2},
        {"version": 1, "round_idx": "3"},
        {"version": 1, "surprise": True},
        {"version": 1, "round_idx": -1},
    ],
)
def test_plain_cursor_rejects_invalid_portable_payloads(payload: dict[str, object]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_plain_loop_cursor(payload)


def test_plain_cursor_validates_phase_issue_relationship() -> None:
    with pytest.raises(ValidationError, match="judge phase requires current_issue_id"):
        PlainLoopCursor(phase="judge")
    with pytest.raises(ValidationError, match="perf_eval phase cannot reference"):
        PlainLoopCursor(phase="perf_eval", current_issue_id=1)


def test_plain_performance_snapshot_codec_preserves_typed_history() -> None:
    snapshot = PlainPerformanceSnapshot(records=(_performance_record(),))

    payload = serialize_plain_performance_snapshot(snapshot)
    restored = parse_plain_performance_snapshot(payload)

    assert payload["version"] == 1
    assert payload["records"][0]["timestamp"] == "2026-08-12T01:02:03Z"
    assert restored == snapshot
    assert restored.records[0].timestamp.tzinfo is not None


def test_plain_performance_record_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone info"):
        PlainPerformanceRecord(
            iteration=1,
            timestamp=datetime(2026, 8, 12, 1, 2, 3),  # noqa: DTZ001  # lint-waiver: LW-010104 [DTZ001]; this test intentionally supplies a naive timestamp to verify rejection.
            throughput_trend="mixed",
            latency_trend="mixed",
            metrics={},
        )


@pytest.mark.parametrize("metric", [float("nan"), float("inf"), float("-inf")])
def test_plain_performance_record_rejects_nested_non_finite_metrics(metric: float) -> None:
    with pytest.raises(ValidationError, match="finite numbers"):
        PlainPerformanceRecord(
            iteration=1,
            timestamp=datetime(2026, 8, 12, tzinfo=UTC),
            throughput_trend="mixed",
            latency_trend="mixed",
            metrics={"nested": {"metric": metric}},
        )


def test_plain_performance_record_rejects_duplicate_issue_ids_and_extra_fields() -> None:
    record = _performance_record()
    with pytest.raises(ValidationError, match="new_issue_ids must be unique"):
        PlainPerformanceRecord.model_validate({**record.model_dump(), "new_issue_ids": (2, 2)})
    payload = serialize_plain_performance_snapshot(
        PlainPerformanceSnapshot(records=(_performance_record(),))
    )
    payload["records"][0]["unknown"] = "value"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_plain_performance_snapshot(payload)


def test_plain_performance_record_rejects_empty_metric_names() -> None:
    with pytest.raises(ValidationError, match="metric names must not be empty"):
        PlainPerformanceRecord(
            iteration=1,
            timestamp=datetime(2026, 8, 12, tzinfo=UTC),
            throughput_trend="mixed",
            latency_trend="mixed",
            metrics={" ": 1.0},
        )


def test_plain_performance_snapshot_requires_strictly_increasing_iterations() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        PlainPerformanceSnapshot(records=(_performance_record(2), _performance_record(1)))
    with pytest.raises(ValidationError, match="strictly increasing"):
        PlainPerformanceSnapshot(records=(_performance_record(1), _performance_record(1)))
