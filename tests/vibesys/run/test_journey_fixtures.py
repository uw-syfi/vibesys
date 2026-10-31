"""The browser's native journeys project to dense, valid records from their declarations."""

import json
from pathlib import Path

import pytest
from tests.support.golden_regeneration import REGENERATE
from tests.support.journey_fixtures import ROOT, project_scenario, scenario_variants

from vs_proto.api import Record, validate


@pytest.mark.parametrize(
    "path",
    [path for path in sorted(ROOT.glob("*.json")) if path.stem != "lineage600"],
    ids=lambda path: path.stem,
)
def test_native_journey_projects_to_dense_valid_records(path: Path) -> None:
    replay = project_scenario(path.stem)
    records = [Record.from_binary(wire) for batch in replay.batches for wire in batch.records]
    assert [record.seq for record in records] == list(range(1, len(records) + 1))
    for record in records:
        validate(record)


def test_recorded_world_uses_the_native_trace_source() -> None:
    source = json.loads((ROOT / "trace.json").read_text())
    queue = json.loads(
        (ROOT.parents[3] / "libs/run-fixtures/src/data/queue-rs-live.json").read_text()
    )
    message = f"queue-rs-live.json is a copy of trace.json; refresh it with {REGENERATE}"
    assert queue["events"] == source["events"], message
    assert queue["objective"] == source["objective"], message


@pytest.mark.parametrize("name", ["startup"])
def test_native_transitions_are_cumulative_prefixes(name: str) -> None:
    previous = project_scenario(name)
    for expected in scenario_variants(name).values():
        assert list(expected.batches[: len(previous.batches)]) == list(previous.batches)
        assert len(expected.batches) == len(previous.batches) + 1
        assert dict(expected.streams) == dict(previous.streams)
        assert dict(expected.blobs) == dict(previous.blobs)
        row = Record.from_binary(expected.batches[-1].records[-1])
        assert row.body is not None
        assert row.body.field == "lifecycle"
        validate(row)
        previous = expected
