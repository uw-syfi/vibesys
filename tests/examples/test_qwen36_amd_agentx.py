"""AMD workload tasks and the TraceLab adapter's public result validator."""

from __future__ import annotations

import csv
import hashlib
import runpy
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.examples.example_task import load_example_task, task_root

_PROJECT = Path(__file__).parents[2] / "examples/model-serving/qwen3.6-35b-a3b-2xh100"
_TRACE = _PROJECT / ".vibesys/shared/tracelab"
_REPLAY = SimpleNamespace(**runpy.run_path(str(_TRACE / "replay.py")))


@pytest.mark.parametrize("workload", ["agentx", "tracelab"])
def test_amd_tasks_share_workload_but_declare_distinct_hardware(workload: str) -> None:
    small = load_example_task(_PROJECT, f"mi2104x-{workload}").manifest
    large = load_example_task(_PROJECT, f"mi300x-{workload}").manifest
    assert small.catalogue is not None
    assert large.catalogue is not None
    assert (small.catalogue.hardware.accelerator, small.catalogue.hardware.count) == (
        "AMD MI210",
        4,
    )
    assert (large.catalogue.hardware.accelerator, large.catalogue.hardware.count) == (
        "AMD MI300X",
        1,
    )
    assert small.backends == large.backends
    assert small.benchmark == large.benchmark
    assert small.accuracy == large.accuracy
    assert small.benchmark.result_protocol == 2
    assert small.benchmark.entrypoint == (
        "agentx-v1" if workload == "agentx" else "request-factory-adapter"
    )
    for hardware in ("mi2104x", "mi300x"):
        root = task_root(_PROJECT, f"{hardware}-{workload}")
        objectives = tomllib.loads((root / "objectives.toml").read_text())
        assert {item["name"] for item in objectives["objective"]} == set(_REPLAY.METRICS)
        assert objectives["pareto"]["relative_noise"] == 0.03
        assert _REPLAY.REVISION in (root / "reference/meta.json").read_text()


def test_recorded_sample_is_pinned_and_contains_every_round() -> None:
    path = _TRACE / "recorded_sessions.csv"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == _REPLAY.TRACE_SHA256
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 303
    assert len({row["session_id"] for row in rows}) == 48
    assert sum(int(row["output_len"]) for row in rows) == 58678
    assert any(float(row["tool_wait_after_ms"]) > 0 for row in rows)
    assert any(int(row["prefix_len"]) > 0 for row in rows)


def _summary(value: object = 1.0) -> dict:
    return {
        "replay": {
            "kind": "sessions",
            "common": {
                "attempted_steps": 303,
                "success_steps": 303,
                "failed_steps": 0,
                "actual_output_tokens": 58678,
                "target_output_tokens": 58678,
                **dict.fromkeys(_REPLAY.METRICS.values(), value),
            },
        },
    }


@given(value=st.floats(min_value=0.001, max_value=1e10, allow_nan=False))
def test_trace_result_preserves_native_measured_values(value: float) -> None:
    assert _REPLAY.summary_metrics(_summary(value), 303, 58678) == dict.fromkeys(
        _REPLAY.METRICS, value
    )


@pytest.mark.parametrize("field", list(_REPLAY.METRICS.values()))
@pytest.mark.parametrize("value", [None, True, "1", -1, float("nan"), float("inf")])
def test_trace_result_rejects_invalid_metrics(field: str, value: object) -> None:
    summary = _summary()
    summary["replay"]["common"][field] = value
    with pytest.raises(ValueError, match=field):
        _REPLAY.summary_metrics(summary, 303, 58678)


@pytest.mark.parametrize(
    "field",
    [
        "attempted_steps",
        "success_steps",
        "failed_steps",
        "actual_output_tokens",
        "target_output_tokens",
    ],
)
@given(value=st.one_of(st.booleans(), st.integers(min_value=1, max_value=302)))
def test_trace_result_rejects_incomplete_or_failed_runs(field: str, value: object) -> None:
    summary = _summary()
    summary["replay"]["common"][field] = value
    with pytest.raises(ValueError, match=field):
        _REPLAY.summary_metrics(summary, 303, 58678)


@pytest.mark.parametrize(
    "summary",
    [None, {}, {"replay": {}}, {"replay": {"kind": "independent_requests", "common": {}}}],
)
def test_trace_result_requires_session_metrics(summary: object) -> None:
    with pytest.raises(ValueError, match="replay"):
        _REPLAY.summary_metrics(summary, 303, 58678)
