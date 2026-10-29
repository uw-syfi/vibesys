"""Shared client scenarios have one canonical source and checked native projection output."""

import hashlib
import json
from pathlib import Path

import pytest
from tests.support.shared_scenarios import GOLDENS, declarations, fingerprint, project_shared

from vs_proto.api import ClientReplay, Record, validate


@pytest.mark.parametrize(
    "path",
    [path for path in declarations() if path.stem != "weeks-600-ideas"],
    ids=lambda path: path.stem,
)
def test_shared_scenario_is_exactly_the_real_native_projection(path: Path) -> None:
    expected = project_shared(path)
    actual = ClientReplay.from_binary((GOLDENS / f"{path.stem}.pb").read_bytes())
    assert actual == expected, "Regenerate: uv run python -m tests.support.journey_fixtures"
    for batch in actual.batches:
        for wire in batch.records:
            validate(Record.from_binary(wire))


def test_shared_native_sources_and_large_golden_cannot_drift() -> None:
    assert (GOLDENS / "source.sha256").read_text().strip() == fingerprint(), (
        "Regenerate: uv run python -m tests.support.journey_fixtures"
    )
    expected = (GOLDENS / "goldens.sha256").read_text()
    assert expected == "".join(
        f"{path.name} {hashlib.sha256(path.read_bytes()).hexdigest()}\n"
        for path in sorted(GOLDENS.glob("*.pb"))
    )


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
