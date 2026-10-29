"""The browser's native records cannot drift from the shipping Python projection."""

import hashlib
import json
from pathlib import Path

import pytest
from tests.support.journey_fixtures import ROOT, fingerprint, project_scenario, scenario_variants

from vs_proto.api import ClientReplay, Record, validate


@pytest.mark.parametrize(
    "path",
    [path for path in sorted(ROOT.glob("*.json")) if path.stem != "lineage600"],
    ids=lambda path: path.stem,
)
def test_native_journey_golden_is_current(path: Path) -> None:
    expected = project_scenario(path.stem)
    actual = ClientReplay.from_binary(path.with_suffix(".pb").read_bytes())
    assert actual == expected, "Regenerate: uv run python -m tests.support.journey_fixtures"
    records = [Record.from_binary(wire) for batch in actual.batches for wire in batch.records]
    assert [record.seq for record in records] == list(range(1, len(records) + 1))
    for record in records:
        validate(record)


def test_every_native_journey_tracks_its_source() -> None:
    # The 600-idea case takes a minute to regenerate: hash all inputs instead of repeating its
    # quadratic projector setup in CI. Small cases above compare real projection output directly.
    assert (ROOT / "source.sha256").read_text().strip() == fingerprint(), (
        "Regenerate: uv run python -m tests.support.journey_fixtures"
    )

    expected = (ROOT / "goldens.sha256").read_text()
    actual = "".join(
        f"{path.name} {hashlib.sha256(path.read_bytes()).hexdigest()}\n"
        for path in sorted(ROOT.glob("*.pb"))
    )
    assert actual == expected, "Native replay bytes differ: regenerate the journey goldens"


def test_recorded_world_uses_the_native_trace_source() -> None:
    source = json.loads((ROOT / "trace.json").read_text())
    queue = json.loads(
        (ROOT.parents[3] / "libs/run-fixtures/src/data/queue-rs-live.json").read_text()
    )
    assert queue["events"] == source["events"], "Regenerate the native journey fixtures"
    assert queue["objective"] == source["objective"]


@pytest.mark.parametrize("name", ["startup"])
def test_native_transitions_are_cumulative_golden_prefixes(name: str) -> None:
    previous = project_scenario(name)
    for variant, expected in scenario_variants(name).items():
        actual = ClientReplay.from_binary((ROOT / f"{variant}.pb").read_bytes())
        assert actual == expected, "Regenerate the native journey fixtures"
        assert list(actual.batches[: len(previous.batches)]) == list(previous.batches)
        assert len(actual.batches) == len(previous.batches) + 1
        assert dict(actual.streams) == dict(previous.streams)
        assert dict(actual.blobs) == dict(previous.blobs)
        row = Record.from_binary(actual.batches[-1].records[-1])
        assert row.body is not None
        assert row.body.field == "lifecycle"
        validate(row)
        previous = actual
