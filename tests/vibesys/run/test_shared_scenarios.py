"""Shared client scenarios have one canonical source and checked native projection output."""

import json
from pathlib import Path

import pytest
from tests.support.replay_golden import assert_replay_golden
from tests.support.shared_scenarios import GOLDENS, declarations, project_shared

from vs_proto.api import Record, validate


@pytest.mark.parametrize(
    "path",
    [path for path in declarations() if path.stem != "weeks-600-ideas"],
    ids=lambda path: path.stem,
)
def test_shared_scenario_is_exactly_the_real_native_projection(path: Path) -> None:
    actual = assert_replay_golden(GOLDENS / f"{path.stem}.pb", project_shared(path))
    for batch in actual.batches:
        for wire in batch.records:
            validate(Record.from_binary(wire))


@pytest.mark.parametrize("path", declarations(), ids=lambda path: path.stem)
def test_every_declared_scalar_measurement_has_canonical_metric_rows(path: Path) -> None:
    fixture = json.loads(path.read_text())
    for event in fixture["events"]:
        span = (event.get("data") or {}).get("span") or {}
        label = span.get("label") or {}
        if label.get("kind") != "gate":
            continue
        attrs = json.loads(label.get("attrs_json") or "{}")
        if "metric_value" in attrs:
            assert attrs.get("metrics"), f"{path.name}: {span['id']} has no native metric rows"
            metric = attrs.get("metric_name", fixture["objective"]["metric"])
            readings = {row["name"]: row["value"] for row in attrs["metrics"]}
            assert readings.get(metric) == attrs["metric_value"], (
                f"{path.name}: {span['id']} scalar {metric} disagrees with its canonical metric row"
            )
